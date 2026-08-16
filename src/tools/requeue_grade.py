import asyncio, sys
sys.path.insert(0, r"D:/My File/大四/高数教材答案/高数作业助手")
from app.db import connection
from app.grading_pipeline import run_grading_job

SUB_ID = 2
with connection() as conn:
    row = conn.execute(
        "SELECT id, status FROM grading_jobs WHERE submission_id=?", (SUB_ID,)
    ).fetchone()
    if not row:
        raise SystemExit("no grading job for submission %s" % SUB_ID)
    jid = row["id"]
    conn.execute("UPDATE grading_jobs SET status='queued', result_json=NULL WHERE id=?", (jid,))
    conn.commit()
    print("reset job", jid, "old_status=", row["status"], "-> queued")

asyncio.run(run_grading_job(jid))
print("done")
