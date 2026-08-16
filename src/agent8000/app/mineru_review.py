"""Staged MinerU review records; only explicit approval updates 8014.

This module also handles the post-approval chain:

    8014 PUT success
        -> local review item marked approved
        -> audit log written
        -> session status recalculated
        -> local question cache updated if the problem was already cached
        -> section queued for re-sync so downstream assignments pick it up
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import settings
from .db import connection
from .knowledge_bridge import retrieve_problem, retrieve_section_problems
from .question_validation import validate_question, first_issue_message


def _same(value: Any, other: Any) -> bool:
    return str(value or "").strip() == str(other or "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_section(section_no: str) -> str:
    """Canonical '1.1' -> '1-1' for queue keys; accepts both forms."""
    return str(section_no or "").replace("．", ".").replace("。", ".").replace(" ", "").replace(".", "-")


def _summarize_session(session_id: int) -> dict[str, Any]:
    with connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM mineru_review_items WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        approved = conn.execute(
            "SELECT COUNT(*) FROM mineru_review_items WHERE session_id=? AND status='approved'", (session_id,)
        ).fetchone()[0]
        rejected = conn.execute(
            "SELECT COUNT(*) FROM mineru_review_items WHERE session_id=? AND status='rejected'", (session_id,)
        ).fetchone()[0]
        pending = total - approved - rejected
    return {
        "total": total,
        "approved": approved,
        "rejected": rejected,
        "pending": pending,
        "completion_rate": round((approved + rejected) / max(1, total), 2),
    }


def _update_session_status(session_id: int) -> dict[str, Any]:
    summary = _summarize_session(session_id)
    if summary["pending"] == 0:
        status = "completed"
    elif summary["approved"] == 0 and summary["rejected"] == 0:
        status = "pending"
    else:
        status = "partial"
    with connection() as conn:
        conn.execute(
            "UPDATE mineru_review_sessions SET status=? WHERE id=?",
            (status, session_id),
        )
    summary["status"] = status
    return summary


def _log_action(
    item_id: int,
    session_id: int,
    action: str,
    *,
    old_status: str,
    new_status: str,
    old_answer: str = "",
    new_answer: str = "",
    old_solution: str = "",
    new_solution: str = "",
    note: str = "",
    actor: str = "teacher",
) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO mineru_review_audit_log
               (item_id, session_id, action, old_status, new_status,
                old_answer, new_answer, old_solution, new_solution, note, actor)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, session_id, action, old_status, new_status,
             old_answer, new_answer, old_solution, new_solution, note, actor),
        )


def _enqueue_pending_sync(section_no: str, reason: str = "答案经 MinerU 审核后写回 8014") -> None:
    key = _norm_section(section_no)
    if not key:
        return
    with connection() as conn:
        conn.execute(
            """INSERT INTO mineru_pending_sync(section_no, source, reason, status)
               VALUES(?,?,?,?)
               ON CONFLICT(section_no, source) DO UPDATE SET
               status='pending', reason=excluded.reason, created_at=CURRENT_TIMESTAMP, synced_at=NULL""",
            (key, "mineru_review", reason, "pending"),
        )


def _update_local_cache(item: dict[str, Any], problem: dict[str, Any]) -> dict[str, Any]:
    """Update the local working cache if the problem was already imported."""
    source_id = str(item.get("source_problem_id") or "")
    section_no = str(item.get("section_no") or "")
    if not source_id:
        return {"updated": False, "reason": "no source_problem_id"}

    evidence = json.dumps(problem.get("evidence") or {}, ensure_ascii=False)
    values = (
        str(problem.get("content_text") or "").strip(),
        section_no,
        _difficulty_label(problem.get("difficulty")),
        str(problem.get("ptype") or "计算题"),
        str(problem.get("std_answer") or ""),
        str(problem.get("grading_steps") or problem.get("full_solution") or ""),
        evidence,
    )
    with connection() as conn:
        existing = conn.execute(
            "SELECT id FROM questions WHERE source_problem_id=?", (source_id,)
        ).fetchone()
        if not existing:
            return {"updated": False, "reason": "not_in_local_cache"}
        # An answer review approves the *answer*. The question stem must still
        # pass the same gate used at sync; a verified answer never promotes an
        # unreadable stem back to published.
        stem_report = validate_question(
            str(problem.get("content_text") or "").strip(),
            source_type="ocr",
            source_confidence=0.78,
            crop_image_path=(problem.get("evidence") or {}).get("crop_image_path"),
        )
        stem_status = "published" if stem_report["publish_allowed"] else "blocked"
        conn.execute(
            """UPDATE questions SET content=?, chapter=?, difficulty=?, question_type=?,
               answer=?, rubric=?, source_evidence_json=?, review_status=?
               WHERE id=?""",
            (*values, stem_status, existing["id"]),
        )
    return {"updated": True, "question_id": existing["id"], "stem_status": stem_status}


def _difficulty_label(value: object) -> str:
    try:
        number = int(value)
        return "基础" if number <= 1 else "提高" if number == 2 else "综合"
    except (TypeError, ValueError):
        text = str(value or "")
        return text if text in {"基础", "提高", "综合"} else "提高"


async def create_session(answer_book: dict) -> dict:
    if answer_book.get("document", {}).get("role") != "answer_book":
        raise ValueError("只接受答案书暂存 JSON")
    document_name = str(answer_book.get("document", {}).get("name") or "MinerU 答案书")
    candidates = [item for section in answer_book.get("sections", []) for item in section.get("items", [])]
    section_cache: dict[str, list[dict]] = {}
    for section in {str(item.get("section_no") or "") for item in candidates}:
        if section:
            section_cache[section] = (await retrieve_section_problems(section, limit=100)).get("items", [])
    with connection() as conn:
        session_id = conn.execute(
            "INSERT INTO mineru_review_sessions(document_name, status) VALUES(?,?)",
            (document_name, "pending"),
        ).lastrowid
        rows = []
        for item in candidates:
            section = str(item.get("section_no") or "")
            question = str(item.get("question_no") or "")
            sub = item.get("subquestion_no")
            target = next(
                (
                    p for p in section_cache.get(section, [])
                    if _same(p.get("problem_no"), question) and _same(p.get("sub_no"), sub)
                ),
                None,
            )
            score = float((item.get("quality") or {}).get("score", 0) or 0)
            confidence = round(score if target else 0.0, 2)
            evidence = {
                "item_id": item.get("item_id"),
                "source": item.get("source"),
                "mapping": "exact" if target else "unmatched",
                "question_crop_image_path": ((target or {}).get("evidence") or {}).get("crop_image_path"),
            }
            rows.append((
                session_id,
                str(target.get("source_problem_id")) if target else None,
                section,
                question,
                str(sub) if sub is not None else None,
                str(item.get("text") or ""),
                confidence,
                json.dumps(item.get("quality") or {}, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
            ))
        conn.executemany(
            """INSERT INTO mineru_review_items
               (session_id, source_problem_id, section_no, question_no, subquestion_no,
                candidate_text, confidence, quality_json, evidence_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    return {
        "session_id": session_id,
        "item_count": len(rows),
        "matched_count": sum(row[1] is not None for row in rows),
        "summary": _summarize_session(session_id),
    }


def get_session(session_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM mineru_review_sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise LookupError("审核批次不存在")
    item = dict(row)
    item["summary"] = _update_session_status(session_id)
    return item


def list_items(session_id: int) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT * FROM mineru_review_items
               WHERE session_id=?
               ORDER BY section_no, CAST(question_no AS INTEGER), subquestion_no""",
            (session_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["quality"] = json.loads(item.pop("quality_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        result.append(item)
    return result


async def approve_item(
    item_id: int,
    std_answer: str,
    full_solution: str,
    note: str = "",
    overwrite_verified: bool = False,
) -> dict:
    std_answer = std_answer.strip()
    if len(std_answer) < 1:
        raise ValueError("请填写可判等的标准答案；完整解答可单独填写。")
    if any(token in std_answer for token in ("锟", "鈫", "禲")):
        raise ValueError("标准答案含 OCR 乱码，不能确认写回。")

    with connection() as conn:
        item = conn.execute("SELECT * FROM mineru_review_items WHERE id=?", (item_id,)).fetchone()
    if not item:
        raise LookupError("审核项不存在")
    item = dict(item)
    if not item["source_problem_id"]:
        raise ValueError("未匹配到 8014 原题，不能写回；请先修正章节、题号或小问。")

    old_status = item["status"]

    # Read the authoritative record before writing. Existing verified answers are
    # protected by default, and the audit log stores the real upstream before-state.
    try:
        before_problem = await retrieve_problem(item["source_problem_id"])
    except Exception as exc:
        raise RuntimeError(f"8014 写回前读取原记录失败，本地状态未变更: {str(exc)[:180]}") from exc
    old_answer = str(before_problem.get("std_answer") or "")
    old_full_solution = str(before_problem.get("full_solution") or "")
    if before_problem.get("answer_status") == "verified" and not overwrite_verified:
        raise ValueError("8014 已有 verified 答案，默认禁止覆盖；请先人工核对并显式允许覆盖。")

    payload = {
        "std_answer": std_answer,
        "full_solution": full_solution.strip(),
        "answer_status": "verified",
    }

    # 1) Write back to 8014 first; do not touch local state until it succeeds.
    put_url = settings.evidence_api_url.rstrip("/") + f"/problems/{item['source_problem_id']}/answer"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.put(put_url, json=payload)
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise RuntimeError("8014 写回超时，本地状态未变更，请稍后重试。") from exc
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200] if exc.response else str(exc)
        raise RuntimeError(f"8014 写回失败 ({exc.response.status_code if exc.response else '未知'}): {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"8014 写回异常: {str(exc)[:180]}") from exc

    # 2) Local review item update + audit + session status.
    with connection() as conn:
        conn.execute(
            """UPDATE mineru_review_items
               SET status='approved', review_note=?, reviewed_at=?
               WHERE id=?""",
            (note, _now_iso(), item_id),
        )
    _log_action(
        item_id,
        item["session_id"],
        "approve",
        old_status=old_status,
        new_status="approved",
        old_answer=old_answer,
        new_answer=std_answer,
        old_solution=old_full_solution,
        new_solution=full_solution.strip(),
        note=note,
    )
    summary = _update_session_status(item["session_id"])

    # 3) Sync the now-verified problem into the local cache if already present,
    #    and queue its section for re-sync so upstream assignments stay consistent.
    cache_result: dict[str, Any] = {"updated": False}
    try:
        problem = await retrieve_problem(item["source_problem_id"])
        cache_result = _update_local_cache(dict(item), problem)
    except Exception as exc:
        cache_result = {"updated": False, "reason": f"retrieve_failed: {exc}"}

    _enqueue_pending_sync(item["section_no"])

    return {
        "ok": True,
        "item_id": item_id,
        "source_problem_id": item["source_problem_id"],
        "session_summary": summary,
        "cache_sync": cache_result,
    }


def reject_item(item_id: int, note: str = "") -> dict:
    with connection() as conn:
        item = conn.execute("SELECT * FROM mineru_review_items WHERE id=?", (item_id,)).fetchone()
        if not item:
            raise LookupError("审核项不存在")
        item = dict(item)
        old_status = item["status"]
        conn.execute(
            """UPDATE mineru_review_items
               SET status='rejected', review_note=?, reviewed_at=?
               WHERE id=?""",
            (note, _now_iso(), item_id),
        )
    _log_action(
        item_id,
        item["session_id"],
        "reject",
        old_status=old_status,
        new_status="rejected",
        old_answer=str(item.get("candidate_text") or ""),
        new_answer="",
        note=note,
    )
    summary = _update_session_status(item["session_id"])
    return {"ok": True, "item_id": item_id, "status": "rejected", "session_summary": summary}


def list_audit_log(session_id: int | None = None, item_id: int | None = None) -> list[dict]:
    clauses: list[str] = []
    args: list[Any] = []
    if session_id is not None:
        clauses.append("session_id=?")
        args.append(session_id)
    if item_id is not None:
        clauses.append("item_id=?")
        args.append(item_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM mineru_review_audit_log {where} ORDER BY created_at DESC",
            args,
        ).fetchall()
    return [dict(row) for row in rows]


def list_pending_sync(status: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM mineru_pending_sync", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC"
    with connection() as conn:
        return [dict(row) for row in conn.execute(sql, args)]


def mark_section_synced(section_no: str, source: str = "mineru_review") -> dict:
    key = _norm_section(section_no)
    with connection() as conn:
        changed = conn.execute(
            """UPDATE mineru_pending_sync
               SET status='synced', synced_at=?
               WHERE section_no=? AND source=?""",
            (_now_iso(), key, source),
        ).rowcount
    return {"ok": True, "section_no": key, "updated": bool(changed)}


def enqueue_pending_sync(section_no: str, reason: str = "手动触发章节同步") -> dict:
    key = _norm_section(section_no)
    if not key:
        raise ValueError("章节编号不能为空")
    _enqueue_pending_sync(key, reason)
    return {"ok": True, "section_no": key}
