import argparse
import json
import os
import re
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg import Connection, Cursor
from psycopg.rows import dict_row

from app.imports.service import discard_uploads, storage_path
from app.privacy import ledger_path, open_ledger, reapply_deletions, trim_ledger


def record_error(cursor: Cursor, job_id: UUID, provider: str, code: str) -> None:
    safe_code = code if re.fullmatch(r"[A-Z0-9_]{1,64}", code) else "UNKNOWN"
    cursor.execute("INSERT INTO operation_errors (job_id, provider, code) VALUES (%s, %s, %s)",
                   (job_id, provider, safe_code))


def mark_delivered(cursor: Cursor, attempt_id: UUID, kind: str) -> None:
    if kind == "acknowledgement":
        cursor.execute("UPDATE attempts SET acknowledged_at = clock_timestamp() WHERE id = %s AND acknowledged_at IS NULL",
                       (attempt_id,))
    elif kind == "grade":
        cursor.execute("UPDATE attempts SET delivered_at = clock_timestamp() WHERE id = %s AND delivered_at IS NULL",
                       (attempt_id,))


def metrics(cursor: Cursor) -> dict:
    delivery = cursor.execute("""
        SELECT count(*) AS submitted, count(acknowledged_at) AS acknowledged, count(delivered_at) AS delivered,
            round(percentile_disc(0.95) WITHIN GROUP (
                ORDER BY 1000 * extract(epoch FROM acknowledged_at - submitted_at)
            )) AS acknowledgement_p95_ms,
            round(percentile_disc(0.95) WITHIN GROUP (
                ORDER BY 1000 * extract(epoch FROM delivered_at - submitted_at)
            )) AS grade_delivery_p95_ms
        FROM attempts WHERE submitted_at BETWEEN statement_timestamp() - interval '7 days' AND statement_timestamp()
    """).fetchone()
    queue = cursor.execute("""
        SELECT count(*) AS ready, COALESCE(round(extract(epoch FROM statement_timestamp() - min(run_at))), 0) AS oldest_seconds
        FROM jobs WHERE state = 'queued' AND run_at <= statement_timestamp()
    """).fetchone()
    errors = cursor.execute("""
        SELECT provider, code, count(*) AS total FROM operation_errors
        WHERE occurred_at >= statement_timestamp() - interval '7 days'
        GROUP BY provider, code ORDER BY provider, code
    """).fetchall()
    undelivered = cursor.execute("""
        SELECT count(*) AS total FROM jobs WHERE state IN ('failed', 'delivery_unknown')
            AND (type LIKE 'deliver_%%' OR type IN ('telegram_reply', 'telegram_prompt'))
            AND finished_at >= statement_timestamp() - interval '7 days'
    """).fetchone()["total"]
    return {
        "delivery": {key: int(value) if value is not None else None for key, value in delivery.items()},
        "queue": {key: int(value) for key, value in queue.items()}, "provider_errors": errors,
        "undelivered_jobs": undelivered,
    }


def delete_courses(cursor: Cursor, course_ids: list[UUID]) -> list[UUID]:
    if not course_ids:
        return []
    imports = [row["id"] for row in cursor.execute("SELECT id FROM imports WHERE course_id = ANY (%s)",
                                                  (course_ids,)).fetchall()]
    cursor.execute("""
        WITH owned AS (
            SELECT a.id AS assignment_id, p.id AS attempt_id FROM assignments AS a
            LEFT JOIN attempts AS p ON p.assignment_id = a.id WHERE a.course_id = ANY (%s)
        )
        DELETE FROM jobs WHERE id IN (SELECT j.id FROM jobs AS j
            WHERE j.payload->>'courseId' = ANY (%s) OR j.payload->>'importId' = ANY (%s)
            OR j.payload->>'assignmentId' IN (SELECT assignment_id::text FROM owned)
            OR j.payload->>'attemptId' IN (SELECT attempt_id::text FROM owned)
            FOR UPDATE OF j SKIP LOCKED)
    """, (course_ids, list(map(str, course_ids)), list(map(str, imports))))
    cursor.execute("DELETE FROM courses WHERE id = ANY (%s)", (course_ids,))
    return imports


def purge_expired(connection: Connection, storage: Path, *, as_of: datetime | None = None) -> dict[str, int]:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        now = as_of or cursor.execute("SELECT statement_timestamp() AS now").fetchone()["now"]
        # Free-form feedback can echo a submitted answer. Keep numeric and rubric-key results.
        cursor.execute("""
            UPDATE appeals AS review SET result = (review.result - 'feedback' - 'errors') ||
                jsonb_build_object('errorCount', CASE WHEN jsonb_typeof(review.result->'errors') = 'array'
                    THEN jsonb_array_length(review.result->'errors') ELSE 0 END)
            FROM attempts AS p, assignments AS a, courses AS c
            WHERE review.attempt_id = p.id AND p.assignment_id = a.id AND a.course_id = c.id
                AND c.exam_date + 7 <= (%s::timestamptz AT TIME ZONE COALESCE(c.timezone, 'UTC'))::date
                AND review.result ?| ARRAY['feedback', 'errors']
        """, (now,))
        scrubbed = cursor.execute("""
            UPDATE attempts AS p SET body = NULL,
                status = CASE WHEN p.status = 'submitted' THEN 'failed' ELSE p.status END,
                result = CASE WHEN p.result ?| ARRAY['feedback', 'errors'] THEN (p.result - 'feedback' - 'errors') ||
                    jsonb_build_object('errorCount', CASE WHEN jsonb_typeof(p.result->'errors') = 'array'
                        THEN jsonb_array_length(p.result->'errors') ELSE 0 END)
                    ELSE p.result END
            FROM assignments AS a, courses AS c WHERE p.assignment_id = a.id AND a.course_id = c.id
                AND c.exam_date + 7 <= (%s::timestamptz AT TIME ZONE COALESCE(c.timezone, 'UTC'))::date
                AND (p.body IS NOT NULL OR p.result ?| ARRAY['feedback', 'errors'])
        """, (now,)).rowcount
        courses = cursor.execute("""
            SELECT id FROM courses WHERE exam_date + 30 <= (%s::timestamptz AT TIME ZONE COALESCE(timezone, 'UTC'))::date
            ORDER BY id FOR UPDATE
        """, (now,)).fetchall()
        imports = delete_courses(cursor, [row["id"] for row in courses])
        cursor.execute("DELETE FROM web_sessions WHERE expires_at <= %s", (now,))
        cursor.execute("DELETE FROM action_tokens WHERE expires_at <= %s", (now,))
        cursor.execute("DELETE FROM deletion_requests WHERE expires_at <= %s", (now,))
        cursor.execute("DELETE FROM operation_errors WHERE occurred_at <= %s - interval '7 days'", (now,))
        cursor.execute("DELETE FROM telegram_updates WHERE received_at <= %s - interval '7 days'", (now,))
        cursor.execute("""
            DELETE FROM telegram_send_limits AS limits
            WHERE limits.chat_id <> 0
                AND GREATEST(limits.next_send_at, limits.last_reachable_at) <= %s - interval '7 days'
                AND (limits.blocked_at IS NULL OR (
                    limits.blocked_at <= %s - interval '7 days'
                    AND NOT EXISTS (
                        SELECT 1 FROM users WHERE dm_chat_id = limits.chat_id OR telegram_user_id = limits.chat_id
                    )
                    AND NOT EXISTS (SELECT 1 FROM study_groups WHERE telegram_chat_id = limits.chat_id)
                ))
        """, (now, now))
        cursor.execute("""
            DELETE FROM jobs WHERE state IN ('done', 'failed', 'delivery_unknown')
                AND COALESCE(finished_at, run_at) <= %s - interval '7 days' AND type <> 'grade'
        """, (now,))
        # Failed grade jobs provide the retry button while the answer is still available.
        cursor.execute("""
            DELETE FROM jobs AS j WHERE j.type = 'grade' AND j.state IN ('done', 'failed')
                AND COALESCE(j.finished_at, j.run_at) <= %s - interval '7 days'
                AND NOT EXISTS (SELECT FROM attempts AS p WHERE p.id::text = j.payload->>'attemptId'
                    AND p.body IS NOT NULL AND p.status = 'failed')
        """, (now,))
    for import_id in imports:
        discard_uploads(storage, import_id)
    return {"scrubbed_answers": scrubbed, "removed_courses": len(courses)}


def replay_restored(connection: Connection, path: Path) -> int:
    erased = reapply_deletions(connection, path, required=True)
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("UPDATE telegram_updates SET payload = NULL, state = 'failed' WHERE payload IS NOT NULL")
        cursor.execute("""
            UPDATE jobs SET state = 'done', lease_until = NULL, payload = '{}'::jsonb, result = NULL
            WHERE state IN ('queued', 'running') AND (type LIKE 'telegram_%%' OR type LIKE 'deliver_%%')
        """)
        cursor.execute("DELETE FROM web_sessions")
        cursor.execute("DELETE FROM deletion_requests")
    return erased


def main() -> None:
    parser = argparse.ArgumentParser(description="Bilet administrative operations")
    parser.add_argument("operation", choices=("init-ledger", "replay-restored", "purge", "metrics"))
    parser.add_argument("--database-name", help="Database to sanitize after restoring a backup")
    args = parser.parse_args()
    load_dotenv()
    path = ledger_path()
    if args.operation == "init-ledger":
        with closing(open_ledger(path)):
            return
    if not (url := os.getenv("DATABASE_URL")):
        parser.exit(1, "DATABASE_URL is required\n")
    if args.database_name:
        if args.operation != "replay-restored" or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", args.database_name):
            parser.error("--database-name requires replay-restored and a valid database name")
        parts = urlsplit(url)
        url = urlunsplit(parts._replace(path="/" + args.database_name))
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=5, row_factory=dict_row) as db:
            if args.operation == "replay-restored":
                output = {"reapplied_deletions": replay_restored(db, path)}
            elif args.operation == "purge":
                output = purge_expired(db, storage_path())
            else:
                with db.cursor() as cursor:
                    output = metrics(cursor)
    except (psycopg.Error, OSError) as error:
        parser.exit(1, f"Operation failed ({type(error).__name__})\n")
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
