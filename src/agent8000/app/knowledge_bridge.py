"""Read-only bridge to the existing 8014 evidence workbench.

The teacher-facing agent never duplicates or overwrites its source material.
It asks the workbench for textbook questions and answer-document coverage, then
uses the returned evidence in generation and grading tasks.
"""
from typing import Any
from urllib.parse import quote
import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
import httpx

from .config import settings


async def evidence_status() -> dict[str, Any]:
    base = settings.evidence_api_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            textbooks, documents, problems = await _gather(client, base)
        problem_rows = problems.get("items", problems) if isinstance(problems, dict) else problems
        problem_count = (problems.get("total") if isinstance(problems, dict) else None) or len(problem_rows or [])
        return {
            "connected": True,
            "source": base,
            "textbook_count": len(textbooks or []),
            "answer_document_count": len(documents or []),
            "problem_count": problem_count,
            "message": "已连接 8014 教材与答案证据库；生成与批改会按需检索，不复制原始资料。",
        }
    except Exception as exc:
        return {
            "connected": False, "source": base, "textbook_count": 0,
            "answer_document_count": 0, "problem_count": 0,
            "message": "8014 资料库暂不可访问：" + str(exc)[:160],
        }


async def list_evidence_sections() -> list[dict[str, str]]:
    """Return selectable textbook sections from the 8014 evidence source."""
    base = settings.evidence_api_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15) as client:
        textbooks_response = await client.get(base + "/textbooks")
        textbooks_response.raise_for_status()
        textbooks = textbooks_response.json() or []
        sections: list[dict[str, str]] = []
        for textbook in textbooks:
            textbook_id = textbook.get("id")
            if not textbook_id:
                continue
            response = await client.get(base + f"/textbooks/{quote(str(textbook_id), safe='')}/sections")
            response.raise_for_status()
            for section in response.json() or []:
                section_no = str(section.get("section_no") or "").strip()
                if section_no:
                    sections.append({"section_no": section_no, "title": str(section.get("title") or "")})
    return sorted(sections, key=lambda item: tuple(int(part) if part.isdigit() else part for part in item["section_no"].split(".")))


async def _gather(client: httpx.AsyncClient, base: str):
    import asyncio
    responses = await asyncio.gather(
        client.get(base + "/textbooks"),
        client.get(base + "/answer-documents"),
        client.get(base + "/problems?size=1"),
    )
    for response in responses:
        response.raise_for_status()
    return tuple(response.json() for response in responses)


async def retrieve_section_problems(section_no: str, limit: int = 30) -> dict[str, Any]:
    """Retrieve assignment-ready evidence without mutating the source library."""
    base = settings.evidence_api_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(base + "/problems", params={"section_no": section_no, "page": 1, "size": limit})
        response.raise_for_status()
        payload = response.json()
    items = payload.get("items", [])
    return {"section_no": section_no, "total": payload.get("total", len(items)), "items": [
        {"source_problem_id": item.get("id"), "problem_no": item.get("problem_no"),
         "sub_no": item.get("sub_no"), "difficulty": item.get("difficulty"),
         "ptype": item.get("ptype"), "content_text": item.get("content_text"),
         "std_answer": item.get("std_answer"), "full_solution": item.get("full_solution"),
         "grading_steps": item.get("grading_steps"), "answer_status": item.get("answer_status"),
         "evidence": {"source": "8014", "section_no": item.get("section_no"),
                      "crop_image_path": item.get("crop_image_path")}}
        for item in items
    ]}


async def retrieve_problem(problem_id: str) -> dict[str, Any]:
    """Fetch a single problem's latest state from 8014 after it has been updated."""
    base = settings.evidence_api_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(base + f"/problems/{problem_id}")
        response.raise_for_status()
        item = response.json()
    return {
        "source_problem_id": item.get("id"),
        "problem_no": item.get("problem_no"),
        "sub_no": item.get("sub_no"),
        "difficulty": item.get("difficulty"),
        "ptype": item.get("ptype"),
        "content_text": item.get("content_text"),
        "std_answer": item.get("std_answer"),
        "full_solution": item.get("full_solution"),
        "grading_steps": item.get("grading_steps"),
        "answer_status": item.get("answer_status") or ("verified" if item.get("std_answer") else "unverified"),
        "evidence": {"source": "8014", "section_no": item.get("section_no"),
                     "crop_image_path": item.get("crop_image_path")},
    }


def _candidate_agreement(first: str, second: str) -> dict[str, Any]:
    """Independent symbolic comparison for two model-produced final answers."""
    try:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)
        from grading_engine import expr_equal
        equal, confidence, method = expr_equal(first, second)
        return {"equal": equal, "confidence": confidence, "method": method}
    except Exception as exc:
        return {"equal": False, "confidence": 0, "method": f"判等不可用：{str(exc)[:80]}"}


def _pending_reason(agreement: dict[str, Any], confidence: float, vision: dict[str, Any]) -> str:
    """Human-readable reason a VLM candidate needs teacher review."""
    if vision.get("risks"):
        return "VLM 识别存在风险标记：" + ", ".join(str(r) for r in vision["risks"])[:120]
    if not agreement.get("equal"):
        return f"双模型答案不一致（{agreement.get('method', '')}），置信度 {confidence:.2f}"
    return f"双模型一致性未达发布阈值，置信度 {confidence:.2f} < 0.80"


def _norm_text(value: str) -> str:
    """Loose normalization for fuzzy answer comparison (spaces/case insensitive)."""
    return re.sub(r"\s+", "", str(value or "").lower())


def _verify_standard_answer(stem: str, vision_std: str, qwen_base: str,
                            section_no: str, problem_no: str,
                            image_bytes: bytes | None = None) -> dict[str, Any]:
    """Secondary verification of an imported standard answer (accuracy & consistency).

    Two independent strategies, chosen by what is available so the gate never relaxes
    just because one channel is missing:

      * stem readable  -> an independent text-only ``/solve`` compared symbolically
        against the answer-book answer.  This catches OCR glitches in the answer book.
      * stem missing   -> a *second* ``/solve-from-image`` read of the same crop; the two
        VLM answers must agree (catches single-pass VLM misreads of the answer).

    Returns ``{verified, method, agreement, risks, secondary}``.
    """
    vision_std = str(vision_std or "").strip()
    risks: list[str] = []
    if not vision_std:
        return {"verified": False, "method": "无标准答案可校验", "agreement": {},
                "risks": ["标准答案为空，无法校验"], "secondary": None}

    if stem:
        try:
            with httpx.Client(timeout=300) as client:
                r = client.post(qwen_base + "/solve", json={
                    "problem_text": stem, "section_no": section_no, "problem_no": problem_no})
                if r.status_code == 200:
                    ind = r.json()
                    ind_std = str(ind.get("std_answer") or "").strip()
                    if ind_std:
                        agreement = _candidate_agreement(vision_std, ind_std)
                        verified = bool(agreement.get("equal")) and float(agreement.get("confidence") or 0) >= 0.6
                        if not verified:
                            risks.append("标准答案与独立求解结论不一致（二次校验未通过）")
                        return {"verified": verified, "method": "独立文本求解+符号判等",
                                "agreement": agreement, "risks": risks,
                                "secondary": {"source": "solve", "std_answer": ind_std}}
        except Exception:
            pass  # fall through to a second VLM read

    # stem missing or independent solve unavailable -> second VLM read on the same crop
    if image_bytes:
        try:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            with httpx.Client(timeout=300) as client:
                r = client.post(qwen_base + "/solve-from-image", json={
                    "image_base64": b64, "section_no": section_no, "problem_no": problem_no})
                if r.status_code == 200:
                    rec = r.json()
                    rec_std = str(rec.get("std_answer") or "").strip()
                    if rec_std:
                        agreement = _candidate_agreement(vision_std, rec_std)
                        fuzzy = _norm_text(vision_std) == _norm_text(rec_std)
                        verified = bool(agreement.get("equal")) or fuzzy
                        if not verified:
                            risks.append("两次视觉识别的标准答案不一致（二次校验未通过）")
                        return {"verified": verified, "method": "同图二次视觉识别比对",
                                "agreement": agreement, "risks": risks,
                                "secondary": {"source": "solve-from-image", "std_answer": rec_std}}
        except Exception:
            pass
    return {"verified": False, "method": "无可用二次校验通道", "agreement": {},
            "risks": ["二次校验通道不可用，已转人工复核"], "secondary": None}


def _pix2text_python_path() -> str:
    configured = os.environ.get(
        "PIX2TEXT_PYTHON",
        r"C:\Users\YXZ\.workbuddy\binaries\python\envs\ocr\Scripts\python.exe",
    )
    return configured if os.path.isfile(configured) else sys.executable


async def rescue_formula_from_crop(crop_image_path: str, base: str | None = None) -> dict[str, Any]:
    """Run an isolated Pix2Text worker on the original crop and return a candidate.

    The worker is review-only. Even if it returns text, the caller must run the
    same ``validate_question`` gate and require teacher confirmation before any
    publication state changes.
    """
    base = base or settings.evidence_api_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            image_response = await client.get(base + "/images/" + quote(str(crop_image_path), safe="/"))
            image_response.raise_for_status()
            image_bytes = image_response.content
    except Exception as exc:
        return {"status": "unavailable", "reason": f"题图下载失败：{str(exc)[:120]}"}

    work_dir = Path(tempfile.mkdtemp(prefix="pix2text_formula_"))
    crop_file = work_dir / "crop.png"
    output_file = work_dir / "result.json"
    crop_file.write_bytes(image_bytes)
    worker = Path(__file__).with_name("pix2text_formula_worker.py")
    local_python = _pix2text_python_path()
    if not worker.is_file():
        return {"status": "unavailable", "reason": "Pix2Text worker 缺失"}
    try:
        proc = await asyncio.create_subprocess_exec(
            local_python, str(worker), "--image", str(crop_file), "--output", str(output_file),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if not output_file.is_file():
            return {"status": "unavailable", "reason": (stderr or b"").decode("utf-8", "ignore")[-300:]}
        result = json.loads(output_file.read_text(encoding="utf-8"))
        result["crop_image_path"] = crop_image_path
        return result
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"Pix2Text 执行失败：{str(exc)[:160]}"}


async def build_image_solve_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Get Qwen to read a source crop, then verify with an independent text solve.

    Handles the VLM server's structured ``partial`` result: when a crop is only
    partially readable (e.g. the stem is missing but the answer present), the
    candidate is parked as ``pending_review`` with a Pix2Text OCR fallback used to
    recover the stem, instead of being dropped as ``unavailable``.
    """
    crop = (item.get("evidence") or {}).get("crop_image_path")
    if not crop:
        return {"status": "unavailable", "reason": "没有原题截图"}
    base = settings.evidence_api_url.rstrip("/")
    qwen_base = settings.qwen_grading_url.rsplit("/", 1)[0]
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            image_response = await client.get(base + "/images/" + quote(str(crop), safe="/"))
            image_response.raise_for_status()
            image_bytes = image_response.content
            vision_response = await client.post(qwen_base + "/solve-from-image", json={
                "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                "section_no": item["evidence"].get("section_no", ""),
                "problem_no": str(item.get("problem_no") or ""),
            })
            vision_response.raise_for_status()
            vision = vision_response.json()
            # The independent text solve needs a stem. When the VLM could not read
            # it (a partial result with ``missing: ["problem_text"]``), skip the
            # cross-check rather than 422-ing the whole candidate.
            independent = None
            stem = str(vision.get("problem_text") or "").strip()
            if stem:
                try:
                    text_response = await client.post(qwen_base + "/solve", json={
                        "problem_text": vision["problem_text"],
                        "section_no": item["evidence"].get("section_no", ""),
                        "problem_no": str(item.get("problem_no") or ""),
                    })
                    text_response.raise_for_status()
                    independent = text_response.json()
                except Exception:
                    independent = None
    except Exception as exc:
        return {"status": "unavailable", "reason": f"题图 AI 兜底暂不可用：{str(exc)[:160]}"}

    # If the VLM did not read the stem, try Pix2Text OCR as a fallback.
    ocr_stem = None
    stem_missing = "problem_text" in (vision.get("missing") or []) or not stem
    if stem_missing:
        ocr = await rescue_formula_from_crop(crop, base)
        if ocr.get("status") == "review_candidate" and str(ocr.get("candidate_text") or "").strip():
            ocr_stem = str(ocr["candidate_text"]).strip()

    candidate_text = stem or ocr_stem or ""
    # A recognised stem is only worth parking for teacher review if it actually
    # contains a readable question. Otherwise there is nothing to review.
    if not candidate_text or len(candidate_text) < 6:
        return {
            "status": "unavailable",
            "reason": "VLM 与 Pix2Text 均未能从原题图识别出可读题干",
            "problem_text": candidate_text,
            "ptype": vision.get("ptype", "calc"),
            "std_answer": vision.get("std_answer", ""),
            "full_solution": vision.get("full_solution", ""),
            "confidence": float(vision.get("confidence", 0) or 0),
            "vision": vision,
            "independent": independent,
        }

    if independent is not None:
        agreement = _candidate_agreement(str(vision.get("std_answer", "")), str(independent.get("std_answer", "")))
        confidence = min(float(vision.get("confidence", 0) or 0), float(independent.get("confidence", 0) or 0), float(agreement["confidence"] or 0))
        eligible = agreement["equal"] and confidence >= 0.8 and not (vision.get("risks") or [])
    else:
        # No independent cross-check is possible (stem came from the OCR fallback
        # or the VLM left it unreadable). Treat as a review candidate; never
        # auto-publish without teacher confirmation.
        agreement = {"equal": False, "confidence": 0.0, "method": "无独立文本解交叉校验"}
        confidence = float(vision.get("confidence", 0) or 0)
        eligible = False

    # Secondary verification of the standard answer (Route 2 准确性与一致性保障).
    # Even when the dual-model gate passes, the imported answer must survive an
    # independent second check before it may be auto-published.
    verification = _verify_standard_answer(
        stem, str(vision.get("std_answer", "")), qwen_base,
        item["evidence"].get("section_no", ""), str(item.get("problem_no") or ""),
        image_bytes)
    v_risks = verification.get("risks") or []
    if eligible and not verification["verified"]:
        eligible = False
        reason = "标准答案二次校验未通过：" + (verification.get("method") or "")
    if eligible:
        status = "eligible"
        reason = ""
    else:
        # The dual-model gate did not pass (or could not run), but the recognised
        # stem is still a useful candidate: keep it as "pending_review" for the
        # teacher instead of silently dropping it. The teacher confirms (or edits)
        # it, and only then is the text written back to the 8014 source — closing
        # the OCR completion loop without relaxing the publication gate.
        status = "pending_review"
        reason = _pending_reason(agreement, confidence, vision)
        if v_risks:
            reason = (reason + "；" if reason else "") + "；".join(v_risks)
    return {
        "status": status,
        "reason": reason,
        "problem_text": candidate_text,
        "ptype": vision.get("ptype", "calc"),
        "std_answer": vision.get("std_answer", ""),
        "full_solution": vision.get("full_solution", ""),
        "confidence": confidence,
        "agreement": agreement,
        "verification": verification,
        "vision": vision,
        "independent": independent,
        "stem_source": "ocr" if ocr_stem else ("vlm" if stem else "none"),
    }
