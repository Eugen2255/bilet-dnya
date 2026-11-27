from datetime import datetime, timedelta
from uuid import UUID

from psycopg import Cursor

from app.queue import enqueue_delivery


APPEAL_WINDOW = timedelta(hours=24)
REPORT_THRESHOLD = 3


def request_appeal(cursor: Cursor, attempt_id: UUID, user_id: int, chat_id: int, received_at: datetime) -> str:
    attempt = student_attempt(cursor, attempt_id, user_id, chat_id)
    if attempt is None:
        return "Апелляция недоступна."
    if attempt["appeal_state"]:
        return {
            "queued": "Апелляция уже на проверке.",
            "succeeded": "Апелляция уже рассмотрена. Результат доступен через /today.",
            "failed": "Повторная проверка завершилась с ошибкой. Исходная оценка сохранена.",
        }[attempt["appeal_state"]]
    graded_at = attempt["graded_at"]
    if not attempt["has_body"] or graded_at is None or not graded_at <= received_at <= graded_at + APPEAL_WINDOW:
        return "Апелляция доступна в течение 24 часов после оценки."
    inserted = cursor.execute("""
        INSERT INTO appeals (attempt_id, created_at) VALUES (%s, %s)
        ON CONFLICT DO NOTHING RETURNING attempt_id
    """, (attempt_id, received_at)).fetchone()
    if inserted is None:
        return "Апелляция уже принята."
    enqueue_delivery(cursor, f"appeal:{attempt_id}", "appeal", {"attemptId": str(attempt_id)})
    return "Апелляция принята. Сообщим результат после повторной проверки."


def report_rubric(cursor: Cursor, attempt_id: UUID, user_id: int, chat_id: int, received_at: datetime) -> str:
    attempt = student_attempt(cursor, attempt_id, user_id, chat_id)
    if attempt is None:
        return "Жалоба доступна после получения оценки за свой ответ."

    # Serialize reports and approvals for the ticket, including reports on older versions.
    ticket = cursor.execute("""
        SELECT approved_rubric_id FROM tickets WHERE id = %s FOR NO KEY UPDATE
    """, (attempt["ticket_id"],)).fetchone()
    if ticket is None:
        return "Эталон недоступен."
    rubric = cursor.execute("SELECT state FROM rubrics WHERE id = %s", (attempt["rubric_id"],)).fetchone()
    if rubric is None or rubric["state"] != "approved":
        return "Эталон проверяет преподаватель. Откройте доступные билеты через /today."
    added = cursor.execute("""
        INSERT INTO rubric_reports (rubric_id, member_id, created_at) VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING RETURNING rubric_id
    """, (attempt["rubric_id"], attempt["member_id"], received_at)).fetchone()
    if added is None:
        return "Вы уже отправили жалобу на эту версию эталона."
    count = cursor.execute("SELECT count(*) AS total FROM rubric_reports WHERE rubric_id = %s",
                           (attempt["rubric_id"],)).fetchone()["total"]
    if count < REPORT_THRESHOLD:
        return "Жалоба на эталон принята."

    cursor.execute("UPDATE rubrics SET state = 'revoked' WHERE id = %s", (attempt["rubric_id"],))
    cursor.execute("""
        UPDATE assignments SET state = 'revoked' WHERE rubric_id = %s AND state = 'open'
    """, (attempt["rubric_id"],))
    if ticket["approved_rubric_id"] == attempt["rubric_id"]:
        cursor.execute("UPDATE tickets SET approved_rubric_id = NULL, state = 'revoked' WHERE id = %s",
                       (attempt["ticket_id"],))
    elif ticket["approved_rubric_id"] is not None:
        reopen_revoked_assignments(cursor, attempt["ticket_id"], ticket["approved_rubric_id"])
    return "Эталон приостановлен. Откройте доступные билеты через /today."


def reopen_revoked_assignments(cursor: Cursor, ticket_id: UUID, rubric_id: UUID) -> None:
    cursor.execute("""
        UPDATE assignments AS a SET rubric_id = %s, state = 'open'
        FROM courses AS c, rubrics AS old, memberships AS m
        WHERE a.ticket_id = %s AND a.state IN ('open', 'revoked') AND a.course_id = c.id
            AND old.id = a.rubric_id AND old.state = 'revoked' AND m.id = a.member_id
            AND m.state = 'active' AND m.role = 'student' AND c.state IN ('active', 'paused')
            AND a.day = (statement_timestamp() AT TIME ZONE c.timezone)::date AND a.day < c.exam_date
            AND NOT EXISTS (SELECT 1 FROM attempts AS p WHERE p.assignment_id = a.id)
    """, (rubric_id, ticket_id))


def student_attempt(cursor: Cursor, attempt_id: UUID, user_id: int, chat_id: int) -> dict | None:
    return cursor.execute("""
        SELECT a.ticket_id, a.rubric_id, a.member_id, p.graded_at, p.body IS NOT NULL AS has_body,
            appeal.state AS appeal_state
        FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
        JOIN memberships AS m ON m.id = a.member_id JOIN users AS u ON u.id = m.user_id
        LEFT JOIN appeals AS appeal ON appeal.attempt_id = p.id
        WHERE p.id = %s AND u.telegram_user_id = %s AND u.dm_chat_id = %s
            AND m.state = 'active' AND m.role = 'student' AND p.status = 'graded' AND p.result IS NOT NULL
    """, (attempt_id, user_id, chat_id)).fetchone()
