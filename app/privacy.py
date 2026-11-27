import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from psycopg import Connection, Cursor
from psycopg.rows import dict_row


def ledger_path() -> Path:
    return Path(os.getenv("PRIVACY_LEDGER_PATH") or "data/privacy/deletions.sqlite3")


def lock_account(cursor: Cursor, telegram_id: int) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(-%s::bigint)", (telegram_id,))


def open_ledger(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    try:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS deletions (
                user_id TEXT PRIMARY KEY, confirmed_at INTEGER NOT NULL DEFAULT (unixepoch())
            )
        """)
    except BaseException:
        db.close()
        raise
    return db


def request_deletion(cursor: Cursor, telegram_id: int, chat_id: int) -> UUID | None:
    row = cursor.execute("""
        INSERT INTO deletion_requests (user_id, expires_at)
        SELECT id, clock_timestamp() + interval '10 minutes' FROM users
        WHERE telegram_user_id = %s AND (dm_chat_id = %s OR dm_chat_id IS NULL)
        ON CONFLICT (user_id) DO UPDATE SET token = gen_random_uuid(), expires_at = EXCLUDED.expires_at
        RETURNING token
    """, (telegram_id, chat_id)).fetchone()
    return row["token"] if row else None


def confirm_deletion(cursor: Cursor, token: UUID, telegram_id: int, chat_id: int) -> bool:
    lock_account(cursor, telegram_id)
    user = cursor.execute("""
        SELECT u.id FROM users AS u JOIN deletion_requests AS request ON request.user_id = u.id
        WHERE request.token = %s AND request.expires_at >= clock_timestamp()
            AND u.telegram_user_id = %s AND (u.dm_chat_id = %s OR u.dm_chat_id IS NULL)
        FOR UPDATE OF u, request
    """, (token, telegram_id, chat_id)).fetchone()
    if not user:
        return False
    # Commit the deletion intent outside the database snapshot before deleting the account.
    with closing(open_ledger(ledger_path())) as ledger, ledger:
        ledger.execute("INSERT OR IGNORE INTO deletions (user_id) VALUES (?)", (str(user["id"]),))
    return erase_user(cursor, user["id"])


def erase_user(cursor: Cursor, user_id: UUID) -> bool:
    actor = cursor.execute("SELECT telegram_user_id FROM users WHERE id = %s", (user_id,)).fetchone()
    if not actor:
        return False
    lock_account(cursor, actor["telegram_user_id"])
    user = cursor.execute("""
        SELECT telegram_user_id, dm_chat_id FROM users WHERE id = %s FOR UPDATE
    """, (user_id,)).fetchone()
    if not user:
        return False
    telegram_id = str(user["telegram_user_id"])
    cursor.execute("""
        UPDATE telegram_updates SET payload = NULL, state = 'processed'
        WHERE payload #>> '{message,from,id}' = %s OR payload #>> '{callback_query,from,id}' = %s
    """, (telegram_id, telegram_id))
    cursor.execute("""
        WITH owned AS (
            SELECT a.id AS assignment_id, p.id AS attempt_id
            FROM memberships AS m JOIN assignments AS a ON a.member_id = m.id
            LEFT JOIN attempts AS p ON p.assignment_id = a.id WHERE m.user_id = %s
        )
        DELETE FROM jobs WHERE id IN (SELECT j.id FROM jobs AS j WHERE j.payload->>'chat_id' IN (%s, %s)
            OR j.payload->>'chatId' IN (%s, %s)
            OR j.payload->>'memberId' IN (SELECT id::text FROM memberships WHERE user_id = %s)
            OR j.payload->>'assignmentId' IN (SELECT assignment_id::text FROM owned)
            OR j.payload->>'attemptId' IN (SELECT attempt_id::text FROM owned)
            FOR UPDATE OF j SKIP LOCKED)
    """, (user_id, telegram_id, str(user["dm_chat_id"] or telegram_id),
          telegram_id, str(user["dm_chat_id"] or telegram_id), user_id))
    cursor.execute("""
        UPDATE group_days AS day SET summary = jsonb_set(day.summary, '{rating}', (
            SELECT COALESCE(jsonb_agg(ranked.entry ORDER BY ranked.position), '[]'::jsonb)
            FROM jsonb_array_elements(day.summary->'rating') WITH ORDINALITY AS ranked(entry, position)
            WHERE NOT EXISTS (
                SELECT FROM memberships AS m WHERE m.user_id = %s AND m.id::text = ranked.entry->>'memberId'
            )
        ))
        WHERE EXISTS (
            SELECT FROM courses AS c JOIN memberships AS m ON m.group_id = c.group_id
            WHERE c.id = day.course_id AND m.user_id = %s
        ) AND jsonb_typeof(day.summary->'rating') = 'array'
    """, (user_id, user_id))
    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return True


def reapply_deletions(connection: Connection, path: Path, *, required: bool = False) -> int:
    if not path.is_file():
        if required:
            raise FileNotFoundError("The current deletion ledger is required for restoration")
        return 0
    with closing(open_ledger(path)) as ledger:
        ids = [UUID(row[0]) for row in ledger.execute("SELECT user_id FROM deletions")]
    erased = 0
    for user_id in ids:
        with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
            erased += erase_user(cursor, user_id)
    return erased


def trim_ledger(path: Path) -> None:
    if path.is_file():
        with closing(open_ledger(path)) as ledger, ledger:
            ledger.execute("DELETE FROM deletions WHERE confirmed_at < unixepoch() - 7 * 86400")
