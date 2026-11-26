from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from app.ai.client import ModelError
from app.ai.grading import Grade, Grader, RubricPoint


RETRY_DELAYS = (15, 60)
POINTS = TypeAdapter(list[RubricPoint])


def handle_grade_job(
    connection: Connection, job_id: UUID, lease_until: datetime, grader: Grader
) -> str:
    """Process a claimed grade job. The worker increments tries when acquiring a lease."""
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Grading requires an idle database connection")

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        job = cursor.execute("""
            SELECT key, payload, tries FROM jobs
            WHERE id = %s AND type = 'grade' AND state = 'running'
                AND lease_until = %s AND lease_until > clock_timestamp()
        """, (job_id, lease_until)).fetchone()
        if job is None:
            return "lease_lost"
        attempt_id = _attempt_id(job["key"], job["payload"])
        attempt = cursor.execute("""
            SELECT attempt.body, attempt.result, ticket.question, rubric.answer, rubric.points
            FROM attempts AS attempt
            JOIN assignments AS assignment ON assignment.id = attempt.assignment_id
            JOIN tickets AS ticket ON ticket.id = assignment.ticket_id
            JOIN rubrics AS rubric ON rubric.id = assignment.rubric_id
            WHERE attempt.id = %s
        """, (attempt_id,)).fetchone() if attempt_id is not None else None

    grade = None
    error = None
    if attempt_id is None:
        error = ModelError("INVALID_GRADE_JOB", retryable=False)
    elif attempt and attempt["body"] is not None and attempt["result"] is None:
        try:
            if job["tries"] > len(RETRY_DELAYS) + 1:
                raise ModelError("GRADE_ATTEMPTS_EXHAUSTED", retryable=False)
            points = POINTS.validate_python(attempt["points"])
            grade = grader.grade(attempt["question"], attempt["answer"], points, attempt["body"])
        except ValidationError:
            error = ModelError("INVALID_RUBRIC", retryable=False)
        except ModelError as failure:
            error = failure

    return _finish(connection, job_id, lease_until, attempt_id, grade, error)


def _attempt_id(key: str, payload: object) -> UUID | None:
    raw_id = payload.get("attemptId") if isinstance(payload, dict) else None
    if not isinstance(raw_id, str):
        return None
    try:
        attempt_id = UUID(raw_id)
    except ValueError:
        return None
    return attempt_id if key == f"grade:{attempt_id}" else None


def _finish(
    connection: Connection,
    job_id: UUID,
    lease_until: datetime,
    attempt_id: UUID | None,
    grade: Grade | None,
    error: ModelError | None,
) -> str:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        job = cursor.execute("""
            SELECT tries FROM jobs
            WHERE id = %s AND state = 'running' AND lease_until = %s
                AND lease_until > clock_timestamp()
            FOR UPDATE
        """, (job_id, lease_until)).fetchone()
        if job is None:
            return "lease_lost"

        if attempt_id is not None:
            attempt = cursor.execute("""
                SELECT body, result FROM attempts WHERE id = %s FOR UPDATE
            """, (attempt_id,)).fetchone()
            if attempt is None or attempt["body"] is None or attempt["result"] is not None:
                _update_job(cursor, job_id, "done", {"skipped": True})
                return "done"

        if error is not None:
            tries = job["tries"]
            if error.retryable and tries <= len(RETRY_DELAYS):
                delay = max(RETRY_DELAYS[max(0, tries - 1)], error.retry_after or 0)
                _update_job(cursor, job_id, "queued", {"error": error.code}, delay)
                return "queued"
            if attempt_id is not None:
                cursor.execute("UPDATE attempts SET status = 'failed' WHERE id = %s", (attempt_id,))
                _enqueue_delivery(cursor, job_id, lease_until, attempt_id)
            _update_job(cursor, job_id, "failed", {"error": error.code})
            return "failed"

        if grade is None:
            raise RuntimeError("Missing grade for an active attempt")

        cursor.execute("""
            UPDATE attempts SET status = 'graded', result = %s, model = %s, prompt_version = %s,
                graded_at = clock_timestamp()
            WHERE id = %s
        """, (Jsonb(grade.result), grade.model, grade.prompt_version, attempt_id))
        _enqueue_delivery(cursor, job_id, lease_until, attempt_id)
        _update_job(cursor, job_id, "done", {"attemptId": str(attempt_id)})
        return "done"


def _update_job(cursor, job_id: UUID, state: str, result: dict, delay: int = 0) -> None:
    cursor.execute("""
        UPDATE jobs SET state = %s, result = %s, lease_until = NULL,
            run_at = CASE WHEN %s = 'queued' THEN clock_timestamp() + (%s * interval '1 second')
                ELSE run_at END
        WHERE id = %s
    """, (state, Jsonb(result), state, delay, job_id))


def _enqueue_delivery(cursor, grade_job_id: UUID, lease_until: datetime, attempt_id: UUID) -> None:
    attempt = str(attempt_id)
    cursor.execute("""
        INSERT INTO jobs (key, type, payload)
        VALUES (%s, 'deliver_grade', %s)
        ON CONFLICT (key) DO NOTHING
    """, (
        f"deliver_grade:{grade_job_id}:{lease_until.timestamp()}",
        Jsonb({"attemptId": attempt}),
    ))
