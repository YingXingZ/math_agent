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
import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import settings
from .evidence_client import client as evidence_client, url as evidence_url
from .db import connection
from .knowledge_bridge import retrieve_problem, retrieve_section_problems
from .question_validation import validate_question
from .answer_matcher import latex_noise_issues


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
        mapped, unique_targets = conn.execute(
            """SELECT COUNT(source_problem_id), COUNT(DISTINCT source_problem_id)
               FROM mineru_review_items WHERE session_id=?""",
            (session_id,),
        ).fetchone()
    return {
        "total": total,
        "approved": approved,
        "rejected": rejected,
        "pending": pending,
        # Number matching is only a provisional link to a question-bank record.
        "number_matched": int(mapped or 0),
        "unmatched": max(0, total - int(mapped or 0)),
        "unique_targets": int(unique_targets or 0),
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
    textbook_id = str(answer_book.get("document", {}).get("textbook_id") or "").strip() or None
    candidates = [item for section in answer_book.get("sections", []) for item in section.get("items", [])]
    section_cache: dict[str, list[dict]] = {}
    for section in {str(item.get("section_no") or "") for item in candidates}:
        if section:
            section_cache[section] = (await retrieve_section_problems(section, limit=1000, textbook_id=textbook_id)).get("items", [])
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
            quality = dict(item.get("quality") or {})
            issues = list(quality.get("issues") or [])
            noise_issues = latex_noise_issues(item.get("text") or "")
            for issue in noise_issues:
                if issue not in issues:
                    issues.append(issue)
            if noise_issues:
                quality["issues"] = issues
                quality["score"] = min(float(quality.get("score", 1.0) or 0), 0.3)
                quality["pass"] = False
            score = float(quality.get("score", 0) or 0)
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
                json.dumps(quality, ensure_ascii=False),
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
        duplicates = []
        if row and row["document_name"]:
            duplicates = [dict(x) for x in conn.execute(
                """SELECT id, created_at, status FROM mineru_review_sessions
                   WHERE document_name=? AND id<>? ORDER BY id DESC LIMIT 5""",
                (row["document_name"], session_id),
            ).fetchall()]
    if not row:
        raise LookupError("审核批次不存在")
    item = dict(row)
    item["summary"] = _update_session_status(session_id)
    item["same_name_sessions"] = duplicates
    return item


def list_items(
    session_id: int, *, limit: int | None = None, offset: int = 0,
    status: str | None = None,
) -> list[dict]:
    clauses = ["session_id=?"]
    args: list[Any] = [session_id]
    if status in {"pending", "approved", "rejected"}:
        clauses.append("status=?")
        args.append(status)
    sql = "SELECT * FROM mineru_review_items WHERE " + " AND ".join(clauses)
    sql += " ORDER BY section_no, CAST(question_no AS INTEGER), subquestion_no, id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args.extend([max(1, min(int(limit), 100)), max(0, int(offset))])
    with connection() as conn:
        rows = conn.execute(sql, args).fetchall()
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
    put_url = evidence_url("").rstrip("/") + f"/problems/{item['source_problem_id']}/answer"
    try:
        async with evidence_client(timeout=25) as client:
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


async def bulk_approve_items(session_id: int, item_ids: list[int]) -> dict[str, Any]:
    """Write back only uniquely matched, clean OCR candidates.

    A question number alone is not enough: answer books can repeat numbers across
    exercise sets. The source problem stem must occur in exactly one pending
    candidate for that target before the candidate can be imported.
    """
    if not item_ids or len(item_ids) > 50:
        raise ValueError("每次批量确认需选择 1–50 条")
    with connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM mineru_review_items WHERE session_id=? AND id IN ({','.join('?' for _ in item_ids)})",
            [session_id, *item_ids],
        ).fetchall()
        target_ids = list({str(row["source_problem_id"]) for row in rows if row["source_problem_id"]})
        pending_by_target: dict[str, list[str]] = {}
        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            pending_rows = conn.execute(
                f"SELECT source_problem_id,candidate_text FROM mineru_review_items "
                f"WHERE session_id=? AND status='pending' AND source_problem_id IN ({placeholders})",
                [session_id, *target_ids],
            ).fetchall()
            for pending_row in pending_rows:
                pending_by_target.setdefault(str(pending_row["source_problem_id"]), []).append(
                    str(pending_row["candidate_text"] or "")
                )
    by_id = {int(row["id"]): dict(row) for row in rows}
    semaphore = asyncio.Semaphore(4)

    def normalized_match_text(value: str) -> str:
        import unicodedata
        normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
        return "".join(
            char for char in normalized
            if char.isalnum() or "\u3400" <= char <= "\u9fff"
        )

    def stem_matches(source_text: str, candidate_text: str) -> bool:
        normalized_stem = normalized_match_text(source_text)
        normalized_candidate = normalized_match_text(candidate_text)
        if len(normalized_stem) >= 12 and normalized_stem in normalized_candidate:
            return True
        # Formula OCR often changes TeX spacing/commands while preserving the
        # Chinese prompt. Use a conservative fuzzy check only for long prompts.
        import difflib
        import unicodedata
        source_cjk = "".join(
            char for char in unicodedata.normalize("NFKC", str(source_text or ""))
            if "\u3400" <= char <= "\u9fff"
        )
        candidate_cjk = "".join(
            char for char in unicodedata.normalize("NFKC", str(candidate_text or ""))
            if "\u3400" <= char <= "\u9fff"
        )
        if len(source_cjk) < 20:
            return False
        candidate_prefix = candidate_cjk[:len(source_cjk) + 80]
        for size in range(max(20, len(source_cjk) - 5), len(source_cjk) + 6):
            for offset in range(max(1, len(candidate_prefix) - size + 1)):
                if difflib.SequenceMatcher(
                    None, source_cjk, candidate_prefix[offset:offset + size], autojunk=False
                ).ratio() >= 0.90:
                    return True
        return False

    async def one(item_id: int) -> dict[str, Any]:
        item = by_id.get(item_id)
        if not item:
            return {"item_id": item_id, "ok": False, "error": "条目不存在或不属于此批次"}
        quality = json.loads(item.get("quality_json") or "{}")
        text = str(item.get("candidate_text") or "").strip()
        if item.get("status") != "pending":
            return {"item_id": item_id, "ok": False, "error": "条目已处理"}
        if not item.get("source_problem_id"):
            return {"item_id": item_id, "ok": False, "error": "题号未精确匹配"}
        if not quality.get("pass") or float(quality.get("score") or 0) < 0.9:
            return {"item_id": item_id, "ok": False, "error": "未通过 OCR 质量门槛"}
        if latex_noise_issues(text):
            return {"item_id": item_id, "ok": False, "error": "检测到重复公式命令，疑似 OCR 噪声"}
        if not text or len(text) > 14000:
            return {"item_id": item_id, "ok": False, "error": "答案为空或过长，需人工核对"}
        async with semaphore:
            try:
                problem = await retrieve_problem(str(item["source_problem_id"]))
                if not _same(problem.get("problem_no"), item.get("question_no")):
                    return {"item_id": item_id, "ok": False, "error": "题号与题库记录不一致"}
                source_sub = str(problem.get("sub_no") or "").strip()
                candidate_sub = str(item.get("subquestion_no") or "").strip()
                if source_sub != ("" if candidate_sub in {"", "0"} else candidate_sub):
                    return {"item_id": item_id, "ok": False, "error": "小问编号与题库记录不一致"}
                source_stem_text = str(problem.get("content_text") or "")
                if not stem_matches(source_stem_text, text):
                    return {"item_id": item_id, "ok": False, "error": "答案文本未包含原题题干，不能自动确认"}
                target = str(item["source_problem_id"])
                matching_candidates = sum(
                    1 for candidate in pending_by_target.get(target, [])
                    if stem_matches(source_stem_text, candidate)
                )
                if matching_candidates != 1:
                    return {"item_id": item_id, "ok": False, "error": "同一题有多个题干匹配候选，需先处理冲突"}
                result = await approve_item(item_id, text, "", "批量确认：题号精确匹配且通过 OCR 质量门槛")
                return {"item_id": item_id, "ok": True, "result": result}
            except Exception as exc:
                return {"item_id": item_id, "ok": False, "error": str(exc)[:200]}

    results = await asyncio.gather(*(one(int(item_id)) for item_id in dict.fromkeys(item_ids)))
    return {
        "session_id": session_id,
        "approved": sum(bool(item["ok"]) for item in results),
        "failed": sum(not item["ok"] for item in results),
        "results": results,
        "summary": _update_session_status(session_id),
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
