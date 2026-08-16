"""Evidence-first grading pipeline for the teacher-facing agent."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .db import connection, normalize_question_type


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


def _load_images(file_path: str) -> list[str]:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        pages = [path]
    elif suffix == ".pdf":
        pages = _render_pdf_pages(path)
    else:
        raise ValueError("暂不支持 Word 自动识别，已转入教师复核，不会误判。")
    return [base64.b64encode(page.read_bytes()).decode("ascii") for page in pages]


async def grade_submission(submission_id: int) -> dict[str, Any]:
    """Retrieve assignment evidence, call Qwen, then cross-check each calc answer."""
    with connection() as conn:
        submission = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not submission:
            raise ValueError("提交不存在")
        rows = conn.execute(
            """SELECT aq.question_id, aq.sort_order, aq.score AS max_score, q.*
               FROM assignment_questions aq JOIN questions q ON q.id=aq.question_id
               WHERE aq.assignment_id=? ORDER BY aq.sort_order""",
            (submission["assignment_id"],),
        ).fetchall()

    questions = [dict(row) for row in rows]
    if not questions:
        raise ValueError("该作业未关联题目")
    problems = [
        {
            "problem_id": str(row["question_id"]),
            "problem_no": str(row["sort_order"]),
            "problem_text": row["content"],
            "std_answer": row["answer"] or "",
            "full_solution": row["rubric"] or "",
            "max_score": float(row["max_score"]),
        }
        for row in questions
    ]

    try:
        image_payload = _load_images(submission["file_path"])
        async with httpx.AsyncClient(timeout=1800) as client:
            response = await client.post(
                settings.qwen_grading_url,
                json={"images_base64": image_payload, "problems": problems},
            )
            response.raise_for_status()
            qwen_results = response.json().get("results", [])
    except Exception as exc:
        qwen_results = []
        qwen_error = str(exc)
    else:
        qwen_error = ""

    qwen_by_question = {str(item.get("problem_id")): item for item in qwen_results}
    results: list[dict[str, Any]] = []
    for row in questions:
        qid = str(row["question_id"])
        qwen = qwen_by_question.get(qid, {})
        standard_answer = row["answer"] or ""
        recognized = str(qwen.get("recognized_work") or "")
        is_proof = normalize_question_type(row["question_type"]) == "proof"
        equivalent = _math_equal(recognized, standard_answer) if not is_proof else {
            "available": False, "equal": None, "confidence": 0, "method": "证明题不作符号判等"
        }
        risks = list(qwen.get("risks") or [])
        review_reasons: list[str] = []
        if qwen_error:
            review_reasons.append("Qwen 识别失败")
        if not standard_answer:
            review_reasons.append("缺少标准答案")
        if row["question_type"] != "calc":
            review_reasons.append("证明/非计算题需教师复核")
        if float(qwen.get("confidence", 0) or 0) < 0.85:
            review_reasons.append("Qwen 置信度不足")
        if qwen.get("need_review", True):
            review_reasons.append("Qwen 标记为需复核")
        if equivalent["available"] and equivalent["confidence"] < 0.85:
            review_reasons.append("数学判等置信度不足")
        if equivalent["available"] and qwen.get("correct") is not None and bool(qwen.get("correct")) != bool(equivalent["equal"]):
            review_reasons.append("Qwen 与数学判等结论不一致")

        needs_review = bool(review_reasons)
        results.append({
            "question_id": row["question_id"],
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
            "math_equivalence": equivalent,
            "handwriting_score": qwen.get("handwriting_score"),
            "evidence": {
                "source_problem_id": row["source_problem_id"],
                "standard_answer": standard_answer,
                "rubric": row["rubric"] or "",
                "source": json.loads(row["source_evidence_json"] or "{}"),
            },
            "risks": risks,
        })

    needs_review = any(item["needs_review"] for item in results)
    total_score = round(sum(item["score"] for item in results), 1)
    hw_scores = [float(item["handwriting_score"]) for item in results
                 if item.get("handwriting_score") is not None]
    handwriting_score = round(sum(hw_scores) / len(hw_scores), 1) if hw_scores else None
    return {
        "submission_id": submission_id,
        "total_score": total_score,
        "max_score": round(sum(item["max_score"] for item in results), 1),
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
                "UPDATE submissions SET status=?, score=?, feedback=?, needs_review=?, handwriting_score=? WHERE id=?",
                ("review_required" if result["needs_review"] else "graded", result["total_score"],
                 "AI 初评完成；请查看评分证据。", int(result["needs_review"]),
                 result.get("handwriting_score"), job["submission_id"]),
            )
    except Exception as exc:
        with connection() as conn:
            conn.execute("UPDATE grading_jobs SET status='failed', result_json=? WHERE id=?", (json.dumps({"error": str(exc)}, ensure_ascii=False), job_id))
            conn.execute("UPDATE submissions SET status='review_required', needs_review=1, feedback=? WHERE id=?", ("初评任务失败，已转入教师复核。", job["submission_id"]))
