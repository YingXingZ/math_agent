"""Optional queue worker for deployments that do not use web background tasks."""
import asyncio
import time

from .config import settings
from .db import connection, init_db
from .grading_pipeline import run_grading_job


def claim_next_job() -> int | None:
    with connection() as conn:
        row = conn.execute("SELECT id FROM grading_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        return int(row["id"]) if row else None


def main() -> None:
    settings.prepare_dirs()
    init_db()
    while True:
        job_id = claim_next_job()
        if job_id is None:
            time.sleep(5)
            continue
        asyncio.run(run_grading_job(job_id))


if __name__ == "__main__":
    main()
