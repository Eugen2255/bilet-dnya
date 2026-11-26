from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class Job:
    id: UUID
    type: str
    payload: object
    lease_until: datetime


def claim_job(connection: Connection, types: tuple[str, ...], lease_seconds: int) -> Job | None:
    """Claim one ready job or recover an expired lease in a committed transaction."""
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute("""
            WITH candidate AS (
                SELECT id FROM jobs
                WHERE type = ANY (%s) AND (
                    (state = 'queued' AND run_at <= statement_timestamp())
                    OR (state = 'running' AND lease_until <= statement_timestamp())
                )
                ORDER BY run_at, id
                LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs AS job
            SET state = 'running', tries = tries + 1,
                lease_until = clock_timestamp() + make_interval(secs => %s)
            FROM candidate
            WHERE job.id = candidate.id
            RETURNING job.id, job.type, job.payload, job.lease_until
        """, (list(types), lease_seconds)).fetchone()
    return Job(**row) if row else None


def enqueue_delivery(cursor: Cursor, key: str, job_type: str, payload: dict) -> None:
    cursor.execute("""
        INSERT INTO jobs (key, type, payload) VALUES (%s, %s, %s)
        ON CONFLICT (key) DO NOTHING
    """, (key, job_type, Jsonb(payload)))
