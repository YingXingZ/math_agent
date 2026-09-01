"""Evidence-first grading pipeline for the teacher-facing agent."""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from .config import settings
from .db import connection, normalize_question_type
from .agent_tools import run_tool_use
from .llm_provider import grade_homework, model_runtime
from .prompt_security import inspect_untrusted_text


def _recognition_is_contaminated(recognized_work: str, reference_answer: str) -> bool:
    """Detect a dangerous VLM failure: copying the supplied answer into OCR text.

    This is intentionally conservative.  A long normalised overlap means the
    display must be withheld and sent to teacher review, rather than allowing
    generated reference prose to become false evidence of student work.
    """
    compact = lambda value: "".join(ch for ch in str(value or "") if not ch.isspace() and ch not in "$\\{}()[]，,。；;：:")
    work, reference = compact(recognized_work), compact(reference_answer)
    return len(work) >= 30 and len(reference) >= 30 and (work in reference or reference in work)


def _math_equal(student_answer: str, standard_answer: str) -> dict[str, Any]:
    """Use the existing symbolic engine as an independent check of Qwen."""
    if not student_answer.strip() or not standard_answer.strip():
        return {"available": False, "equal": None, "confidence": 0, "method": "缺少可判等表达式"}
    try:
        project_root = str(Path(__file__).resolve().parents[2])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from grading_engine import expr_equal

        equal, confidence, method = expr_equal(student_answer, standard_answer)
        if equal:
            return {"available": True, "equal": equal, "confidence": confidence, "method": method}

        # Handwritten calculation work is commonly recognised as a complete
        # equality (for example ``1+0=1``), while the answer key stores only
        # the final value (``1``).  Compare its right-hand side only for a
        # single plain equality.  Do not apply this to inequalities, chained
        # equations, set membership, or prose: those carry mathematical
        # structure that must remain in the normal conservative path.
        raw = student_answer.strip().replace("＝", "=")
        if (raw.count("=") == 1 and not any(token in raw for token in ("<", ">", "≤", "≥", "≠", "∈", "∉"))):
            _left, rhs = raw.split("=", 1)
            if _left.strip() and rhs.strip():
                rhs_equal, rhs_confidence, rhs_method = expr_equal(rhs.strip(), standard_answer)
                if rhs_equal:
                    return {
                        "available": True,
                        "equal": True,
                        "confidence": rhs_confidence,
                        "method": f"等式右端与标准答案一致（{rhs_method}）",
                    }
        return {"available": True, "equal": equal, "confidence": confidence, "method": method}
    except Exception as exc:
        return {"available": False, "equal": None, "confidence": 0, "method": f"判等不可用：{str(exc)[:100]}"}


def _render_pdf_pages(path: Path) -> list[Path]:
    """Render a bounded PDF copy for Qwen while keeping the original upload."""
    try:
        from pypdf import PdfReader
        page_count = len(PdfReader(str(path)).pages)
    except Exception as exc:
        raise ValueError(f"无法读取 PDF：{str(exc)[:100]}") from exc
    if page_count == 0:
        raise ValueError("PDF 没有可识别页面")

    # 设计文档 3.1：大作业"自动分页渲染 + 跨页定位"，而非整份拒识。
    # 渲染全部页面交给 VLM 的两遍定位器跨页匹配题号；仅在极端页数（>安全上限）
    # 时才转人工，避免把真实多页作业误判为"无法识别"。
    SAFE_PAGE_CEILING = 80
    if page_count > SAFE_PAGE_CEILING:
        raise ValueError(
            f"PDF 共 {page_count} 页，超过安全上限 {SAFE_PAGE_CEILING} 页，已转入教师复核。"
        )
    page_limit = page_count
    rendered_dir = path.parent / f"{path.stem}_qwen_pages"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    prefix = rendered_dir / "page"
    bundled_renderer = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
    renderer = (
        settings.pdf_renderer_path
        or os.environ.get("PDFTOPPM_PATH", "")
        or (str(bundled_renderer) if bundled_renderer.is_file() else "")
        or shutil.which("pdftoppm")
    )
    if not renderer:
        raise ValueError("PDF 渲染组件不可用，已转入教师复核")
    command = [
        renderer, "-png", "-r", str(settings.qwen_pdf_render_dpi),
        "-f", "1", "-l", str(page_limit), str(path), str(prefix),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=180)
    except FileNotFoundError as exc:
        raise ValueError("PDF 渲染组件不可用，已转入教师复核") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"PDF 转图片失败：{exc.stderr.decode(errors='replace')[:120]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("PDF 转图片超时，已转入教师复核") from exc

    pages = sorted(rendered_dir.glob("page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[-1]))
    if not pages:
        raise ValueError("PDF 未生成可识别页面")
    return pages


def _submission_page_paths(file_path: str) -> list[Path]:
    """Return the original image page(s) used by grading, never alter the upload."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return [path]
    if suffix == ".pdf":
        return _render_pdf_pages(path)
    raise ValueError("暂不支持 Word 自动识别，已转入教师复核，不会误判。")


def _load_images(file_path: str) -> list[str]:
    return [base64.b64encode(page.read_bytes()).decode("ascii")
            for page in _submission_page_paths(file_path)]


def _crop_confirmed_region(file_path: str, mapping: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Crop one teacher-confirmed normalized rectangle without modifying the source page."""
    pages = _submission_page_paths(file_path)
    page_no = int(mapping["page_no"])
    if page_no < 1 or page_no > len(pages):
        raise ValueError("教师确认的页码不存在")
    try:
        with Image.open(pages[page_no - 1]) as source:
            image = source.convert("RGB")
            full_width, full_height = image.size
            left = max(0, min(full_width - 1, round(float(mapping["x"]) * full_width)))
            top = max(0, min(full_height - 1, round(float(mapping["y"]) * full_height)))
            right = max(left + 1, min(full_width, round((float(mapping["x"]) + float(mapping["width"])) * full_width)))
            bottom = max(top + 1, min(full_height, round((float(mapping["y"]) + float(mapping["height"])) * full_height)))
            if right - left < 32 or bottom - top < 32:
                raise ValueError("教师确认的区域过小，无法可靠识别")
            cropped = image.crop((left, top, right, bottom))
            buffer = io.BytesIO()
            cropped.save(buffer, format="PNG", optimize=True)
    except OSError as exc:
        raise ValueError("无法读取教师确认区域所在页图") from exc
    return base64.b64encode(buffer.getvalue()).decode("ascii"), {
        "mode": "teacher_confirmed_crop",
        "page_no": page_no,
        "region": {key: float(mapping[key]) for key in ("x", "y", "width", "height")},
        "crop_size": {"width": right - left, "height": bottom - top},
    }


async def grade_submission(submission_id: int) -> dict[str, Any]:
    """Retrieve assignment evidence, call Qwen, then cross-check each calc answer."""
    with connection() as conn:
        submission = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not submission:
            raise ValueError("提交不存在")
        parents = [dict(row) for row in conn.execute(
            """SELECT aq.question_id, aq.sort_order, aq.score AS max_score, aq.original_no, q.*
               FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
               WHERE aq.assignment_id=? ORDER BY aq.sort_order""",
            (submission["assignment_id"],),
        ).fetchall()]
        selected_parts = [dict(row) for row in conn.execute(
            """SELECT question_id,subpart_no,part_order,content,answer,rubric,score
               FROM assignment_question_parts WHERE assignment_id=?
               ORDER BY question_id,part_order""",
            (submission["assignment_id"],),
        ).fetchall()]

    if not parents:
        raise ValueError("该作业未关联题目")
    parts_by_parent: dict[int, list[dict]] = {}
    for part in selected_parts:
        parts_by_parent.setdefault(part["question_id"], []).append(part)
    questions: list[dict] = []
    for parent in parents:
        parts = parts_by_parent.get(parent["question_id"], [])
        if not parts:
            parent["grading_key"] = str(parent["question_id"])
            parent["subpart_no"] = None
            parent["problem_no"] = str(parent["original_no"] or parent["sort_order"])
            questions.append(parent)
            continue
        for part in parts:
            questions.append({
                **parent, **part,
                "max_score": float(part["score"]),
                "grading_key": f"{parent['question_id']}:{part['subpart_no']}",
                "problem_no": f"{parent['original_no'] or parent['sort_order']}（{part['subpart_no']}）",
            })
    problems = [
        {
            "problem_id": row["grading_key"],
            "problem_no": row["problem_no"],
            "problem_text": row["content"],
            "std_answer": row["answer"] or "",
            "full_solution": row["rubric"] or "",
            "max_score": float(row["max_score"]),
        }
        for row in questions
    ]

    # A teacher-confirmed rectangle is stronger evidence than an image-wide
    # number guess.  Each confirmed question/subpart is therefore cropped and
    # graded in its own Qwen request.  Unconfirmed items deliberately retain
    # the legacy full-page locator path instead of pretending they were cut.
    with connection() as conn:
        region_rows = [dict(item) for item in conn.execute(
            """SELECT question_id,subpart_no,sort_order,page_no,x,y,width,height,confirmed_at
               FROM submission_question_regions WHERE submission_id=?""", (submission_id,)
        ).fetchall()]
    regions = {(item["question_id"], item["subpart_no"] or "", item["sort_order"]): item for item in region_rows}
    qwen_by_question: dict[str, dict[str, Any]] = {}
    qwen_context_by_question: dict[str, dict[str, Any]] = {}
    crop_errors: dict[str, str] = {}
    qwen_errors: list[str] = []
    unconfirmed: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def request_qwen(images: list[str], requested_problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return await grade_homework(images, requested_problems)

    for row, problem in zip(questions, problems):
        qid = row["grading_key"]
        region_key = (row["question_id"], row.get("subpart_no") or "", row["sort_order"])
        mapping = regions.get(region_key)
        # A default full-page rectangle is only a UI placeholder, not proof
        # that the teacher identified this particular question.  Crop only a
        # genuinely boxed region; otherwise preserve the full-page locator.
        is_confirmed_crop = bool(mapping) and (
            float(mapping.get("width", 1)) < 0.999 or float(mapping.get("height", 1)) < 0.999
        )
        if not is_confirmed_crop:
            unconfirmed.append((row, problem))
            continue
        try:
            crop_payload, crop_context = _crop_confirmed_region(submission["file_path"], mapping)
            confirmed_problem = {
                **problem,
                "problem_text": "【教师已确认：本图片仅包含当前题目/小问的作答区域；不要再猜测或匹配其他题号。】\n"
                + str(problem.get("problem_text") or ""),
            }
            qwen_items = await request_qwen([crop_payload], [confirmed_problem])
            qwen_by_question[qid] = next(
                (item for item in qwen_items if str(item.get("problem_id")) == qid), {}
            )
            qwen_context_by_question[qid] = crop_context
        except Exception as exc:
            crop_errors[qid] = str(exc)[:180]
            # Keep the original full-page candidate route as a visible fallback,
            # never replace it silently with a cropped result.
            unconfirmed.append((row, problem))

    if unconfirmed:
        try:
            image_payload = _load_images(submission["file_path"])
            qwen_items = await request_qwen(image_payload, [item[1] for item in unconfirmed])
            fallback_by_question = {str(item.get("problem_id")): item for item in qwen_items}
            for row, _problem in unconfirmed:
                qid = row["grading_key"]
                qwen_by_question[qid] = fallback_by_question.get(qid, {})
                qwen_context_by_question[qid] = {
                    "mode": "full_page_locator_fallback" if qid in crop_errors else "full_page_locator",
                    "page_count": len(image_payload),
                }
        except Exception as exc:
            message = str(exc)[:180]
            qwen_errors.append(message)
            for row, _problem in unconfirmed:
                qid = row["grading_key"]
                qwen_by_question.setdefault(qid, {})
                qwen_context_by_question.setdefault(qid, {
                    "mode": "qwen_unavailable",
                    "detail": message,
                })

    qwen_error = "；".join(qwen_errors)

    results: list[dict[str, Any]] = []
    for row in questions:
        qid = row["grading_key"]
        qwen = qwen_by_question.get(qid, {})
        qwen_input = qwen_context_by_question.get(qid, {"mode": "unknown"})
        standard_answer = row["answer"] or ""
        recognized = str(qwen.get("recognized_work") or "")
        is_proof = normalize_question_type(row["question_type"]) == "proof"
        risks = list(qwen.get("risks") or [])
        input_guard = inspect_untrusted_text(str(row["content"] or ""))
        recognition_guard = inspect_untrusted_text(recognized)
        if input_guard.suspicious:
            risks.append("题目文本含疑似提示词注入内容：" + "、".join(input_guard.reasons))
        if recognition_guard.suspicious:
            risks.append("学生识别文本含疑似提示词注入内容：" + "、".join(recognition_guard.reasons))
        if _recognition_is_contaminated(recognized, standard_answer):
            # Do not show or grade against text which may have been generated
            # from the reference answer injected into the VLM grading prompt.
            recognized = ""
            risks.append("识别结果疑似受参考答案污染，已隐藏，需以原图人工复核")
        tool_use = run_tool_use(
            question_id=int(row["question_id"]),
            question_type=normalize_question_type(row["question_type"]),
            problem_text=str(row["content"] or ""),
            recognized_work=recognized,
            standard_answer=standard_answer,
        )
        equivalent = tool_use["math_equivalence"]
        review_reasons: list[str] = []
        if qwen_error:
            review_reasons.append("Qwen 识别失败")
        if qid in crop_errors:
            review_reasons.append(f"已确认区域裁剪失败，已回退整页识别：{crop_errors[qid]}")
        if not standard_answer:
            review_reasons.append("缺少标准答案")
        if normalize_question_type(row["question_type"]) != "calc":
            review_reasons.append("证明/非计算题需教师复核")
        if float(qwen.get("confidence", 0) or 0) < 0.85:
            review_reasons.append("Qwen 置信度不足")
        if qwen.get("need_review", True):
            review_reasons.append("Qwen 标记为需复核")
        if any("参考答案污染" in str(risk) for risk in risks):
            review_reasons.append("识别文本疑似参考答案污染")
        if input_guard.suspicious or recognition_guard.suspicious:
            review_reasons.append("检测到疑似提示词注入内容，已保留证据并转教师复核")
        if equivalent["available"] and equivalent["confidence"] < 0.85:
            review_reasons.append("数学判等置信度不足")
        if equivalent["available"] and qwen.get("correct") is not None and bool(qwen.get("correct")) != bool(equivalent["equal"]):
            review_reasons.append("Qwen 与数学判等结论不一致")

        needs_review = bool(review_reasons)
        results.append({
            "question_id": row["question_id"],
            "question_type": normalize_question_type(row["question_type"]),
            "subpart_no": row.get("subpart_no"),
            "problem_no": row["problem_no"],
            "sort_order": row["sort_order"],
            "score": float(qwen.get("score", 0) or 0),
            "max_score": float(row["max_score"]),
            "correct": qwen.get("correct"),
            "confidence": float(qwen.get("confidence", 0) or 0),
            "recognized_work": recognized,
            "feedback": str(qwen.get("feedback") or "待教师查看识别结果"),
            "needs_review": needs_review,
            "review_reasons": review_reasons,
            "qwen": qwen,
            "qwen_input": qwen_input,
            "math_equivalence": equivalent,
            "answer_evidence": tool_use["answer_evidence"],
            "formula_reference": tool_use["formula_reference"],
            "tool_trace": tool_use["tool_trace"],
            "tool_router_version": tool_use["tool_router_version"],
            "handwriting_score": qwen.get("handwriting_score"),
            "evidence": {
                "source_problem_id": row["source_problem_id"],
                "standard_answer": standard_answer,
                "rubric": row["rubric"] or "",
                "source": json.loads(row["source_evidence_json"] or "{}"),
            },
            "risks": risks,
            "prompt_security": {
                "input": input_guard.trace(),
                "recognized_work": recognition_guard.trace(),
            },
        })

    # Teacher-confirmed page/region mappings survive a regrade and remain
    # visible as evidence even though the original upload is never altered.
    for item in results:
        key = (item["question_id"], item.get("subpart_no") or "", item["sort_order"])
        if key in regions:
            item["teacher_page_mapping"] = regions[key]

    needs_review = any(item["needs_review"] for item in results)
    quality_score = round(sum(item["score"] for item in results), 1)
    quality_max_score = round(sum(item["max_score"] for item in results), 1)
    with connection() as conn:
        score_meta = conn.execute(
            "SELECT score_policy,completion_points,total_score FROM assignments WHERE id=?",
            (submission["assignment_id"],),
        ).fetchone()
    score_policy = str(score_meta["score_policy"] or "legacy") if score_meta else "legacy"
    completion_score = float(score_meta["completion_points"] or 0) if score_meta else 0.0
    total_score = round(quality_score + completion_score, 1)
    max_score = float(score_meta["total_score"] or quality_max_score) if score_meta else quality_max_score
    hw_scores = [float(item["handwriting_score"]) for item in results
                 if item.get("handwriting_score") is not None]
    handwriting_score = round(sum(hw_scores) / len(hw_scores), 1) if hw_scores else None
    return {
        "submission_id": submission_id,
        "total_score": total_score,
        "max_score": max_score,
        "quality_score": quality_score,
        "quality_max_score": quality_max_score,
        "completion_score": completion_score,
        "score_policy": score_policy,
        "model_runtime": model_runtime(),
        "handwriting_score": handwriting_score,
        "needs_review": needs_review,
        "qwen_error": qwen_error,
        "results": results,
    }


async def run_grading_job(job_id: int) -> None:
    """Claim and persist one job; this is safe for web background tasks or worker.py."""
    with connection() as conn:
        job = conn.execute("SELECT * FROM grading_jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] != "queued":
            return
        changed = conn.execute("UPDATE grading_jobs SET status='running' WHERE id=? AND status='queued'", (job_id,)).rowcount
        if not changed:
            return
    try:
        result = await grade_submission(job["submission_id"])
        with connection() as conn:
            conn.execute("UPDATE grading_jobs SET status='completed', result_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), job_id))
            conn.execute(
                "UPDATE submissions SET status=?, score=?, feedback=?, needs_review=?, handwriting_score=?, completion_score=?, quality_score=?, quality_max_score=?, score_policy_version=? WHERE id=?",
                ("review_required" if result["needs_review"] else "graded", result["total_score"],
                 "AI 初评完成；请查看评分证据。", int(result["needs_review"]),
                 result.get("handwriting_score"), result.get("completion_score", 0),
                 result.get("quality_score"), result.get("quality_max_score"), result.get("score_policy", "legacy"),
                 job["submission_id"]),
            )
    except Exception as exc:
        with connection() as conn:
            conn.execute("UPDATE grading_jobs SET status='failed', result_json=? WHERE id=?", (json.dumps({"error": str(exc)}, ensure_ascii=False), job_id))
            conn.execute("UPDATE submissions SET status='review_required', needs_review=1, feedback=? WHERE id=?", ("初评任务失败，已转入教师复核。", job["submission_id"]))
