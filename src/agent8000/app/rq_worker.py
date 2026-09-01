"""Entry point for the production RQ worker container."""
from .config import settings
from .db import init_db


def main() -> None:
    if settings.task_queue_mode.strip().lower() != "rq":
        raise RuntimeError("RQ worker requires TASK_QUEUE_MODE=rq")
    from redis import Redis
    from rq import Queue, Worker
    init_db()
    redis = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=redis)
    Worker([queue], connection=redis).work(with_scheduler=True)


if __name__ == "__main__":
    main()
