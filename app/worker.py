import logging
import os
import signal
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore, Event
from time import monotonic

import psycopg
from dotenv import load_dotenv

from app.ai.client import ChatClient
from app.ai.grading import Grader
from app.ai.jobs import handle_appeal_job, handle_grade_job
from app.imports.service import (
    IMPORT_JOB_TYPE, IMPORT_LEASE_SECONDS, discard_old_upload_directories, expire_uploads, handle_import_job, storage_path,
)
from app.queue import Job, claim_job
from app.rubrics.generation import handle_index_job, handle_rubric_job
from app.rubrics.service import INDEX_JOB_TYPE, RUBRIC_JOB_TYPE
from app.schedule import SCHEDULE_TYPES, handle_schedule_job, schedule_courses
from app.telegram.api import BotAPI
from app.telegram.jobs import TELEGRAM_JOB_TYPES, handle_reply_job, handle_update_job


logger = logging.getLogger(__name__)
MODEL_CONCURRENCY = 2
PREPARATION_TYPES = (IMPORT_JOB_TYPE, INDEX_JOB_TYPE, RUBRIC_JOB_TYPE)


def open_connection(database_url: str):
    return psycopg.connect(database_url, autocommit=True, connect_timeout=5)


def process_grade(database_url: str, job: Job, grader: Grader, slots: BoundedSemaphore) -> None:
    with slots, open_connection(database_url) as connection:
        handler = handle_appeal_job if job.type == "appeal" else handle_grade_job
        handler(connection, job.id, job.lease_until, grader)


def process_telegram(database_url: str, job: Job, bot: BotAPI) -> None:
    with open_connection(database_url) as connection:
        if job.type == "telegram_update":
            handle_update_job(connection, job, bot)
        else:
            handle_reply_job(connection, job, bot)


def process_preparation(database_url: str, job: Job, storage: Path, grader: Grader | None, slots: BoundedSemaphore) -> None:
    with open_connection(database_url) as connection:
        if job.type == INDEX_JOB_TYPE:
            handle_index_job(connection, job)
            return
        suggest, model = None, None
        if grader is not None:
            model = grader.primary.model

            def complete(messages):
                with slots:
                    if job.type == RUBRIC_JOB_TYPE:
                        return grader.primary.complete(messages, temperature=0.2, max_tokens=1000)
                    return grader.primary.complete(messages)

            suggest = complete
        if job.type == RUBRIC_JOB_TYPE:
            handle_rubric_job(connection, job, suggest=suggest, model=model)
        else:
            handle_import_job(connection, job, storage, suggest=suggest, model=model)


def check_task(future: Future, name: str) -> None:
    if error := future.exception():
        logger.error("%s handler failed (%s). Lease will expire.", name, type(error).__name__)


def run(
    database_url: str, grader: Grader | None, stop: Event, bot: BotAPI | None = None, *, reviewer: Grader | None = None,
) -> None:
    if reviewer is None and grader is not None:
        reviewer = Grader(grader.primary)
    graders = {kind: model for kind, model in (("grade", grader), ("appeal", reviewer)) if model is not None}
    pending = set()
    telegram_pending = None
    import_pending = None
    uploads = storage_path()
    cleanup_at = 0.0
    slots = BoundedSemaphore(MODEL_CONCURRENCY)
    with (ThreadPoolExecutor(max_workers=MODEL_CONCURRENCY) as pool,
          ThreadPoolExecutor(max_workers=1) as import_pool,
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
                        if import_pending is not None and import_pending.done():
                            check_task(import_pending, "Preparation")
                            import_pending = None

                        schedule_courses(connection, datetime.now(timezone.utc))
                        if monotonic() >= cleanup_at:
                            expire_uploads(connection, uploads)
                            cleanup_at = monotonic() + 60
                        # Keep timetable jobs responsive while model calls run in the pool.
                        for _ in range(20):
                            job = claim_job(connection, SCHEDULE_TYPES, 30)
                            if job is None:
                                break
                            handle_schedule_job(connection, job, datetime.now(timezone.utc))

                        if graders:
                            while len(pending) < MODEL_CONCURRENCY:
                                job = claim_job(connection, tuple(graders), 90)
                                if job is None:
                                    break
                                pending.add(pool.submit(process_grade, database_url, job, graders[job.type], slots))
                        if import_pending is None and not pending:
                            job = claim_job(connection, PREPARATION_TYPES, IMPORT_LEASE_SECONDS)
                            if job is not None:
                                import_pending = import_pool.submit(
                                    process_preparation, database_url, job, uploads, grader, slots,
                                )
                        if bot is not None and telegram_pending is None:
                            job = claim_job(connection, TELEGRAM_JOB_TYPES, 90)
                            if job is not None:
                                telegram_pending = telegram_pool.submit(process_telegram, database_url, job, bot)
                        stop.wait(1)
            except (psycopg.OperationalError, psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
                logger.warning("Database or worker migration unavailable (%s)", type(exc).__name__)
                if monotonic() >= cleanup_at:
                    discard_old_upload_directories(uploads)
                    cleanup_at = monotonic() + 60
                stop.wait(5)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("Set DATABASE_URL")
    grader = reviewer = None
    if os.getenv("MODEL_PRIMARY_URL") or os.getenv("MODEL_PRIMARY_ID"):
        try:
            grader = Grader.from_env()
            if review_model := os.getenv("MODEL_REVIEW_ID", "").strip():
                reviewer = Grader(replace(ChatClient.from_env(), model=review_model))
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    else:
        logger.info("Model is unconfigured. Imports use rules and rubric drafts can be filled manually.")

    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    run(database_url, grader, stop, BotAPI(token) if token else None, reviewer=reviewer)


if __name__ == "__main__":
    main()
