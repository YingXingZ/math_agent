"""RQ callable functions. Keep this module importable by a standalone worker."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from .db import connection, init_db
from .grading_pipeline import run_grading_job


def grade_submission_task(job_id: int) -> None:
    init_db()
    with connection() as conn:
        row = conn.execute("SELECT status,queue_attempts FROM grading_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return
        if row["status"] == "completed":
            return
        # RQ retries re-enter after the pipeline recorded failed. Re-arm the
        # durable DB state before the atomic queued -> running claim.
        if row["status"] == "failed":
            conn.execute("UPDATE grading_jobs SET status='queued',result_json=NULL WHERE id=?", (job_id,))
        conn.execute(
            "UPDATE grading_jobs SET queue_attempts=?,last_started_at=?,last_error=NULL WHERE id=?",
            (int(row["queue_attempts"] or 0) + 1, datetime.now(timezone.utc).isoformat(), job_id),
        )
    asyncio.run(run_grading_job(job_id))
    with connection() as conn:
        row = conn.execute("SELECT status,result_json FROM grading_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "failed":
            return
        try:
            detail = json.loads(row["result_json"] or "{}").get("error", "批改任务失败")
        except ValueError:
            detail = "批改任务失败"
        conn.execute("UPDATE grading_jobs SET last_error=? WHERE id=?", (str(detail)[:500], job_id))
    # Raising here makes RQ schedule the configured retry. Qwen returning an
    # uncertain candidate is *completed* and therefore does not retry.
    raise RuntimeError(f"grading job {job_id} failed: {detail}")



def rebuild_question_candidates_task(batch_id: int) -> None:
    """Reliable RQ worker entry for answer-bank source-image reconstructions."""
    init_db()
    from .main import _process_repair_batch
    try:
        asyncio.run(_process_repair_batch(batch_id))
    except Exception as exc:
        with connection() as conn:
            conn.execute(
                "UPDATE question_repair_batches SET status='ready',notes=? WHERE id=?",
                (f"Qwen 批量重建任务异常：{str(exc)[:220]}", batch_id),
            )
            conn.execute(
                "INSERT INTO question_repair_batch_audit(batch_id,event_type,payload_json) VALUES(?,?,?)",
                (batch_id, "qwen_rebuild_failed", json.dumps({"detail": str(exc)[:500]}, ensure_ascii=False)),
            )
        raise


def verify_provisional_question_bank_task(batch_id: int = 1) -> dict:
    """Second-pass, evidence-preserving verification for provisional bank items."""
    import sqlite3, urllib.request, time
    from pathlib import Path
    from .config import settings
    home_db = settings.database_path
    wb_db = "/home/zhangzhuohan/math-agent/src/workbench8014/api.workbench.db"
    conn = sqlite3.connect(home_db)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS question_auto_verifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
        baseline_json TEXT NOT NULL, second_json TEXT NOT NULL DEFAULT '{}',
        decision TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(batch_id,candidate_id))""")
    rows = conn.execute("""SELECT i.candidate_id,i.normalized_content,i.normalized_answer,i.normalized_solution,
                                  i.snapshot_json
                           FROM question_repair_batch_items i
                           WHERE i.batch_id=? AND i.publish_status='provisional'""", (batch_id,)).fetchall()
    wb = sqlite3.connect(wb_db); wb.row_factory = sqlite3.Row
    verified = kept = failed = 0
    for row in rows:
        candidate_id = int(row["candidate_id"])
        current = wb.execute("""SELECT c.ai_review_json,c.ai_review_status,p.ptype
                                FROM answer_import_candidates c JOIN problems p ON p.id=c.problem_id
                                WHERE c.id=?""", (candidate_id,)).fetchone()
        if not current:
            failed += 1; continue
        baseline = current["ai_review_json"] or "{}"
        decision, reason, second = "provisional", "", "{}"
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:8014/api/answer-import-candidates/{candidate_id}/ai-review",
                data=b"", headers={"Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=620) as response:
                response.read()
            after = wb.execute("SELECT ai_review_json,ai_review_status,p.ptype FROM answer_import_candidates c JOIN problems p ON p.id=c.problem_id WHERE c.id=?", (candidate_id,)).fetchone()
            second = after["ai_review_json"] or "{}"
            a, b = json.loads(baseline), json.loads(second)
            answer1 = "".join(str(a.get("std_answer") or "").split())
            answer2 = "".join(str(b.get("std_answer") or "").split())
            confidence_ok = min(float(a.get("confidence") or 0), float(b.get("confidence") or 0)) >= 0.90
            risks = list(a.get("risks") or []) + list(b.get("risks") or [])
            structural_ok = len(str(row["normalized_content"] or "").strip()) >= 12 and bool(answer1) and bool(answer2)
            if after["ptype"] != "calc":
                reason = "证明题保留教师核验"
            elif not structural_ok:
                reason = "题干或标准答案结构不完整"
            elif risks:
                reason = "模型标记风险"
            elif not confidence_ok:
                reason = "两次识别置信度不足"
            elif answer1 != answer2:
                reason = "两次识别答案不一致"
            else:
                payload = {"action":"approved","content_text":row["normalized_content"],
                           "std_answer":str(b.get("std_answer") or ""),
                           "full_solution":str(b.get("full_solution") or ""),
                           "ptype":"calc","note":"批量二次 Qwen 验证：两次结果一致，自动升级"}
                req2 = urllib.request.Request(
                    f"http://127.0.0.1:8014/api/answer-import-candidates/{candidate_id}/review",
                    data=json.dumps(payload,ensure_ascii=False).encode(),headers={"Content-Type":"application/json"},method="POST")
                with urllib.request.urlopen(req2,timeout=60) as response:
                    response.read()
                conn.execute("UPDATE question_repair_batch_items SET publish_status='verified_auto' WHERE batch_id=? AND candidate_id=?", (batch_id,candidate_id))
                decision, reason, verified = "verified_auto", "两次 Qwen 结果一致且结构校验通过", verified + 1
        except Exception as exc:
            decision, reason, failed = "provisional", "二次验证调用失败：" + str(exc)[:160], failed + 1
        if decision == "provisional":
            kept += 1
        conn.execute("""INSERT INTO question_auto_verifications(batch_id,candidate_id,baseline_json,second_json,decision,reason)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(batch_id,candidate_id) DO UPDATE SET
                        baseline_json=excluded.baseline_json,second_json=excluded.second_json,
                        decision=excluded.decision,reason=excluded.reason,created_at=CURRENT_TIMESTAMP""",
                     (batch_id,candidate_id,baseline,second,decision,reason))
        conn.commit()
    conn.execute("INSERT INTO question_repair_batch_audit(batch_id,event_type,payload_json) VALUES(?,?,?)",
                 (batch_id,"provisional_auto_verify_completed",
                  json.dumps({"verified_auto":verified,"kept_provisional":kept,"failed":failed,"total":len(rows)},ensure_ascii=False)))
    conn.commit(); conn.close(); wb.close()
    return {"verified_auto":verified,"kept_provisional":kept,"failed":failed,"total":len(rows)}
