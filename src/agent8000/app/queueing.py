"""Queue abstraction: RQ/Redis in production, FastAPI background task locally."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from typing import Any
from uuid import uuid4

from .config import settings
from .db import connection


class QueueUnavailable(RuntimeError):
    pass


def rq_enabled() -> bool:
    return settings.task_queue_mode.strip().lower() == "rq"


def _rq_objects():
    try:
        from redis import Redis
        from rq import Queue, Retry
    except ImportError as exc:
        raise QueueUnavailable("RQ/Redis 依赖未安装") from exc
    if not settings.redis_url:
        raise QueueUnavailable("TASK_QUEUE_MODE=rq 但未设置 REDIS_URL")
    return Redis.from_url(settings.redis_url), Queue, Retry


def queue_health() -> dict[str, Any]:
    if not rq_enabled():
        return {"mode": "inline", "ok": True}
    try:
        redis, Queue, _ = _rq_objects()
        redis.ping()
        queue = Queue(settings.rq_queue_name, connection=redis)
        return {"mode": "rq", "ok": True, "queued": len(queue)}
    except Exception as exc:
        return {"mode": "rq", "ok": False, "detail": str(exc)[:160]}


def enqueue_rq_grading_job(job_id: int) -> dict[str, Any]:
    redis, Queue, _ = _rq_objects()
    try:
        redis.ping()
        queue = Queue(settings.rq_queue_name, connection=redis)
        job = queue.enqueue(
            "app.rq_tasks.grade_submission_task",
            job_id,
            # Match the worker's hard stop to the UI/task SLA so a timed-out
            # page cannot leave an orphaned CPU-bound grading process running.
            job_timeout=max(settings.rq_job_timeout_seconds, 3600),
            ttl=24 * 3600,
            result_ttl=7 * 24 * 3600,
            failure_ttl=30 * 24 * 3600,
        )
    except Exception as exc:
        raise QueueUnavailable(f"无法投递批改队列：{str(exc)[:160]}") from exc
    now = datetime.now(timezone.utc).isoformat()
    with connection() as conn:
        conn.execute(
            "UPDATE grading_jobs SET rq_job_id=?,last_enqueued_at=?,last_error=NULL WHERE id=?",
            (job.id, now, job_id),
        )
    return {"backend": "rq", "rq_job_id": job.id}


def dispatch_grading_job(job_id: int, background_tasks=None) -> dict[str, Any]:
    if rq_enabled():
        return enqueue_rq_grading_job(job_id)
    if background_tasks is None:
        return {"backend": "inline", "deferred": True}
    from .grading_pipeline import run_grading_job
    background_tasks.add_task(run_grading_job, job_id)
    return {"backend": "inline"}


def expire_stale_grading_jobs(stale_minutes: int = 65) -> int:
    """Turn worker-killed jobs into an actionable failure instead of `running` forever.

    RQ's hard timeout kills the worker process before run_grading_job can execute
    its exception handler. The scheduler is the durable recovery path; it marks
    these rows failed without automatically looping another expensive grading run.
    """
    error = f"初评执行超过 {stale_minutes} 分钟，后台任务已超时终止；作业图片已保留，可点击重新初评。"
    with connection() as conn:
        rows = conn.execute(
            """SELECT id,submission_id FROM grading_jobs
               WHERE status='running'
                 AND julianday('now') - julianday(COALESCE(last_enqueued_at,last_started_at,started_at,created_at)) > ?""",
            (stale_minutes / 1440.0,),
        ).fetchall()
        for row in rows:
            changed = conn.execute(
                """UPDATE grading_jobs SET status='failed',last_error=?,finished_at=CURRENT_TIMESTAMP,
                   result_json=? WHERE id=? AND status='running'""",
                (error, __import__('json').dumps({"error": error, "retryable": True, "results": []}, ensure_ascii=False), row["id"]),
            ).rowcount
            if changed:
                conn.execute(
                    """UPDATE submissions SET status='review_required',score=NULL,quality_score=NULL,
                       needs_review=1,feedback=? WHERE id=? AND released_at IS NULL""",
                    (error, row["submission_id"]),
                )
        return len(rows)


def enqueue_pending_grading_jobs(limit: int = 100) -> dict[str, int]:
    """Scheduler recovery path for DB jobs left queued while Redis was unavailable."""
    if not rq_enabled():
        return {"enqueued": 0, "failed": 0}
    with connection() as conn:
        rows = conn.execute(
            "SELECT id FROM grading_jobs WHERE status='queued' AND (rq_job_id IS NULL OR rq_job_id='') ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    enqueued = failed = 0
    for row in rows:
        try:
            enqueue_rq_grading_job(int(row["id"]))
            enqueued += 1
        except QueueUnavailable:
            failed += 1
    return {"enqueued": enqueued, "failed": failed}
