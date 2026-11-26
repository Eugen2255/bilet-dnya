import logging
import os
import signal
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

import psycopg
from dotenv import load_dotenv

from app.ai.grading import Grader
from app.ai.jobs import handle_grade_job
from app.queue import Job, claim_job
from app.schedule import SCHEDULE_TYPES, handle_schedule_job, schedule_courses
from app.telegram.api import BotAPI
from app.telegram.jobs import TELEGRAM_JOB_TYPES, handle_reply_job, handle_update_job


logger = logging.getLogger(__name__)
MODEL_CONCURRENCY = 2


def open_connection(database_url: str):
    return psycopg.connect(database_url, autocommit=True, connect_timeout=5)


def process_grade(database_url: str, job: Job, grader: Grader) -> None:
    with open_connection(database_url) as connection:
        handle_grade_job(connection, job.id, job.lease_until, grader)


def process_telegram(database_url: str, job: Job, bot: BotAPI) -> None:
    with open_connection(database_url) as connection:
        if job.type == "telegram_update":
            handle_update_job(connection, job, bot)
        else:
            handle_reply_job(connection, job, bot)


def check_task(future: Future, name: str) -> None:
    if error := future.exception():
        logger.error("%s handler failed (%s). Lease will expire.", name, type(error).__name__)


def run(database_url: str, grader: Grader | None, stop: Event, bot: BotAPI | None = None) -> None:
    pending = set()
    telegram_pending = None
    with (ThreadPoolExecutor(max_workers=MODEL_CONCURRENCY) as pool,
          ThreadPoolExecutor(max_workers=1) as telegram_pool):
        while not stop.is_set():
            try:
                with open_connection(database_url) as connection:
                    while not stop.is_set():
                        for future in list(pending):
                            if future.done():
                                pending.remove(future)
                                check_task(future, "Grade")
                        if telegram_pending is not None and telegram_pending.done():
                            check_task(telegram_pending, "Telegram")
                            telegram_pending = None

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
                        if bot is not None and telegram_pending is None:
                            job = claim_job(connection, TELEGRAM_JOB_TYPES, 90)
                            if job is not None:
                                telegram_pending = telegram_pool.submit(process_telegram, database_url, job, bot)
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
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    run(database_url, grader, stop, BotAPI(token) if token else None)


if __name__ == "__main__":
    main()
