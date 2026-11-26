from dataclasses import dataclass
from datetime import datetime, timedelta
from unicodedata import normalize
from uuid import UUID

from psycopg import Cursor
from psycopg.types.json import Jsonb

from app.queue import enqueue_delivery
from app.telegram.onboarding import reply


@dataclass(frozen=True)
class StudyEvent:
    kind: str
    user_id: int
    chat_id: int
    text: str = ""
    message_id: int | None = None
    callback_id: str = ""
    addressed_to: str = ""


def study_event(update: dict) -> StudyEvent | None:
    callback = update.get("callback_query")
    message = callback.get("message") if isinstance(callback, dict) else update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    sender = callback.get("from") if isinstance(callback, dict) else message.get("from")
    if (not isinstance(chat, dict) or chat.get("type") != "private" or type(chat.get("id")) is not int
            or not isinstance(sender, dict) or type(sender.get("id")) is not int or sender.get("is_bot")):
        return None
    user_id, chat_id = sender["id"], chat["id"]
    if isinstance(callback, dict):
        data, callback_id = callback.get("data"), callback.get("id")
        if isinstance(data, str) and isinstance(callback_id, str):
            return StudyEvent("callback", user_id, chat_id, text=data, callback_id=callback_id)
        return None
    target = message.get("reply_to_message")
    target_id = target.get("message_id") if isinstance(target, dict) else None
    text = message.get("text")
    if isinstance(text, str):
        command = text.strip()
        name, _, addressed_to = command.partition("@")
        if name == "/today":
            return StudyEvent("today", user_id, chat_id, addressed_to=addressed_to)
        if command.startswith("/") and target_id is None:
            return None
    return StudyEvent("answer", user_id, chat_id, text=text if isinstance(text, str) else "",
                      message_id=target_id if type(target_id) is int else None)


def portion_markup(assignments: list[dict]) -> dict:
    return {"inline_keyboard": [[
        {"text": f"Билет {row['ordinal']} · {row['question'][:42]}", "callback_data": f"ticket:{row['id']}"}
    ] for row in assignments]}


def show_today(
    cursor: Cursor, update_id: int, user_id: int, chat_id: int, *, group_id: UUID | None = None
) -> None:
    # Materialize the current shared ticket for a student who joined after the morning job ran.
    cursor.execute("""
        INSERT INTO assignments (course_id, group_id, member_id, ticket_id, day, kind, rubric_id, state)
        SELECT c.id, c.group_id, m.id, t.id, d.day, 'daily', r.id, 'open'
        FROM users AS u JOIN memberships AS m ON m.user_id = u.id
        JOIN courses AS c ON c.group_id = m.group_id
        JOIN group_days AS d ON d.course_id = c.id
            AND d.day = (statement_timestamp() AT TIME ZONE c.timezone)::date
        JOIN tickets AS t ON t.id = d.ticket_id
        JOIN rubrics AS r ON r.id = t.approved_rubric_id AND r.state = 'approved'
        WHERE u.telegram_user_id = %s AND u.dm_chat_id = %s
            AND m.state = 'active' AND m.role = 'student' AND c.state = 'active'
            AND (%s::uuid IS NULL OR m.group_id = %s)
            AND NOT EXISTS (
                SELECT 1 FROM assignments AS existing WHERE existing.course_id = c.id
                    AND existing.member_id = m.id AND existing.day = d.day
            )
        ON CONFLICT (member_id, ticket_id, day) DO NOTHING
    """, (user_id, chat_id, group_id, group_id))
    assignments = cursor.execute("""
        SELECT a.id, t.question, t.ordinal, a.state, c.state AS course_state,
            r.state AS rubric_state, p.id AS attempt_id, p.status AS attempt_status
        FROM assignments AS a JOIN courses AS c ON c.id = a.course_id
        JOIN memberships AS m ON m.id = a.member_id
        JOIN users AS u ON u.id = m.user_id
        JOIN tickets AS t ON t.id = a.ticket_id
        JOIN rubrics AS r ON r.id = a.rubric_id
        LEFT JOIN attempts AS p ON p.assignment_id = a.id
        WHERE u.telegram_user_id = %s AND u.dm_chat_id = %s AND m.state = 'active'
            AND a.day = (statement_timestamp() AT TIME ZONE c.timezone)::date
            AND (%s::uuid IS NULL OR m.group_id = %s)
        ORDER BY a.course_id, t.ordinal
    """, (user_id, chat_id, group_id, group_id)).fetchall()

    open_tickets = [
        row for row in assignments if row["state"] == "open" and row["course_state"] == "active"
        and row["rubric_state"] == "approved" and row["attempt_id"] is None
    ]
    ready = [row["attempt_id"] for row in assignments if row["attempt_status"] in {"graded", "failed"}]
    if not assignments and group_id is None:
        previous = cursor.execute("""
            SELECT p.id FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
            JOIN memberships AS m ON m.id = a.member_id JOIN users AS u ON u.id = m.user_id
            WHERE u.telegram_user_id = %s AND u.dm_chat_id = %s AND m.state = 'active'
                AND p.status IN ('graded', 'failed')
            ORDER BY p.submitted_at DESC, p.id DESC LIMIT 1
        """, (user_id, chat_id)).fetchone()
        if previous:
            ready = [previous["id"]]
    for attempt_id in ready:
        enqueue_delivery(cursor, f"today_grade:{update_id}:{attempt_id}", "deliver_grade",
                         {"attemptId": str(attempt_id)})
    if open_tickets:
        reply(cursor, update_id, chat_id, "Выберите билет.", portion_markup(open_tickets), slot="today")
    elif not ready and group_id is None:
        waiting = any(row["attempt_status"] == "submitted" for row in assignments)
        reply(cursor, update_id, chat_id,
              "Ответ принят, проверяем." if waiting else "На сегодня пока нет доступных билетов.", slot="today")


def accept_answer(cursor: Cursor, update_id: int, event: StudyEvent, received_at: datetime) -> None:
    text = normalize("NFC", event.text.strip())
    if event.message_id is None or not 1 <= len(text) <= 1000:
        reply(cursor, update_id, event.chat_id, "Выберите билет через /today и ответьте текстом до 1000 знаков.")
        return
    assignment = cursor.execute("""
        SELECT a.id, a.state FROM assignments AS a JOIN courses AS c ON c.id = a.course_id
        JOIN memberships AS m ON m.id = a.member_id JOIN users AS u ON u.id = m.user_id
        JOIN rubrics AS r ON r.id = a.rubric_id
        WHERE u.telegram_user_id = %s AND u.dm_chat_id = %s AND m.state = 'active'
            AND a.dm_message_id = %s AND c.state IN ('active', 'paused') AND r.state = 'approved'
            AND a.day = (%s::timestamptz AT TIME ZONE c.timezone)::date
        ORDER BY a.id LIMIT 1 FOR NO KEY UPDATE OF a
    """, (event.user_id, event.chat_id, event.message_id, received_at)).fetchone()
    if assignment is None:
        reply(cursor, update_id, event.chat_id, "Задание недоступно. Откройте текущую порцию через /today.")
        return
    if assignment["state"] != "open":
        reply(cursor, update_id, event.chat_id, "Ответ на этот билет уже принят. Результат доступен через /today.")
        return
    row = cursor.execute("""
        INSERT INTO attempts (assignment_id, telegram_update_id, body, submitted_at, status)
        VALUES (%s, %s, %s, %s, 'submitted') ON CONFLICT DO NOTHING RETURNING id
    """, (assignment["id"], update_id, text, received_at)).fetchone()
    if row is not None:
        cursor.execute("UPDATE assignments SET state = 'answered' WHERE id = %s", (assignment["id"],))
        attempt_id = str(row["id"])
        enqueue_delivery(cursor, f"grade:{attempt_id}", "grade", {"attemptId": attempt_id})
        cursor.execute("""
            INSERT INTO jobs (key, type, payload, run_at)
            VALUES (%s, 'deliver_grade_delay', %s, %s)
            ON CONFLICT (key) DO NOTHING
        """, (f"deliver_grade_delay:{attempt_id}", Jsonb({"attemptId": attempt_id}),
              received_at + timedelta(seconds=25)))
        reply(cursor, update_id, event.chat_id, "Ответ принят, проверяем.")


def choose_ticket(cursor: Cursor, event: StudyEvent, assignment_id: UUID) -> str:
    row = cursor.execute("""
        SELECT a.dm_message_id FROM assignments AS a JOIN courses AS c ON c.id = a.course_id
        JOIN memberships AS m ON m.id = a.member_id JOIN users AS u ON u.id = m.user_id
        JOIN rubrics AS r ON r.id = a.rubric_id
        WHERE a.id = %s AND u.telegram_user_id = %s AND u.dm_chat_id = %s AND m.state = 'active'
            AND a.state = 'open' AND c.state = 'active' AND r.state = 'approved'
            AND a.day = (statement_timestamp() AT TIME ZONE c.timezone)::date
    """, (assignment_id, event.user_id, event.chat_id)).fetchone()
    if row is None:
        return "Этот билет сейчас недоступен."
    if row["dm_message_id"] is not None:
        return "Ответьте на сообщение бота с этим билетом."
    cursor.execute("""
        INSERT INTO jobs (key, type, payload) VALUES (%s, 'telegram_prompt', %s)
        ON CONFLICT (key) DO UPDATE SET state = 'queued', run_at = clock_timestamp(), tries = 0,
            result = NULL, payload = EXCLUDED.payload
        WHERE jobs.state IN ('failed', 'delivery_unknown', 'done')
    """, (f"telegram_prompt:{assignment_id}", Jsonb({"assignmentId": str(assignment_id)})))
    return "Открываю билет."


def retry_grade(cursor: Cursor, event: StudyEvent, attempt_id: UUID) -> str:
    owned = cursor.execute("""
        SELECT 1 FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
        JOIN memberships AS m ON m.id = a.member_id JOIN users AS u ON u.id = m.user_id
        WHERE p.id = %s AND u.telegram_user_id = %s AND u.dm_chat_id = %s
            AND m.state = 'active' AND p.status = 'failed' AND p.body IS NOT NULL
    """, (attempt_id, event.user_id, event.chat_id)).fetchone()
    if not owned:
        return "Проверка недоступна для повтора."
    job = cursor.execute("SELECT id, state FROM jobs WHERE key = %s FOR UPDATE",
                         (f"grade:{attempt_id}",)).fetchone()
    if job is None or job["state"] != "failed":
        return "Проверка уже запущена."
    attempted = cursor.execute("""
        UPDATE attempts SET status = 'submitted'
        WHERE id = %s AND status = 'failed' AND body IS NOT NULL RETURNING id
    """, (attempt_id,)).fetchone()
    if not attempted:
        return "Проверка недоступна для повтора."
    cursor.execute("""
        UPDATE jobs SET state = 'queued', run_at = clock_timestamp(), tries = 0, lease_until = NULL, result = NULL
        WHERE id = %s
    """, (job["id"],))
    return "Проверка запущена повторно."


def handle_study_event(cursor: Cursor, update_id: int, event: StudyEvent, received_at: datetime) -> None:
    if event.kind == "today":
        show_today(cursor, update_id, event.user_id, event.chat_id)
    elif event.kind == "answer":
        accept_answer(cursor, update_id, event, received_at)
    else:
        kind, _, raw_id = event.text.partition(":")
        try:
            target = UUID(raw_id)
        except ValueError:
            target = None
        message = "Действие недоступно."
        if target is not None and kind == "ticket":
            message = choose_ticket(cursor, event, target)
        elif target is not None and kind == "retry":
            message = retry_grade(cursor, event, target)
        enqueue_delivery(cursor, f"telegram_callback:{update_id}", "telegram_callback",
                         {"callback_query_id": event.callback_id, "text": message})
