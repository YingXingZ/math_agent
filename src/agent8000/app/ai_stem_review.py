"""Pending VLM-recognised stems awaiting teacher review.

When ``build_image_solve_candidate`` reads a source crop, the strict dual-model
confidence gate may not pass.  Previously such a recognition was discarded.  Now
the recognised stem is parked here as a ``pending_review`` candidate so a teacher
can confirm (or edit) it.  Approval writes the validated text back to the
authoritative 8014 source and into the local working cache, closing the
OCR-completion loop without relaxing the publication gate.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import settings
from .evidence_client import client as evidence_client, url as evidence_url
from .db import connection, normalize_question_type


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _difficulty_label(value: object) -> str:
    """Normalise 8014's numeric difficulty (1/2/3) to the local paper levels."""
    try:
        number = int(value)
        return "基础" if number <= 1 else "提高" if number == 2 else "综合"
    except (TypeError, ValueError):
        text = str(value or "")
        return text if text in {"基础", "提高", "综合"} else "提高"


def store_pending_candidate(item: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Upsert a pending VLM stem candidate keyed by ``source_problem_id``.

    An already-approved candidate is never overwritten by a later re-sync, so the
    teacher's decision survives.  Returns ``{"stored": False, "reason": "..."}``
    when nothing was written.
    """
    source_id = str(item.get("source_problem_id") or "")
    if not source_id:
        return {"stored": False, "reason": "no source_problem_id"}
    evidence = item.get("evidence") or {}
    candidate_text = str(candidate.get("problem_text") or "").strip()
    if len(candidate_text) < 6:
        return {"stored": False, "reason": "candidate_text_too_short"}
    payload_evidence = {
        "source": "8014",
        "section_no": evidence.get("section_no") or item.get("section_no") or "",
        "crop_image_path": evidence.get("crop_image_path"),
        "vision": candidate.get("vision"),
        "independent": candidate.get("independent"),
    }
    with connection() as conn:
        existing = conn.execute(
            "SELECT id, status FROM ai_stem_candidates WHERE source_problem_id=?", (source_id,)
        ).fetchone()
        if existing and existing["status"] == "approved":
            return {"stored": False, "reason": "already_approved", "id": existing["id"]}
        values = (
            source_id,
            str(payload_evidence["section_no"]),
            str(item.get("problem_no") or ""),
            str(item.get("sub_no") or ""),
            candidate_text,
            str(candidate.get("ptype") or "calc"),
            str(candidate.get("std_answer") or "").strip(),
            str(candidate.get("full_solution") or "").strip(),
            str(item.get("difficulty") or ""),
            float(candidate.get("confidence") or 0),
            json.dumps(candidate.get("agreement") or {}, ensure_ascii=False),
            json.dumps(payload_evidence, ensure_ascii=False),
            "pending",
        )
        if existing:
            conn.execute(
                """UPDATE ai_stem_candidates SET section_no=?, problem_no=?, sub_no=?,
                       candidate_text=?, ptype=?, std_answer=?, full_solution=?, difficulty=?,
                       confidence=?, agreement_json=?, evidence_json=?, status='pending',
                       reviewed_at=NULL, review_note='', approved_content=NULL WHERE id=?""",
                (*values[1:12], existing["id"]),
            )
            cid = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO ai_stem_candidates
                   (source_problem_id, section_no, problem_no, sub_no, candidate_text, ptype,
                    std_answer, full_solution, difficulty, confidence, agreement_json, evidence_json, status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            cid = cur.lastrowid
    return {"stored": True, "id": cid, "source_problem_id": source_id}


def list_candidates(status: str | None = "pending", section_no: str | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if status:
        clauses.append("status=?")
        args.append(status)
    if section_no:
        clauses.append("section_no=?")
        args.append(section_no)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM ai_stem_candidates {where} ORDER BY section_no, CAST(problem_no AS INTEGER)",
            args,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record["agreement"] = json.loads(record.pop("agreement_json") or "{}")
        record["evidence"] = json.loads(record.pop("evidence_json") or "{}")
        out.append(record)
    return out


def _load_candidate(candidate_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM ai_stem_candidates WHERE id=?", (candidate_id,)).fetchone()
    if not row:
        raise LookupError("候选不存在")
    return dict(row)


def _upsert_local_cache(
    source_id: str,
    candidate: dict[str, Any],
    content: str,
    answer: str,
    solution: str,
    difficulty: str,
) -> dict[str, Any]:
    """Make the approved stem immediately usable in the local working cache."""
    section_no = candidate["section_no"]
    evidence_payload = {}
    try:
        evidence_payload = json.loads(candidate.get("evidence_json") or "{}")
    except Exception:
        pass
    crop = evidence_payload.get("crop_image_path")
    evidence = json.dumps(
        {"source": "8014", "section_no": section_no, "crop_image_path": crop},
        ensure_ascii=False,
    )
    with connection() as conn:
        existing = conn.execute("SELECT id FROM questions WHERE source_problem_id=?", (source_id,)).fetchone()
        qtype = normalize_question_type(candidate.get("ptype"))
        if existing:
            conn.execute(
                """UPDATE questions SET content=?, chapter=?, difficulty=?, question_type=?,
                       answer=?, rubric=?, source_evidence_json=?, review_status='published' WHERE id=?""",
                (content, section_no, difficulty, qtype,
                 answer, solution, evidence, existing["id"]),
            )
            return {"updated": True, "question_id": existing["id"]}
        cur = conn.execute(
            """INSERT INTO questions
               (content, chapter, difficulty, question_type, answer, rubric, source_evidence_json,
                source_problem_id, source_problem_no)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (content, section_no, difficulty, qtype,
             answer, solution, evidence, source_id, candidate.get("problem_no")),
        )
        return {"inserted": True, "question_id": cur.lastrowid}


async def approve_candidate(
    candidate_id: int,
    content_text: str | None = None,
    std_answer: str | None = None,
    full_solution: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    candidate = _load_candidate(candidate_id)
    if candidate["status"] == "approved":
        raise ValueError("该候选已确认，不能重复确认。")
    source_id = str(candidate["source_problem_id"])
    final_content = (content_text or candidate["candidate_text"] or "").strip()
    if len(final_content) < 6:
        raise ValueError("题干内容过短，无法确认写回。")
    if "�" in final_content:
        raise ValueError("题干含无法解码字符，不能确认写回。")
    final_answer = (std_answer or candidate["std_answer"] or "").strip()
    final_solution = (full_solution or candidate["full_solution"] or "").strip()
    difficulty = _difficulty_label(candidate["difficulty"])

    # 1) Write back to 8014 (authoritative source) FIRST.  Do not touch local
    #    state until the upstream write succeeds, mirroring the MinerU pattern.
    put_url = evidence_url("").rstrip("/") + f"/problems/{source_id}/content"
    try:
        async with evidence_client(timeout=25) as client:
            response = await client.put(put_url, json={"content_text": final_content})
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise RuntimeError("8014 写回超时，本地状态未变更，请稍后重试。") from exc
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200] if exc.response else str(exc)
        raise RuntimeError(
            f"8014 写回失败 ({exc.response.status_code if exc.response else '未知'}): {detail}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"8014 写回异常: {str(exc)[:180]}") from exc

    # 2) Make it usable in the local working cache.
    cache_result = _upsert_local_cache(source_id, candidate, final_content, final_answer, final_solution, difficulty)

    # 3) Mark the candidate approved so re-syncs never re-park it.
    with connection() as conn:
        conn.execute(
            """UPDATE ai_stem_candidates SET status='approved', review_note=?, reviewed_at=?, approved_content=?
               WHERE id=?""",
            (note, _now_iso(), final_content, candidate_id),
        )
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "source_problem_id": source_id,
        "difficulty": difficulty,
        "cache": cache_result,
    }


def reject_candidate(candidate_id: int, note: str = "") -> dict[str, Any]:
    candidate = _load_candidate(candidate_id)
    with connection() as conn:
        conn.execute(
            "UPDATE ai_stem_candidates SET status='rejected', review_note=?, reviewed_at=? WHERE id=?",
            (note, _now_iso(), candidate_id),
        )
    return {"ok": True, "candidate_id": candidate_id, "status": "rejected"}
