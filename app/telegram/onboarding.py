import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

from psycopg import Connection, Cursor
from psycopg.rows import dict_row

from app.queue import enqueue_delivery
from app.telegram.api import BotAPI


ADMINS = {"administrator", "creator"}
INVALID_LINK = "Ссылка недействительна. Откройте свежую ссылку из чата группы."
NOT_MEMBER = "Подключиться могут только участники этой группы."


@dataclass(frozen=True)
class Command:
    name: str
    addressed_to: str
    argument: str
    chat_id: int
    chat_type: str
    user_id: int


@dataclass(frozen=True)
class Action:
    kind: str
    text: str = ""
    username: str = ""
    token_hash: str = ""


def parse_command(update: object) -> Command | None:
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return None
    chat, user, text = message.get("chat"), message.get("from"), message.get("text")
    if not isinstance(chat, dict) or not isinstance(user, dict) or not isinstance(text, str):
        return None
    if type(chat.get("id")) is not int or type(user.get("id")) is not int or user.get("is_bot"):
        return None
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    name, _, addressed_to = parts[0].partition("@")
    if name not in {"/bind", "/start"}:
        return None
    chat_type = chat.get("type")
    if chat_type not in ("group", "supergroup", "private"):
        return None
    return Command(name, addressed_to, parts[1] if len(parts) == 2 else "", chat["id"], chat_type, user["id"])


def prepare_action(connection: Connection, bot: BotAPI, command: Command) -> Action:
    if command.addressed_to and command.addressed_to.casefold() != bot.username.casefold():
        return Action("ignore")
    if command.name == "/bind":
        if command.chat_type == "private":
            return Action("reply", "Отправьте /bind в чате группы.")
        if bot.get_member(command.chat_id, command.user_id)["status"] not in ADMINS:
            return Action("reply", "Привязать группу может её администратор.")
        return Action("issue_bind", username=bot.username)

    if command.chat_type != "private":
        return Action("ignore")
    if not command.argument:
        return Action("reply", "Откройте кнопку «Ответить» в группе, чтобы подключиться.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", command.argument):
        return Action("reply", INVALID_LINK)
    digest = token_hash(command.argument)
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        link = find_link(cursor, digest, command.user_id)
    if link is None:
        return Action("reply", INVALID_LINK)

    member = bot.get_member(link["telegram_chat_id"], command.user_id)
    status = member["status"]
    if link["kind"] == "bind":
        if status not in ADMINS:
            return Action("reply", "Подтвердить привязку может администратор группы.")
        return Action("bind", username=bot.username, token_hash=digest)
    if status in ADMINS or status == "member" or (status == "restricted" and member.get("is_member") is True):
        return Action("join", token_hash=digest)
    return Action("reply", NOT_MEMBER)


def find_link(cursor: Cursor, digest: str, user_id: int, *, lock: bool = False) -> dict | None:
    query = """
        SELECT token.kind, token.group_id, token.member_id, study_groups.telegram_chat_id
        FROM action_tokens AS token
        JOIN study_groups ON study_groups.id = token.group_id
        LEFT JOIN memberships AS member ON member.id = token.member_id
        LEFT JOIN users ON users.id = member.user_id
        WHERE token.token_hash = %s AND token.consumed_at IS NULL AND token.expires_at > clock_timestamp()
            AND (
                (token.kind = 'bind' AND users.telegram_user_id = %s)
                OR (token.kind = 'join' AND EXISTS (
                    SELECT 1 FROM memberships AS organizer
                    WHERE organizer.group_id = token.group_id AND organizer.role = 'organizer'
                        AND organizer.state = 'active'
                ))
            )
    """
    if lock:
        query += " FOR UPDATE OF token"
    return cursor.execute(query, (digest, user_id)).fetchone()


def token_hash(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def issue_button(
    cursor: Cursor, group_id: UUID, username: str, title: str, expires_at: datetime,
    kind: str, member_id: UUID | None = None,
) -> dict:
    token = secrets.token_urlsafe(24)
    cursor.execute("""
        INSERT INTO action_tokens (token_hash, kind, group_id, member_id, expires_at)
        VALUES (%s, %s, %s, %s, %s)
    """, (token_hash(token), kind, group_id, member_id, expires_at))
    return {"inline_keyboard": [[{"text": title, "url": f"https://t.me/{username}?start={token}"}]]}


def issue_join_button(cursor: Cursor, group_id: UUID, username: str, expires_at: datetime) -> dict:
    return issue_button(cursor, group_id, username, "Ответить", expires_at, "join")


def store_user(cursor: Cursor, user_id: int, dm_chat_id: int | None) -> UUID:
    return cursor.execute("""
        INSERT INTO users (telegram_user_id, dm_chat_id) VALUES (%s, %s)
        ON CONFLICT (telegram_user_id) DO UPDATE
        SET dm_chat_id = COALESCE(EXCLUDED.dm_chat_id, users.dm_chat_id)
        RETURNING id
    """, (user_id, dm_chat_id)).fetchone()["id"]


def reply(cursor: Cursor, update_id: int, chat_id: int, text: str, button: dict | None = None, slot: str = "reply"):
    payload = {"chat_id": chat_id, "text": text}
    if button is not None:
        payload["reply_markup"] = button
    enqueue_delivery(cursor, f"telegram_reply:{update_id}:{slot}", "telegram_reply", payload)


def apply_action(cursor: Cursor, update_id: int, command: Command, action: Action) -> None:
    if action.kind == "ignore":
        return
    if action.kind == "reply":
        reply(cursor, update_id, command.chat_id, action.text)
        return

    if action.kind == "issue_bind":
        group = cursor.execute("""
            INSERT INTO study_groups (telegram_chat_id) VALUES (%s)
            ON CONFLICT (telegram_chat_id) DO UPDATE SET telegram_chat_id = EXCLUDED.telegram_chat_id
            RETURNING id
        """, (command.chat_id,)).fetchone()["id"]
        user = store_user(cursor, command.user_id, None)
        member = cursor.execute("""
            INSERT INTO memberships (group_id, user_id, role, state, alias)
            VALUES (%s, %s, 'organizer', 'pending', 'Организатор')
            ON CONFLICT (group_id, user_id) DO UPDATE SET user_id = EXCLUDED.user_id
            RETURNING id
        """, (group, user)).fetchone()["id"]
        expires = datetime.now(timezone.utc) + timedelta(minutes=15)
        button = issue_button(cursor, group, action.username, "Подтвердить привязку", expires, "bind", member)
        reply(cursor, update_id, command.chat_id, "Администратор может завершить привязку в личном чате.", button)
        return

    link = find_link(cursor, action.token_hash, command.user_id, lock=True)
    if link is None or link["kind"] != action.kind:
        reply(cursor, update_id, command.chat_id, INVALID_LINK)
        return

    user = store_user(cursor, command.user_id, command.chat_id)
    if action.kind == "bind":
        cursor.execute("""
            UPDATE action_tokens SET consumed_at = clock_timestamp() WHERE token_hash = %s
        """, (action.token_hash,))
        cursor.execute("""
            UPDATE memberships SET role = 'organizer', state = 'active' WHERE id = %s
        """, (link["member_id"],))
        button = issue_join_button(cursor, link["group_id"], action.username,
                                   datetime.now(timezone.utc) + timedelta(days=1))
        reply(cursor, update_id, link["telegram_chat_id"], "Группа подключена. Откройте бота, чтобы участвовать.",
              button, slot="group")
        reply(cursor, update_id, command.chat_id, "Привязка завершена. Вы организатор группы.")
    else:
        cursor.execute("""
            INSERT INTO memberships (group_id, user_id, role, state, alias)
            VALUES (%s, %s, 'student', 'active', %s)
            ON CONFLICT (group_id, user_id) DO UPDATE SET state = 'active'
        """, (link["group_id"], user, f"Участник-{secrets.token_hex(3)}"))
        reply(cursor, update_id, command.chat_id, "Вы подключились к группе.")
