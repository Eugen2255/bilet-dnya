from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.queue import Job
from app.telegram.api import BotAPI, TelegramError
from app.telegram.onboarding import apply_action, parse_command, prepare_action, reply


TELEGRAM_JOB_TYPES = ("telegram_update", "telegram_reply")
MAX_TRIES = 3


def leased_job(cursor: Cursor, job: Job, *, lock: bool = True) -> dict | None:
    query = """
        SELECT tries, result FROM jobs WHERE id = %s AND state = 'running'
            AND lease_until = %s AND lease_until > clock_timestamp()
    """
    if lock:
        query += " FOR UPDATE"
    return cursor.execute(query, (job.id, job.lease_until)).fetchone()


def finish_job(cursor: Cursor, job: Job, state: str, result: dict, *, clear_payload: bool = False) -> str:
    cursor.execute("""
        UPDATE jobs SET state = %s, lease_until = NULL, result = %s,
            payload = CASE WHEN %s THEN '{}'::jsonb ELSE payload END
        WHERE id = %s
    """, (state, Jsonb(result), clear_payload, job.id))
    return state


def defer_job(cursor: Cursor, job: Job, error: TelegramError) -> str:
    cursor.execute("""
        UPDATE jobs SET state = 'queued', lease_until = NULL, result = NULL,
            run_at = clock_timestamp() + make_interval(secs => %s)
        WHERE id = %s
    """, (error.retry_after or 5, job.id))
    return "queued"


def handle_update_job(connection: Connection, job: Job, bot: BotAPI) -> str:
    update_id = job.payload.get("updateId") if isinstance(job.payload, dict) else None
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = leased_job(cursor, job, lock=False)
        if current is None:
            return "lease_lost"
        update = cursor.execute("""
            SELECT payload FROM telegram_updates WHERE update_id = %s AND state = 'received'
        """, (update_id,)).fetchone() if type(update_id) is int else None

    command = parse_command(update["payload"]) if update else None
    action, error = None, None
    if command is not None:
        try:
            action = prepare_action(connection, bot, command)
        except TelegramError as failure:
            error = failure

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = leased_job(cursor, job)
        if current is None:
            return "lease_lost"
        if update is None:
            return finish_job(cursor, job, "done", {"skipped": "update_unavailable"})
        if error is not None and error.transient and current["tries"] < MAX_TRIES:
            return defer_job(cursor, job, error)
        if action is not None:
            apply_action(cursor, update_id, command, action)
        if error is not None:
            reply(cursor, update_id, command.chat_id,
                  "Не удалось проверить права в группе. Попробуйте снова и проверьте, что бот — администратор.")
        cursor.execute("""
            UPDATE telegram_updates SET state = %s, payload = NULL WHERE update_id = %s
        """, ("failed" if error is not None else "processed", update_id))
        return finish_job(cursor, job, "failed" if error is not None else "done",
                          {"error": error.code} if error is not None else {})


def handle_reply_job(connection: Connection, job: Job, bot: BotAPI) -> str:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = leased_job(cursor, job)
        if current is None:
            return "lease_lost"
        if current["result"] == {"sending": True}:
            return finish_job(cursor, job, "delivery_unknown", {}, clear_payload=True)
        if not isinstance(job.payload, dict) or type(job.payload.get("chat_id")) is not int:
            return finish_job(cursor, job, "failed", {"error": "INVALID_REPLY"}, clear_payload=True)
        cursor.execute("UPDATE jobs SET result = %s WHERE id = %s", (Jsonb({"sending": True}), job.id))

    message_id, error = None, None
    try:
        message_id = bot.send(job.payload)
    except TelegramError as failure:
        error = failure

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = leased_job(cursor, job)
        if current is None:
            return "lease_lost"
        if error is None:
            return finish_job(cursor, job, "done", {"messageId": message_id}, clear_payload=True)
        if error.code == 429 and current["tries"] < MAX_TRIES:
            return defer_job(cursor, job, error)
        state = "delivery_unknown" if error.code != 429 and error.transient else "failed"
        return finish_job(cursor, job, state, {"error": error.code}, clear_payload=True)
