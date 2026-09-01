"""Queue abstraction: RQ/Redis in production, FastAPI background task locally."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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
    redis, Queue, Retry = _rq_objects()
    try:
        redis.ping()
        queue = Queue(settings.rq_queue_name, connection=redis)
        job = queue.enqueue(
            "app.rq_tasks.grade_submission_task",
            job_id,
            job_timeout=settings.rq_job_timeout_seconds,
            result_ttl=7 * 24 * 3600,
            failure_ttl=30 * 24 * 3600,
            retry=Retry(max=settings.rq_retry_max, interval=[60, 300, 900]),
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
