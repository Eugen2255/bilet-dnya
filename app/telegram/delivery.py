from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Cursor

from app.queue import Job
from app.reviews import APPEAL_WINDOW
from app.schedule import local_time_utc
from app.telegram.onboarding import issue_join_button
from app.telegram.study import portion_forecast, portion_markup


@dataclass(frozen=True)
class Outbound:
    payload: dict
    prompt_id: UUID | None = None
    attempt_id: UUID | None = None
    receipt: str | None = None


def ready_delivery(cursor: Cursor, job: Job, username: str = "") -> Outbound | None:
    if not isinstance(job.payload, dict):
        return None
    payload = job.payload
    if job.type == "telegram_reply":
        return Outbound(payload) if type(payload.get("chat_id")) is int else None
    if job.type == "telegram_callback":
        return Outbound(payload) if isinstance(payload.get("callback_query_id"), str) else None
    try:
        if job.type == "deliver_answer_ack":
            attempt_id = UUID(payload["attemptId"])
            row = cursor.execute("""
                SELECT u.dm_chat_id FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
                JOIN memberships AS m ON m.id = a.member_id JOIN users AS u ON u.id = m.user_id
                WHERE p.id = %s AND m.state = 'active' AND u.dm_chat_id IS NOT NULL
                    AND p.body IS NOT NULL AND p.acknowledged_at IS NULL
            """, (attempt_id,)).fetchone()
            return Outbound({"chat_id": row["dm_chat_id"], "text": "Ответ принят, проверяем."},
                            attempt_id=attempt_id, receipt="acknowledgement") if row else None
        if job.type == "deliver_grade":
            return grade_message(cursor, UUID(payload["attemptId"]), payload.get("expectedStatus"))
        if job.type == "deliver_grade_delay":
            return grade_message(cursor, UUID(payload["attemptId"]), "submitted")
        if job.type == "telegram_prompt":
            row = open_assignment(cursor, UUID(payload["assignmentId"]))
            if row and row["dm_message_id"] is None:
                return Outbound({
                    "chat_id": row["dm_chat_id"],
                    "text": f"Билет {row['ordinal']}. {row['question']}\n\nОтветьте на это сообщение в 2–3 предложениях.",
                    "reply_markup": {"force_reply": True, "input_field_placeholder": "Ваш ответ"},
                }, prompt_id=row["id"])
            return None
        return scheduled_message(cursor, job.type, payload, username)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def open_assignment(cursor: Cursor, assignment_id: UUID) -> dict | None:
    return cursor.execute("""
        SELECT a.id, a.course_id, a.day, a.dm_message_id, m.id AS member_id, u.dm_chat_id,
            t.question, t.ordinal
        FROM assignments AS a JOIN memberships AS m ON m.id = a.member_id
        JOIN courses AS c ON c.id = a.course_id JOIN users AS u ON u.id = m.user_id
        JOIN tickets AS t ON t.id = a.ticket_id JOIN rubrics AS r ON r.id = a.rubric_id
        WHERE a.id = %s AND a.state = 'open' AND m.state = 'active' AND u.dm_chat_id IS NOT NULL
            AND c.state = 'active' AND r.state = 'approved'
            AND a.day = (statement_timestamp() AT TIME ZONE c.timezone)::date
    """, (assignment_id,)).fetchone()


def scheduled_message(cursor: Cursor, kind: str, payload: dict, username: str) -> Outbound | None:
    expires = datetime.fromisoformat(payload["expiresAt"])
    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
        return None
    course_id, group_day_id = UUID(payload["courseId"]), UUID(payload["groupDayId"])
    row = cursor.execute("""
        SELECT d.day, d.summary, c.group_id, c.timezone, g.telegram_chat_id, t.question, r.state AS rubric_state
        FROM group_days AS d JOIN courses AS c ON c.id = d.course_id
        JOIN study_groups AS g ON g.id = c.group_id
        LEFT JOIN tickets AS t ON t.id = d.ticket_id
        LEFT JOIN rubrics AS r ON r.id = t.approved_rubric_id
        WHERE d.id = %s AND d.course_id = %s AND c.state = 'active'
            AND d.day = (statement_timestamp() AT TIME ZONE c.timezone)::date
    """, (group_day_id, course_id)).fetchone()
    if not row or payload.get("day") != row["day"].isoformat():
        return None

    if kind == "deliver_daily_portion":
        member_id = UUID(payload["memberId"])
        chat_id = payload.get("chatId")
        member = cursor.execute("""
            SELECT 1 FROM memberships AS m JOIN users AS u ON u.id = m.user_id
            WHERE m.id = %s AND m.group_id = %s AND m.state = 'active' AND m.role = 'student'
                AND u.dm_chat_id = %s
        """, (member_id, row["group_id"], chat_id)).fetchone()
        if not member:
            return None
        assignments = cursor.execute("""
            SELECT a.id, t.ordinal, t.question FROM assignments AS a
            JOIN tickets AS t ON t.id = a.ticket_id JOIN rubrics AS r ON r.id = a.rubric_id
            WHERE a.course_id = %s AND a.member_id = %s AND a.day = %s
                AND a.state = 'open' AND r.state = 'approved'
            ORDER BY CASE a.kind WHEN 'repeat' THEN 0 ELSE 1 END, t.ordinal
        """, (course_id, member_id, row["day"])).fetchall()
        plan = cursor.execute("""
            SELECT daily_target, assigned_count, projected_missing FROM daily_portions
            WHERE course_id = %s AND member_id = %s AND day = %s
        """, (course_id, member_id, row["day"])).fetchone()
        forecast = portion_forecast(plan) if plan else ""
        if not assignments and not forecast:
            return None
        text = "Доступна порция на сегодня. Выберите билет." if assignments else "На сегодня пока нет доступных билетов."
        message = {"chat_id": chat_id, "text": f"{text}\n\n{forecast}" if forecast else text}
        if assignments:
            message["reply_markup"] = portion_markup(assignments)
        return Outbound(message)

    chat_id = row["telegram_chat_id"]
    if chat_id != payload.get("chatId"):
        return None
    if kind == "deliver_morning" and row["question"] and row["rubric_state"] == "approved" and username:
        midnight = local_time_utc(row["day"] + timedelta(days=1), time.min, ZoneInfo(row["timezone"]))
        button = issue_join_button(cursor, row["group_id"], username, midnight)
        return Outbound({"chat_id": chat_id, "text": f"Билет дня\n\n{row['question']}", "reply_markup": button})
    if kind == "deliver_digest" and row["summary"]:
        consenting = {str(member["id"]) for member in cursor.execute("""
            SELECT id FROM memberships WHERE group_id = %s AND state = 'active'
                AND role = 'student' AND rank_opt_in
        """, (row["group_id"],)).fetchall()}
        return Outbound({"chat_id": chat_id, "text": format_digest(row["summary"], consenting)})
    return None


def format_digest(summary: dict, consenting: set[str]) -> str:
    text = f"Итоги дня\n\nУчастников ответило: {summary['responded']} из {summary['active']}."
    if summary["meanScore"] is None:
        return text + "\nПроверенных ответов пока нет."
    text += f"\nПроверено ответов: {summary['graded']}.\nСредний балл: {summary['meanScore']:g}/5."
    rating = [row for row in summary.get("rating", []) if row["memberId"] in consenting]
    if rating:
        text += "\n\nРейтинг\n" + "\n".join(
            f"{i}. {row['alias']} · дней: {row['days']} · {row['meanScore']:g}/5"
            for i, row in enumerate(rating, 1)
        )
    return text[:4000]


def grade_message(cursor: Cursor, attempt_id: UUID, expected_status: str | None) -> Outbound | None:
    row = cursor.execute("""
        SELECT p.status, COALESCE(appeal.result, p.result) AS result, p.body IS NOT NULL AS can_retry,
            u.dm_chat_id, t.ordinal, m.role, p.graded_at, appeal.state AS appeal_state,
            r.answer, r.points, r.state AS rubric_state, statement_timestamp() AS read_at
        FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
        JOIN memberships AS m ON m.id = a.member_id JOIN users AS u ON u.id = m.user_id
        JOIN tickets AS t ON t.id = a.ticket_id JOIN rubrics AS r ON r.id = a.rubric_id
        LEFT JOIN appeals AS appeal ON appeal.attempt_id = p.id
        WHERE p.id = %s AND m.state = 'active' AND u.dm_chat_id IS NOT NULL
    """, (attempt_id,)).fetchone()
    if not row or (expected_status and expected_status != row["status"]):
        return None
    payload = {"chat_id": row["dm_chat_id"]}
    if row["status"] == "submitted" and expected_status == "submitted":
        payload["text"] = "Проверка задерживается. Ответ сохранён."
    elif row["status"] == "failed":
        payload["text"] = "Не удалось проверить ответ. Ответ сохранён."
        if row["can_retry"]:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": "Повторить проверку", "callback_data": f"retry:{attempt_id}"}
            ]]}
    elif row["status"] == "graded" and isinstance(row["result"], dict):
        heading = {
            "queued": "Апелляция на проверке.",
            "succeeded": "Апелляция рассмотрена. Действующая оценка ниже.",
            "failed": "Повторная проверка завершилась с ошибкой. Исходная оценка сохранена.",
        }.get(row["appeal_state"])
        payload["text"] = "\n\n".join(filter(None, (heading, format_grade(row))))[:4000]
        if row["role"] == "student":
            buttons = []
            if (not row["appeal_state"] and row["can_retry"] and row["graded_at"]
                    and row["graded_at"] <= row["read_at"] <= row["graded_at"] + APPEAL_WINDOW):
                buttons.append([{"text": "Оспорить оценку", "callback_data": f"appeal:{attempt_id}"}])
            if row["rubric_state"] == "approved":
                buttons.append([{"text": "Пожаловаться на эталон", "callback_data": f"report:{attempt_id}"}])
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
    else:
        return None
    return Outbound(payload, attempt_id=attempt_id, receipt="grade" if row["status"] == "graded" else None)


def format_grade(row: dict) -> str:
    result = row["result"]
    lines = [f"Билет {row['ordinal']}. Оценка {result['score']}/5"]
    if row["rubric_state"] == "revoked":
        return lines[0] + "\n\nЭталон проверяет преподаватель. Доступные билеты — /today."
    points = {point["id"]: point["text"] for point in row["points"]}
    for title, field in (("Засчитано", "coveredIds"), ("Упущено", "missingIds")):
        entries = [points[point_id] for point_id in result.get(field, []) if point_id in points]
        if entries:
            lines.append(f"{title}\n" + "\n".join(f"• {text}" for text in entries))
    if result.get("errors"):
        lines.append("Фактические ошибки\n" + "\n".join(f"• {error}" for error in result["errors"]))
    if result.get("feedback"):
        lines.append(result["feedback"])
    if row["rubric_state"] == "approved":
        lines.append(f"Эталон\n{row['answer']}")
    return "\n\n".join(lines)[:4000]
