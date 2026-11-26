import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

import psycopg
from dotenv import load_dotenv

from app.ai.grading import Grader
from app.ai.jobs import handle_grade_job
from app.queue import Job, claim_job
from app.schedule import SCHEDULE_TYPES, handle_schedule_job, schedule_courses


logger = logging.getLogger(__name__)
MODEL_CONCURRENCY = 2


def open_connection(database_url: str):
    return psycopg.connect(database_url, autocommit=True, connect_timeout=5)


def process_grade(database_url: str, job: Job, grader: Grader) -> None:
    with open_connection(database_url) as connection:
        handle_grade_job(connection, job.id, job.lease_until, grader)


def run(database_url: str, grader: Grader | None, stop: Event) -> None:
    pending = set()
    with ThreadPoolExecutor(max_workers=MODEL_CONCURRENCY) as pool:
        while not stop.is_set():
            try:
                with open_connection(database_url) as connection:
                    while not stop.is_set():
                        for future in list(pending):
                            if future.done():
                                pending.remove(future)
                                if error := future.exception():
                                    logger.error("Grade handler failed (%s); lease will expire", type(error).__name__)

                        schedule_courses(connection, datetime.now(timezone.utc))
                        # Keep timetable jobs responsive while model calls run in the pool.
                        for _ in range(20):
                            job = claim_job(connection, SCHEDULE_TYPES, 30)
                            if job is None:
                                break
                            handle_schedule_job(connection, job, datetime.now(timezone.utc))

                        if grader is not None:
                            while len(pending) < MODEL_CONCURRENCY:
                                job = claim_job(connection, ("grade",), 90)
                                if job is None:
                                    break
                                pending.add(pool.submit(process_grade, database_url, job, grader))
                        stop.wait(1)
            except (psycopg.OperationalError, psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
                logger.warning("Database or worker migration unavailable (%s)", type(exc).__name__)
                stop.wait(5)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("Set DATABASE_URL")
    grader = None
    if os.getenv("MODEL_PRIMARY_URL") or os.getenv("MODEL_PRIMARY_ID"):
        try:
            grader = Grader.from_env()
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    else:
        logger.info("Model is unconfigured; running timetable and summaries only")

    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(database_url, grader, stop)


if __name__ == "__main__":
    main()
