from datetime import datetime, timedelta

from psycopg import Cursor


GLOBAL_INTERVAL = timedelta(milliseconds=40)
PRIVATE_INTERVAL = timedelta(seconds=1)
GROUP_INTERVAL = timedelta(seconds=3)


def reserve_send(cursor: Cursor, chat_id: int, *, pace: bool) -> tuple[bool, datetime | None]:
    """Return whether the chat is blocked and when a deferred send can run."""
    # Lock the shared row first so two workers cannot reserve the same slot.
    shared = cursor.execute("""
        SELECT next_send_at FROM telegram_send_limits WHERE chat_id = 0 FOR UPDATE
    """).fetchone()["next_send_at"]
    chat = cursor.execute("""
        INSERT INTO telegram_send_limits (chat_id) VALUES (%s)
        ON CONFLICT (chat_id) DO UPDATE SET next_send_at = telegram_send_limits.next_send_at
        RETURNING blocked_at, next_send_at, clock_timestamp() AS now
    """, (chat_id,)).fetchone()
    if chat["blocked_at"] is not None:
        return True, None
    ready_at = max(shared, chat["next_send_at"])
    now = chat["now"]
    if ready_at > now:
        return False, ready_at
    if pace:
        chat_interval = GROUP_INTERVAL if chat_id < 0 else PRIVATE_INTERVAL
        cursor.execute("""
            UPDATE telegram_send_limits SET next_send_at = CASE WHEN chat_id = 0 THEN %s ELSE %s END
            WHERE chat_id IN (0, %s)
        """, (now + GLOBAL_INTERVAL, now + chat_interval, chat_id))
    return False, None


def pause_sends(cursor: Cursor, delay: int) -> datetime:
    # Telegram does not identify the scope of a 429 returned by sendMessage.
    return cursor.execute("""
        UPDATE telegram_send_limits
        SET next_send_at = GREATEST(next_send_at, clock_timestamp() + make_interval(secs => %s))
        WHERE chat_id = 0 RETURNING next_send_at
    """, (delay,)).fetchone()["next_send_at"]


def block_chat(cursor: Cursor, chat_id: int, send_started_at: datetime) -> None:
    cursor.execute("""
        INSERT INTO telegram_send_limits (chat_id, blocked_at) VALUES (%s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET blocked_at = GREATEST(telegram_send_limits.blocked_at, EXCLUDED.blocked_at)
        WHERE telegram_send_limits.last_reachable_at IS NULL
            OR telegram_send_limits.last_reachable_at < EXCLUDED.blocked_at
    """, (chat_id, send_started_at))


def restore_chat(cursor: Cursor, chat_id: int, received_at: datetime) -> None:
    cursor.execute("""
        UPDATE telegram_send_limits
        SET last_reachable_at = GREATEST(last_reachable_at, %s),
            blocked_at = CASE WHEN blocked_at <= %s THEN NULL ELSE blocked_at END
        WHERE chat_id = %s AND (last_reachable_at IS NULL OR last_reachable_at < %s)
    """, (received_at, received_at, chat_id, received_at))


def reachable_chat(update: dict) -> int | None:
    change = update.get("my_chat_member")
    if isinstance(change, dict):
        chat, member = change.get("chat"), change.get("new_chat_member")
        if (isinstance(chat, dict) and type(chat.get("id")) is int and isinstance(member, dict)
                and member.get("status") in ("member", "administrator", "creator")):
            return chat["id"]
    message = update.get("message")
    if isinstance(message, dict):
        chat, sender = message.get("chat"), message.get("from")
        if (isinstance(chat, dict) and chat.get("type") == "private" and type(chat.get("id")) is int
                and isinstance(sender, dict) and sender.get("id") == chat["id"] and not sender.get("is_bot")):
            return chat["id"]
    return None
