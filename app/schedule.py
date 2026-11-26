import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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


def schedule_courses(connection: Connection, now: datetime) -> None:
    """Persist jobs for the current and next teaching dates."""
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        courses = cursor.execute("""
            SELECT id, exam_date, timezone, morning_at, evening_at, weekdays, schedule_from
            FROM courses WHERE state = 'active'
        """).fetchall()
        for course in courses:
            try:
                zone = ZoneInfo(course["timezone"])
            except (ZoneInfoNotFoundError, ValueError):
                logger.warning("Invalid schedule timezone for course %s", course["id"])
                continue
            today = now.astimezone(zone).date()
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


def handle_schedule_job(connection: Connection, job: Job, now: datetime) -> str:
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
        if course is None or course["state"] != "active":
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
            result = save_digest(cursor, course, day, due, expires if now < expires else None)
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
    ticket = cursor.execute("""
        SELECT ticket.id, ticket.approved_rubric_id
        FROM tickets AS ticket
        JOIN rubrics AS rubric ON rubric.id = ticket.approved_rubric_id AND rubric.state = 'approved'
        WHERE ticket.course_id = %s
        ORDER BY (SELECT count(*) FROM group_days WHERE ticket_id = ticket.id), ticket.ordinal
        LIMIT 1
    """, (course["id"],)).fetchone()
    if ticket is None:
        return {"skipped": "no_approved_ticket"}

    row = cursor.execute("""
        INSERT INTO group_days (course_id, day, ticket_id, state)
        VALUES (%s, %s, %s, 'open')
        ON CONFLICT (course_id, day) DO NOTHING
        RETURNING id
    """, (course["id"], day, ticket["id"])).fetchone()
    if row is None:
        return {"skipped": "already_opened"}

    group_day = row["id"]
    new_assignments = cursor.execute("""
        INSERT INTO assignments (course_id, group_id, member_id, ticket_id, day, kind, rubric_id, state)
        SELECT %s, group_id, id, %s, %s, 'daily', %s, 'open'
        FROM memberships
        WHERE group_id = %s AND role = 'student' AND state = 'active'
        ON CONFLICT (member_id, ticket_id, day) DO NOTHING
        RETURNING id, member_id
    """, (course["id"], ticket["id"], day, ticket["approved_rubric_id"], course["group_id"])).fetchall()

    payload = delivery_payload(course, day, group_day, expires)
    enqueue_delivery(cursor, f"deliver_morning:{group_day}", "deliver_morning", payload)
    if new_assignments:
        rows = cursor.execute("""
            SELECT member.id, account.dm_chat_id
            FROM memberships AS member JOIN users AS account ON account.id = member.user_id
            WHERE member.id = ANY (%s) AND account.dm_chat_id IS NOT NULL
        """, ([row["member_id"] for row in new_assignments],)).fetchall()
        private_chats = {row["id"]: row["dm_chat_id"] for row in rows}
        for assignment in new_assignments:
            chat_id = private_chats.get(assignment["member_id"])
            if chat_id is not None:
                enqueue_delivery(cursor, f"deliver_daily_portion:{assignment['id']}", "deliver_daily_portion", {
                    **payload,
                    "chatId": chat_id,
                    "assignmentId": str(assignment["id"]),
                    "memberId": str(assignment["member_id"]),
                })
    return {"groupDayId": str(group_day), "assignments": len(new_assignments)}


def save_digest(cursor: Cursor, course: dict, day: date, cutoff: datetime, expires: datetime | None) -> dict:
    stats = cursor.execute("""
        WITH students AS (
            SELECT id FROM memberships WHERE group_id = %s AND role = 'student' AND state = 'active'
        )
        SELECT (SELECT count(*) FROM students) AS active,
            count(DISTINCT assignment.member_id) AS responded,
            count(*) AS graded,
            round(avg((attempt.result->>'score')::numeric), 2) AS mean_score
        FROM assignments AS assignment
        JOIN students ON students.id = assignment.member_id
        JOIN attempts AS attempt ON attempt.assignment_id = assignment.id
        WHERE assignment.course_id = %s AND assignment.day = %s AND attempt.status = 'graded'
            AND attempt.graded_at <= %s AND attempt.result IS NOT NULL
    """, (course["group_id"], course["id"], day, cutoff)).fetchone()
    summary = {
        "asOf": cutoff.isoformat(),
        "active": stats["active"],
        "responded": stats["responded"],
        "graded": stats["graded"],
        "meanScore": float(stats["mean_score"]) if stats["mean_score"] is not None else None,
    }
    row = cursor.execute("""
        INSERT INTO group_days (course_id, day, state, summary) VALUES (%s, %s, 'summarized', %s)
        ON CONFLICT (course_id, day) DO UPDATE SET summary = EXCLUDED.summary, state = EXCLUDED.state
        WHERE group_days.summary IS NULL
        RETURNING id
    """, (course["id"], day, Jsonb(summary))).fetchone()
    if row is None:
        return {"skipped": "already_summarized"}
    if expires is not None:
        enqueue_delivery(cursor, f"deliver_digest:{row['id']}", "deliver_digest",
                         delivery_payload(course, day, row["id"], expires))
    return {"groupDayId": str(row["id"])}
