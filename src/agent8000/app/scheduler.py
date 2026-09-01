"""Separate deadline scheduler: never bind scheduled work to a web request process."""
from datetime import datetime, timezone
import logging, time
from .config import settings
from .db import init_db, queue_due_grading
from .queueing import enqueue_pending_grading_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def main() -> None:
    settings.prepare_dirs(); init_db()
    while True:
        count = queue_due_grading(datetime.now(timezone.utc).isoformat())
        dispatch = enqueue_pending_grading_jobs()
        if count or dispatch["enqueued"]:
            logging.info("queued due=%s dispatched=%s", count, dispatch["enqueued"])
        time.sleep(60)

if __name__ == "__main__": main()
