"""Re-grade submission #1 (张三, 49-page PDF) to prove the page-split fix.

Inserts a fresh queued grading_job and drives run_grading_job in-process so the
fixed module (render ALL pages, no 12-page cap) is exercised. Writes the result
summary to _regrade_zhang.json for inspection.
"""
import sys, json, asyncio, sqlite3
sys.path.insert(0, r"D:/My File/大四/高数教材答案/高数作业助手")

from app.db import connection
from app.grading_pipeline import run_grading_job

adb = r"D:\My File\大四\高数教材答案\高数作业助手\data\homework.db"

with connection() as conn:
    # grading_jobs has a UNIQUE(submission_id): reset the existing job to queued
    # rather than inserting a duplicate.
    cur = conn.execute(
        "UPDATE grading_jobs SET status='queued', result_json=NULL WHERE submission_id=?", (1,)
    )
    row = conn.execute(
        "SELECT id FROM grading_jobs WHERE submission_id=?", (1,)
    ).fetchone()
    job_id = row["id"]
print("reset grading_job", job_id, "-> queued for submission 1")

asyncio.run(run_grading_job(job_id))

with connection() as conn:
    row = conn.execute(
        "SELECT status, result_json FROM grading_jobs WHERE id=?", (job_id,)
    ).fetchone()
    res = json.loads(row["result_json"] or "{}")
    sub = conn.execute(
        "SELECT status, score, needs_review FROM submissions WHERE id=1"
    ).fetchone()

summary = {
    "job_id": job_id,
    "job_status": row["status"],
    "submission_status": dict(sub),
    "total_score": res.get("total_score"),
    "max_score": res.get("max_score"),
    "qwen_error": res.get("qwen_error") or None,
    "needs_review": res.get("needs_review"),
    "per_question": [
        {
            "no": r.get("sort_order"),
            "score": r.get("score"),
            "max": r.get("max_score"),
            "correct": r.get("correct"),
            "confidence": r.get("confidence"),
            "needs_review": r.get("needs_review"),
            "reasons": r.get("review_reasons"),
        }
        for r in res.get("results", [])
    ],
}
out = r"D:/workbuddy/2026-08-06-15-31-48/_regrade_zhang.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(json.dumps(summary, ensure_ascii=False, indent=2))
