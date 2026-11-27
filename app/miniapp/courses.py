from datetime import datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg import Connection
from psycopg.types.json import Jsonb

from app.miniapp.errors import ApiError
from app.practice import DAILY_LIMIT, study_day_count
from app.schedule import local_time_utc


EDITOR_ROLES = ("organizer", "teacher")
ORGANIZER = ("organizer",)
FIELDS = {
    "examDate": "exam_date", "timezone": "timezone", "morningAt": "morning_at",
    "eveningAt": "evening_at", "weekdays": "weekdays",
}


def require_member(db: Connection, user_id: UUID, group_id: UUID, roles: tuple[str, ...] = ()) -> dict:
    row = db.execute("""
        SELECT id, role FROM memberships WHERE user_id = %s AND group_id = %s AND state = 'active'
    """, (user_id, group_id)).fetchone()
    if row is None:
        raise ApiError("NOT_FOUND", "Объект недоступен.", 404)
    if roles and row["role"] not in roles:
        raise ApiError("FORBIDDEN", "Недостаточно прав для этого действия.", 403)
    return row


def related_member(db: Connection, user_id: UUID, resource: str, resource_id: UUID, roles: tuple[str, ...] = ()) -> dict:
    # All identifiers here come from fixed route definitions.
    relation = {"course": ("courses", "group_id"), "import": ("imports", "group_id")}
    if resource in relation:
        table, column = relation[resource]
        return require_member(db, user_id, resource_group(db, f"SELECT {column} FROM {table} WHERE id = %s", resource_id), roles)
    queries = {
        "ticket": "SELECT c.group_id FROM tickets AS t JOIN courses AS c ON c.id = t.course_id WHERE t.id = %s",
        "chunk": "SELECT c.group_id FROM chunks AS ch JOIN courses AS c ON c.id = ch.course_id WHERE ch.id = %s",
    }
    return require_member(db, user_id, resource_group(db, queries[resource], resource_id), roles)


def resource_group(db: Connection, query: str, resource_id: UUID) -> UUID:
    row = db.execute(query, (resource_id,)).fetchone()
    if row is None:
        raise ApiError("NOT_FOUND", "Объект недоступен.", 404)
    return row["group_id"]


def organizer_course(db: Connection, user_id: UUID, course_id: UUID) -> dict:
    course = db.execute("SELECT * FROM courses WHERE id = %s FOR NO KEY UPDATE", (course_id,)).fetchone()
    if course is None:
        raise ApiError("NOT_FOUND", "Курс недоступен.", 404)
    require_member(db, user_id, course["group_id"], ORGANIZER)
    return course


def as_course(course: dict) -> dict:
    return {
        "id": str(course["id"]), "title": course["title"], "status": course["state"],
        "examDate": course["exam_date"].isoformat() if course["exam_date"] else None,
        "timezone": course["timezone"],
        "morningAt": course["morning_at"].strftime("%H:%M") if course["morning_at"] else None,
        "eveningAt": course["evening_at"].strftime("%H:%M") if course["evening_at"] else None,
        "weekdays": course["weekdays"] or [],
    }


def groups_for(db: Connection, user_id: UUID) -> dict:
    groups = db.execute("""
        SELECT g.id, g.title, m.role
        FROM memberships AS m JOIN study_groups AS g ON g.id = m.group_id
        WHERE m.user_id = %s AND m.state = 'active' ORDER BY g.title NULLS LAST, g.id
    """, (user_id,)).fetchall()
    items = []
    for group in groups:
        courses = db.execute("""
            SELECT id, title, state FROM courses WHERE group_id = %s
                AND (state IN ('active', 'paused') OR (%s AND state = 'draft'))
            ORDER BY CASE WHEN state IN ('active', 'paused') THEN 0 ELSE 1 END, title, id
        """, (group["id"], group["role"] in EDITOR_ROLES)).fetchall()
        active = next((str(course["id"]) for course in courses if course["state"] in {"active", "paused"}), None)
        items.append({
            "id": str(group["id"]), "title": group["title"] or "Учебная группа",
            "role": group["role"], "activeCourseId": active,
            "courses": [{"id": str(course["id"]), "title": course["title"], "status": course["state"]} for course in courses],
        })
    return {"items": items}


def save_schedule(db: Connection, user_id: UUID, course_id: UUID, changes: dict) -> dict:
    with db.transaction():
        course = organizer_course(db, user_id, course_id)
        if course["state"] == "closed":
            raise ApiError("STATE_CONFLICT", "Курс закрыт.", 409)
        for key, value in changes.items():
            if value is None:
                raise ApiError("INVALID_INPUT", "Укажите значение для каждого изменяемого поля.")
            course[FIELDS[key]] = time.fromisoformat(value) if key in {"morningAt", "eveningAt"} else value
        validate_schedule(course)
        return as_course(db.execute("""
            UPDATE courses SET exam_date = %s, timezone = %s, morning_at = %s, evening_at = %s, weekdays = %s
            WHERE id = %s RETURNING *
        """, (course["exam_date"], course["timezone"], course["morning_at"], course["evening_at"],
              course["weekdays"], course_id)).fetchone())


def validate_schedule(course: dict, *, complete: bool = False) -> ZoneInfo:
    if complete and any(course[data] is None for data in FIELDS.values()):
        raise ApiError("INVALID_INPUT", "Укажите дату экзамена, время и учебные дни.")
    try:
        zone = ZoneInfo(course["timezone"] if course["timezone"] is not None else "UTC")
    except (ValueError, ZoneInfoNotFoundError):
        raise ApiError("INVALID_INPUT", "Укажите часовой пояс IANA, например Asia/Novosibirsk.") from None
    exam = course["exam_date"]
    if exam is not None and exam <= datetime.now(zone).date():
        raise ApiError("INVALID_INPUT", "Дата экзамена должна быть в будущем.")
    morning, evening = course["morning_at"], course["evening_at"]
    if morning is not None and evening is not None and morning >= evening:
        raise ApiError("INVALID_INPUT", "Утреннее время должно быть раньше вечернего.")
    days = course["weekdays"]
    if days is not None and (not days or len(set(days)) != len(days) or any(not 1 <= day <= 7 for day in days)):
        raise ApiError("INVALID_INPUT", "Выберите учебные дни недели.")
    return zone


def remaining_days(course: dict) -> int:
    zone = validate_schedule(course, complete=True)
    now = datetime.now(timezone.utc)
    first = now.astimezone(zone).date()
    if now > local_time_utc(first, course["morning_at"], zone):
        first += timedelta(days=1)
    days = study_day_count(first, course["exam_date"], course["weekdays"])
    if days <= 0:
        raise ApiError("INVALID_INPUT", "До экзамена должен оставаться хотя бы один учебный день.")
    return days


def idempotency_key(key: str) -> str:
    if not key or len(key) > 128 or not key.isascii() or not key.isprintable():
        raise ApiError("INVALID_INPUT", "Нужен Idempotency-Key длиной до 128 символов.")
    return key


def activate(db: Connection, user_id: UUID, course_id: UUID, key: str, acknowledge: bool) -> dict:
    try:
        with db.transaction():
            course = organizer_course(db, user_id, course_id)
            previous = db.execute("""
                SELECT acknowledge_load, result FROM course_activation_requests
                WHERE course_id = %s AND user_id = %s AND key = %s
            """, (course_id, user_id, idempotency_key(key))).fetchone()
            if previous:
                if previous["acknowledge_load"] != acknowledge:
                    raise ApiError("STATE_CONFLICT", "Ключ уже использован с другими параметрами.", 409)
                return previous["result"]
            if course["state"] != "draft" or db.execute("""
                SELECT 1 FROM courses WHERE group_id = %s AND state IN ('active','paused') LIMIT 1
            """, (course["group_id"],)).fetchone():
                raise ApiError("STATE_CONFLICT", "Для запуска нужен черновик и свободная группа.", 409)
            days = remaining_days(course)
            counts = db.execute("""
                SELECT count(*) AS tickets, count(*) FILTER (WHERE r.state = 'approved') AS approved
                FROM tickets AS t LEFT JOIN rubrics AS r ON r.id = t.approved_rubric_id WHERE t.course_id = %s
            """, (course_id,)).fetchone()
            if not counts["tickets"] or counts["tickets"] != counts["approved"]:
                raise ApiError("STATE_CONFLICT", "Сначала утвердите эталон для каждого билета.", 409)
            total = 2 * counts["tickets"]
            target = (total + days - 1) // days
            capacity = min(DAILY_LIMIT * days, counts["tickets"] * min(2, days))
            if capacity < total and not acknowledge:
                raise ApiError("LOAD_ACK_REQUIRED", "До экзамена может не хватить учебных дат.", 409,
                               dailyTarget=target, projectedMissing=total-capacity)
            db.execute("UPDATE courses SET state = 'active' WHERE id = %s", (course_id,))
            result = {"status": "active", "dailyTarget": target}
            db.execute("""
                INSERT INTO course_activation_requests (course_id, user_id, key, acknowledge_load, result)
                VALUES (%s, %s, %s, %s, %s)
            """, (course_id, user_id, key, acknowledge, Jsonb(result)))
            return result
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name != "courses_one_running_per_group":
            raise
        raise ApiError("STATE_CONFLICT", "У группы уже есть запущенный курс.", 409) from None
    except psycopg.errors.CheckViolation as exc:
        code = exc.diag.constraint_name
        if code == "course_activation_rubrics":
            raise ApiError("STATE_CONFLICT", "Сначала утвердите все эталоны.", 409) from None
        if code and code.startswith("course_"):
            raise ApiError("INVALID_INPUT", "Проверьте учебные даты и расписание.") from None
        raise


def set_paused(db: Connection, user_id: UUID, course_id: UUID, pause: bool) -> dict:
    with db.transaction():
        course = organizer_course(db, user_id, course_id)
        state, before = ("paused", "active") if pause else ("active", "paused")
        if course["state"] == state:
            return {"status": state}
        if course["state"] != before:
            raise ApiError("STATE_CONFLICT", "Операция недоступна в текущем состоянии курса.", 409)
        if not pause:
            remaining_days(course)
        db.execute("UPDATE courses SET state = %s WHERE id = %s", (state, course_id))
    return {"status": state}
