import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ai.digest import PUBLICATION_BUDGET, enqueue_digest
from app.practice import ensure_portion, issue_repeat, start_repeat
from app.progress import snapshot
from app.queue import Job, enqueue_delivery


SCHEDULE_TYPES = ("morning", "digest")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DayWindow:
    morning: datetime
    evening: datetime
    end: datetime


def local_time_utc(day: date, at: time, zone: ZoneInfo) -> datetime:
    """Choose the first occurrence and advance through nonexistent local times."""
    local = datetime.combine(day, at)
    while True:
        utc = local.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            return utc
        local += timedelta(minutes=1)


def day_window(course: dict, day: date, zone: ZoneInfo) -> DayWindow | None:
    if day >= course["exam_date"] or day.isoweekday() not in course["weekdays"]:
        return None
    morning = local_time_utc(day, course["morning_at"], zone)
    evening = local_time_utc(day, course["evening_at"], zone)
    end = local_time_utc(day + timedelta(days=1), time.min, zone)
    return DayWindow(morning, evening, end) if morning < evening < end else None


def close_expired_course(cursor: Cursor, course_id: UUID, zone_name: str, today: date) -> None:
    """Close the expired course if its schedule still matches the selected time zone."""
    cursor.execute("""
        WITH closed AS (
            UPDATE courses SET state = 'closed'
            WHERE id = %s AND state IN ('active', 'paused')
                AND exam_date <= %s AND timezone = %s
            RETURNING id
        )
        UPDATE repeats SET state = 'closed'
        WHERE course_id IN (SELECT id FROM closed) AND state = 'pending'
    """, (course_id, today, zone_name))


def schedule_courses(connection: Connection, now: datetime) -> None:
    """Persist jobs for the current and next teaching dates."""
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""
            UPDATE repeats AS r SET state = 'closed' FROM courses AS c
            WHERE r.course_id = c.id AND r.state = 'pending' AND c.state = 'closed'
        """)
        courses = cursor.execute("""
            SELECT id, exam_date, timezone, morning_at, evening_at, weekdays, schedule_from, state
            FROM courses WHERE state IN ('active', 'paused')
        """).fetchall()
        for course in courses:
            try:
                zone = ZoneInfo(course["timezone"])
            except (ZoneInfoNotFoundError, ValueError):
                logger.warning("Invalid schedule timezone for course %s", course["id"])
                continue
            today = now.astimezone(zone).date()
            if today >= course["exam_date"]:
                close_expired_course(cursor, course["id"], course["timezone"], today)
                continue
            if course["state"] == "paused":
                continue
            for offset in range(8):
                day = today + timedelta(days=offset)
                window = day_window(course, day, zone)
                if window is None or course["schedule_from"] > window.morning:
                    continue
                payload = Jsonb({"courseId": str(course["id"]), "day": day.isoformat()})
                for job_type, due in zip(SCHEDULE_TYPES, (window.morning, window.evening), strict=True):
                    cursor.execute("""
                        INSERT INTO jobs (key, type, payload, run_at) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (key) DO UPDATE SET run_at = EXCLUDED.run_at
                        WHERE jobs.state = 'queued' AND jobs.run_at <> EXCLUDED.run_at
                    """, (f"{job_type}:{course['id']}:{day}", job_type, payload, due))
                if offset:
                    break


def handle_schedule_job(connection: Connection, job: Job, now: datetime, *, digest_model: bool = False) -> str:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        current = cursor.execute("""
            SELECT id FROM jobs WHERE id = %s AND state = 'running'
                AND lease_until = %s AND lease_until > clock_timestamp()
            FOR UPDATE
        """, (job.id, job.lease_until)).fetchone()
        if current is None:
            return "lease_lost"

        try:
            if not isinstance(job.payload, dict) or job.type not in SCHEDULE_TYPES:
                raise ValueError
            course_id = UUID(job.payload["courseId"])
            day = date.fromisoformat(job.payload["day"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return finish_schedule(cursor, job, {"error": "INVALID_SCHEDULE_JOB"}, state="failed")

        course = cursor.execute("""
            SELECT course.*, study_groups.telegram_chat_id
            FROM courses AS course JOIN study_groups ON study_groups.id = course.group_id
            WHERE course.id = %s FOR NO KEY UPDATE OF course
        """, (course_id,)).fetchone()
        allowed_states = ("active", "closed") if job.type == "digest" else ("active",)
        if course is None or course["state"] not in allowed_states:
            return finish_schedule(cursor, job, {"skipped": "course_unavailable"})

        try:
            window = day_window(course, day, ZoneInfo(course["timezone"]))
        except (ZoneInfoNotFoundError, ValueError):
            return finish_schedule(cursor, job, {"error": "INVALID_TIMEZONE"}, state="failed")
        if window is None:
            return finish_schedule(cursor, job, {"skipped": "non_study_day"})

        due, expires = (window.morning, window.evening) if job.type == "morning" else (window.evening, window.end)
        if job.type == "morning" and now >= expires:
            return finish_schedule(cursor, job, {"skipped": "expired"})
        if now < due:
            cursor.execute("""
                UPDATE jobs SET state = 'queued', lease_until = NULL, run_at = %s WHERE id = %s
            """, (due, job.id))
            return "queued"

        if course["schedule_from"] > window.morning:
            has_day = cursor.execute("""
                SELECT 1 FROM group_days WHERE course_id = %s AND day = %s
            """, (course_id, day)).fetchone()
            if job.type == "morning" or has_day is None:
                return finish_schedule(cursor, job, {"skipped": "started_after_morning"})

        if job.type == "morning":
            result = open_day(cursor, course, day, expires)
        else:
            publish_until = expires if course["state"] == "active" and now < expires else None
            result = save_digest(cursor, course, day, due, publish_until, use_model=digest_model)
        return finish_schedule(cursor, job, result)


def finish_schedule(cursor: Cursor, job: Job, result: dict, state: str = "done") -> str:
    cursor.execute("""
        UPDATE jobs SET state = %s, lease_until = NULL, result = %s WHERE id = %s
    """, (state, Jsonb(result), job.id))
    return state


def delivery_payload(course: dict, day: date, group_day: UUID, expires: datetime) -> dict:
    return {
        "courseId": str(course["id"]),
        "groupDayId": str(group_day),
        "day": day.isoformat(),
        "expiresAt": expires.isoformat(),
        "chatId": course["telegram_chat_id"],
    }


def open_day(cursor: Cursor, course: dict, day: date, expires: datetime) -> dict:
    pending = cursor.execute("""
        SELECT repeat.id, repeat.ticket_id
        FROM repeats AS repeat JOIN tickets AS t ON t.id = repeat.ticket_id
        JOIN rubrics AS r ON r.id = t.approved_rubric_id AND r.state = 'approved'
        WHERE repeat.course_id = %s AND repeat.member_id IS NULL
            AND repeat.state = 'pending' AND repeat.due_day <= %s
        ORDER BY repeat.due_day, repeat.id LIMIT 1
    """, (course["id"], day)).fetchone()
    ticket = cursor.execute("""
        SELECT ticket.id
        FROM tickets AS ticket
        JOIN rubrics AS rubric ON rubric.id = ticket.approved_rubric_id AND rubric.state = 'approved'
        WHERE ticket.course_id = %s
        ORDER BY (SELECT count(*) FROM group_days WHERE ticket_id = ticket.id), ticket.ordinal
        LIMIT 1
    """, (course["id"],)).fetchone() if pending is None else None
    common_id = pending["ticket_id"] if pending else ticket["id"] if ticket else None

    row = cursor.execute("""
        INSERT INTO group_days (course_id, day, ticket_id, state)
        VALUES (%s, %s, %s, 'open')
        ON CONFLICT (course_id, day) DO NOTHING
        RETURNING id
    """, (course["id"], day, common_id)).fetchone()
    if row is None:
        return {"skipped": "already_opened"}

    group_day = row["id"]
    if pending:
        issue_repeat(cursor, course, pending["id"], day)
    payload = delivery_payload(course, day, group_day, expires)
    if common_id is not None:
        enqueue_delivery(cursor, f"deliver_morning:{group_day}", "deliver_morning", payload)
    members = cursor.execute("""
        SELECT m.id, u.dm_chat_id FROM memberships AS m JOIN users AS u ON u.id = m.user_id
        WHERE m.group_id = %s AND m.role = 'student' AND m.state = 'active' ORDER BY m.id
    """, (course["group_id"],)).fetchall()
    assignments = 0
    for member in members:
        plan = ensure_portion(cursor, course, member["id"], day, common_id)
        if plan is None:
            continue
        assignments += plan["assigned_count"]
        if member["dm_chat_id"] is not None and (plan["assigned_count"] or plan["projected_missing"]):
            enqueue_delivery(cursor, f"deliver_daily_portion:{group_day}:{member['id']}", "deliver_daily_portion", {
                **payload, "chatId": member["dm_chat_id"], "memberId": str(member["id"]),
            })
    return {"groupDayId": str(group_day), "assignments": assignments}


def save_digest(
    cursor: Cursor, course: dict, day: date, cutoff: datetime, expires: datetime | None, *, use_model: bool = False,
) -> dict:
    summary = snapshot(cursor, course, day, cutoff)
    row = cursor.execute("""
        INSERT INTO group_days (course_id, day, state, summary) VALUES (%s, %s, 'summarized', %s)
        ON CONFLICT (course_id, day) DO UPDATE SET summary = EXCLUDED.summary, state = EXCLUDED.state
        WHERE group_days.summary IS NULL
        RETURNING id
    """, (course["id"], day, Jsonb(summary))).fetchone()
    if row is None:
        return {"skipped": "already_summarized"}
    if course["state"] == "active":
        for ticket in summary["weakTickets"]:
            start_repeat(cursor, course, UUID(ticket["id"]), day)
    if expires is not None:
        enqueue_digest(cursor, summary, delivery_payload(course, day, row["id"], expires),
                       min(expires, cutoff + PUBLICATION_BUDGET), use_model=use_model)
    return {"groupDayId": str(row["id"])}
