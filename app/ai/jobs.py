from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from app.ai.client import ModelError
from app.ai.grading import Grade, Grader, RubricPoint
from app.practice import reconsider_personal_repeat, remember_weak_attempt
from app.queue import enqueue_delivery


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
        grade, error = _evaluate_attempt(grader, attempt, job["tries"])

    return _finish(connection, job_id, lease_until, attempt_id, grade, error)


def handle_appeal_job(connection: Connection, job_id: UUID, lease_until: datetime, reviewer: Grader) -> str:
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("Review requires an idle database connection")

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        job = cursor.execute("""
            SELECT key, payload, tries FROM jobs
            WHERE id = %s AND type = 'appeal' AND state = 'running'
                AND lease_until = %s AND lease_until > clock_timestamp()
        """, (job_id, lease_until)).fetchone()
        if job is None:
            return "lease_lost"
        attempt_id = _attempt_id(job["key"], job["payload"], prefix="appeal")
        attempt = cursor.execute("""
            SELECT p.body, ticket.question, rubric.answer, rubric.points
            FROM appeals AS appeal JOIN attempts AS p ON p.id = appeal.attempt_id
            JOIN assignments AS assignment ON assignment.id = p.assignment_id
            JOIN tickets AS ticket ON ticket.id = assignment.ticket_id
            JOIN rubrics AS rubric ON rubric.id = assignment.rubric_id
            WHERE appeal.attempt_id = %s AND appeal.state = 'queued' AND p.status = 'graded'
        """, (attempt_id,)).fetchone()

    grade = error = None
    if attempt is not None and attempt["body"] is not None:
        grade, error = _evaluate_attempt(reviewer, attempt, job["tries"])

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = cursor.execute("""
            SELECT tries FROM jobs WHERE id = %s AND state = 'running' AND lease_until = %s
                AND lease_until > clock_timestamp() FOR UPDATE
        """, (job_id, lease_until)).fetchone()
        if current is None:
            return "lease_lost"
        if attempt_id is None:
            _update_job(cursor, job_id, "failed", {"error": "INVALID_APPEAL_JOB"})
            return "failed"
        live = cursor.execute("""
            SELECT p.body, (p.result->>'score')::int AS original_score
            FROM appeals AS appeal JOIN attempts AS p ON p.id = appeal.attempt_id
            WHERE appeal.attempt_id = %s AND appeal.state = 'queued' AND p.status = 'graded'
            FOR NO KEY UPDATE OF appeal, p
        """, (attempt_id,)).fetchone()
        if live is None:
            _update_job(cursor, job_id, "done", {"skipped": True})
            return "done"
        if live["body"] is None or attempt is None or attempt["body"] is None:
            error = ModelError("APPEAL_ANSWER_UNAVAILABLE", retryable=False)
        if error is not None:
            tries = current["tries"]
            if error.retryable and tries <= len(RETRY_DELAYS):
                delay = max(RETRY_DELAYS[max(0, tries - 1)], error.retry_after or 0)
                _update_job(cursor, job_id, "queued", {"error": error.code}, delay)
                return "queued"
            cursor.execute("""
                UPDATE appeals SET state = 'failed', reviewed_at = clock_timestamp() WHERE attempt_id = %s
            """, (attempt_id,))
            _update_job(cursor, job_id, "failed", {"error": error.code})
        else:
            if grade is None:
                raise RuntimeError("Missing grade for an active appeal")
            reconsider_personal_repeat(cursor, attempt_id, live["original_score"], grade.result["score"])
            cursor.execute("""
                UPDATE appeals SET state = 'succeeded', result = %s, model = %s, prompt_version = %s,
                    reviewed_at = clock_timestamp() WHERE attempt_id = %s
            """, (Jsonb(grade.result), grade.model, grade.prompt_version, attempt_id))
            _update_job(cursor, job_id, "done", {"attemptId": str(attempt_id)})
        enqueue_delivery(cursor, f"deliver_appeal:{attempt_id}", "deliver_grade", {"attemptId": str(attempt_id)})
        return "failed" if error is not None else "done"


def _evaluate_attempt(grader: Grader, attempt: dict, tries: int) -> tuple[Grade | None, ModelError | None]:
    try:
        if tries > len(RETRY_DELAYS) + 1:
            raise ModelError("GRADE_ATTEMPTS_EXHAUSTED", retryable=False)
        points = POINTS.validate_python(attempt["points"])
        return grader.grade(attempt["question"], attempt["answer"], points, attempt["body"]), None
    except ValidationError:
        return None, ModelError("INVALID_RUBRIC", retryable=False)
    except ModelError as error:
        return None, error


def _attempt_id(key: str, payload: object, *, prefix: str = "grade") -> UUID | None:
    raw_id = payload.get("attemptId") if isinstance(payload, dict) else None
    if not isinstance(raw_id, str):
        return None
    try:
        attempt_id = UUID(raw_id)
    except ValueError:
        return None
    return attempt_id if key == f"{prefix}:{attempt_id}" else None


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
                _enqueue_delivery(cursor, job_id, lease_until, attempt_id, "failed")
            _update_job(cursor, job_id, "failed", {"error": error.code})
            return "failed"

        if grade is None:
            raise RuntimeError("Missing grade for an active attempt")

        cursor.execute("""
            UPDATE attempts SET status = 'graded', result = %s, model = %s, prompt_version = %s,
                graded_at = clock_timestamp()
            WHERE id = %s
        """, (Jsonb(grade.result), grade.model, grade.prompt_version, attempt_id))
        if grade.result["score"] < 3:
            remember_weak_attempt(cursor, attempt_id)
        _enqueue_delivery(cursor, job_id, lease_until, attempt_id, "graded")
        _update_job(cursor, job_id, "done", {"attemptId": str(attempt_id)})
        return "done"


def _update_job(cursor, job_id: UUID, state: str, result: dict, delay: int = 0) -> None:
    cursor.execute("""
        UPDATE jobs SET state = %s, result = %s, lease_until = NULL,
            run_at = CASE WHEN %s = 'queued' THEN clock_timestamp() + (%s * interval '1 second')
                ELSE run_at END
        WHERE id = %s
    """, (state, Jsonb(result), state, delay, job_id))


def _enqueue_delivery(cursor, grade_job_id: UUID, lease_until: datetime, attempt_id: UUID, status: str) -> None:
    attempt = str(attempt_id)
    cursor.execute("""
        INSERT INTO jobs (key, type, payload)
        VALUES (%s, 'deliver_grade', %s)
        ON CONFLICT (key) DO NOTHING
    """, (
        f"deliver_grade:{grade_job_id}:{lease_until.timestamp()}",
        Jsonb({"attemptId": attempt, "expectedStatus": status}),
    ))
