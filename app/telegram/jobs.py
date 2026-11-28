from datetime import datetime

from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.operations import mark_delivered, record_error
from app.privacy import lock_account
from app.queue import Job, claim_job
from app.telegram.api import BotAPI, TelegramError
from app.telegram.delivery import ready_delivery
from app.telegram.onboarding import apply_action, parse_command, prepare_action, reply
from app.telegram.rate_limit import block_chat, pause_sends, reachable_chat, reserve_send, restore_chat
from app.telegram.study import handle_study_event, show_today, study_event


TELEGRAM_JOB_TYPES = (
    "telegram_update", "telegram_reply", "telegram_callback", "telegram_prompt",
    "deliver_morning", "deliver_daily_portion", "deliver_digest", "deliver_grade", "deliver_grade_delay",
    "deliver_answer_ack",
)
MAX_TRIES = 3


def claim_telegram_job(connection: Connection) -> Job | None:
    with connection.cursor(row_factory=dict_row) as cursor:
        paused = cursor.execute("""
            SELECT next_send_at > clock_timestamp() AS paused FROM telegram_send_limits WHERE chat_id = 0
        """).fetchone()["paused"]
    # Updates and callback acknowledgements can proceed during a sendMessage cooldown.
    types = ("telegram_update", "telegram_callback") if paused else TELEGRAM_JOB_TYPES
    return claim_job(connection, types, 90)


def leased_job(cursor: Cursor, job: Job, *, lock: bool = True) -> dict | None:
    query = """
        SELECT tries, result FROM jobs WHERE id = %s AND state = 'running'
            AND lease_until = %s AND lease_until > clock_timestamp()
    """
    if lock:
        query += " FOR UPDATE"
    return cursor.execute(query, (job.id, job.lease_until)).fetchone()


def finish_job(cursor: Cursor, job: Job, state: str, result: dict, *, clear_payload: bool = False) -> str:
    if "error" in result:
        record_error(cursor, job.id, "telegram", str(result["error"] or "UNAVAILABLE"))
    cursor.execute("""
        UPDATE jobs SET state = %s, lease_until = NULL, result = %s,
            payload = CASE WHEN %s THEN '{}'::jsonb ELSE payload END
        WHERE id = %s
    """, (state, Jsonb(result), clear_payload, job.id))
    return state


def defer_job(cursor: Cursor, job: Job, error: TelegramError, *, retry_at: datetime | None = None) -> str:
    record_error(cursor, job.id, "telegram", str(error.code or "UNAVAILABLE"))
    cursor.execute("""
        UPDATE jobs SET state = 'queued', lease_until = NULL, result = NULL,
            run_at = COALESCE(%s, clock_timestamp() + make_interval(secs => %s))
        WHERE id = %s
    """, (retry_at, error.retry_after or 5, job.id))
    return "queued"


def handle_update_job(connection: Connection, job: Job, bot: BotAPI) -> str:
    update_id = job.payload.get("updateId") if isinstance(job.payload, dict) else None
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = leased_job(cursor, job, lock=False)
        if current is None:
            return "lease_lost"
        update = cursor.execute("""
            SELECT payload, received_at FROM telegram_updates WHERE update_id = %s AND state = 'received'
        """, (update_id,)).fetchone() if type(update_id) is int else None

    command = parse_command(update["payload"]) if update else None
    event = study_event(update["payload"]) if update and command is None and isinstance(update["payload"], dict) else None
    action, error = None, None
    try:
        if command is not None:
            action = prepare_action(connection, bot, command)
        elif event and event.addressed_to and event.addressed_to.casefold() != bot.username.casefold():
            event = None
    except TelegramError as failure:
        error = failure

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = leased_job(cursor, job)
        if current is None:
            return "lease_lost"
        if actor := command or event:
            lock_account(cursor, actor.user_id)
        if update is None or cursor.execute("""
            SELECT 1 FROM telegram_updates WHERE update_id = %s AND state = 'received'
        """, (update_id,)).fetchone() is None:
            return finish_job(cursor, job, "done", {"skipped": "update_unavailable"})
        chat_id = reachable_chat(update["payload"])
        if action is not None and action.kind == "issue_bind":
            chat_id = command.chat_id
        if chat_id:
            restore_chat(cursor, chat_id, update["received_at"])
        if error is not None and error.transient and current["tries"] < MAX_TRIES:
            return defer_job(cursor, job, error)
        if action is not None:
            group = apply_action(cursor, update_id, command, action)
            if group is not None:
                show_today(cursor, update_id, command.user_id, command.chat_id, group_id=group)
        elif event is not None and error is None:
            handle_study_event(cursor, update_id, event, update["received_at"])
        if error is not None:
            target = command or event
            reply(cursor, update_id, target.chat_id, "Не удалось обработать запрос. Попробуйте снова позже.")
        cursor.execute("""
            UPDATE telegram_updates SET state = %s, payload = NULL WHERE update_id = %s
        """, ("failed" if error is not None else "processed", update_id))
        return finish_job(cursor, job, "failed" if error is not None else "done",
                          {"error": error.code} if error is not None else {})


def handle_reply_job(connection: Connection, job: Job, bot: BotAPI, *, pace: bool = False) -> str:
    username = ""
    if job.type == "deliver_morning":
        try:
            username = bot.username
        except TelegramError as failure:
            with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
                current = leased_job(cursor, job)
                if current is None:
                    return "lease_lost"
                if current["result"] == {"sending": True}:
                    return finish_job(cursor, job, "delivery_unknown", {}, clear_payload=True)
                if failure.transient and current["tries"] < MAX_TRIES:
                    return defer_job(cursor, job, failure)
                return finish_job(cursor, job, "failed", {"error": failure.code}, clear_payload=True)
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = leased_job(cursor, job)
        if current is None:
            return "lease_lost"
        if current["result"] == {"sending": True}:
            return finish_job(cursor, job, "delivery_unknown", {}, clear_payload=True)
        outbound = ready_delivery(cursor, job, username)
        if outbound is None:
            return finish_job(cursor, job, "done", {"skipped": "delivery_unavailable"}, clear_payload=True)
        chat_id = outbound.payload.get("chat_id") if job.type != "telegram_callback" else None
        if chat_id:
            blocked, retry_at = reserve_send(cursor, chat_id, pace=pace)
            if blocked:
                return finish_job(cursor, job, "done", {"skipped": "chat_unreachable"}, clear_payload=True)
            if retry_at is not None:
                # Waiting for a send slot did not make a Telegram request.
                cursor.execute("""
                    UPDATE jobs SET state = 'queued', lease_until = NULL, run_at = %s, tries = tries - 1
                    WHERE id = %s
                """, (retry_at, job.id))
                return "queued"
        send_started_at = cursor.execute("""
            UPDATE jobs SET result = %s WHERE id = %s RETURNING clock_timestamp() AS started_at
        """, (Jsonb({"sending": True}), job.id)).fetchone()["started_at"]

    message_id, error = None, None
    try:
        if job.type == "telegram_callback":
            bot.answer_callback(outbound.payload)
        else:
            message_id = bot.send(outbound.payload)
    except TelegramError as failure:
        error = failure

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = leased_job(cursor, job)
        if current is None:
            return "lease_lost"
        if error is None:
            if outbound.attempt_id is not None and outbound.receipt is not None:
                mark_delivered(cursor, outbound.attempt_id, outbound.receipt)
            if outbound.prompt_id is not None:
                cursor.execute("""
                    UPDATE assignments SET dm_message_id = %s WHERE id = %s AND state = 'open'
                        AND dm_message_id IS NULL
                """, (message_id, outbound.prompt_id))
            result = {"messageId": message_id} if message_id is not None else {}
            return finish_job(cursor, job, "done", result, clear_payload=True)
        if error.code == 429:
            retry_at = pause_sends(cursor, error.retry_after or 5) if chat_id else None
            if current["tries"] < MAX_TRIES:
                return defer_job(cursor, job, error, retry_at=retry_at)
        if error.code == 403 and chat_id:
            block_chat(cursor, chat_id, send_started_at)
        state = "delivery_unknown" if error.code != 429 and error.transient else "failed"
        return finish_job(cursor, job, state, {"error": error.code}, clear_payload=True)
