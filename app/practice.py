from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from psycopg import Cursor


DAILY_LIMIT = 8
PASSES_PER_TICKET = 2


@dataclass(frozen=True)
class TicketProgress:
    id: UUID
    ordinal: int
    rubric_id: UUID | None
    passes: int


@dataclass(frozen=True)
class Portion:
    target: int
    tickets: tuple[TicketProgress, ...]
    projected_missing: int


def study_day_count(start: date, stop: date, weekdays: list[int]) -> int:
    days = max(0, (stop - start).days)
    weekdays_set = set(weekdays)
    weeks, rest = divmod(days, 7)
    return weeks * len(weekdays_set) + sum(
        (start + timedelta(days=i)).isoweekday() in weekdays_set for i in range(rest)
    )


def next_study_day(start: date, exam_date: date, weekdays: list[int]) -> date | None:
    for i in range(min(7, max(0, (exam_date - start).days))):
        day = start + timedelta(days=i)
        if day.isoweekday() in weekdays:
            return day
    return None


def plan_portion(
    tickets: list[TicketProgress],
    due_ids: list[UUID],
    common_id: UUID | None,
    remaining_days: int,
    *,
    existing_ids: list[UUID] | None = None,
) -> Portion:
    unmet = {ticket.id: max(0, PASSES_PER_TICKET - ticket.passes) for ticket in tickets}
    missing = sum(unmet.values())
    if remaining_days <= 0:
        return Portion(0, (), missing)
    target = (missing + remaining_days - 1) // remaining_days
    available = {ticket.id: ticket for ticket in tickets if ticket.rubric_id is not None}

    if existing_ids is not None:
        # A portion from an earlier version has already been issued.
        all_tickets = {ticket.id: ticket for ticket in tickets}
        selected = [all_tickets[ticket_id] for ticket_id in existing_ids]
    else:
        selected = [available[ticket_id] for ticket_id in dict.fromkeys(due_ids) if ticket_id in available][:DAILY_LIMIT]
        used = {ticket.id for ticket in selected}
        ordered = sorted(available.values(), key=lambda ticket: (ticket.passes, ticket.id != common_id, ticket.ordinal))
        for ticket in ordered:
            if len(selected) >= min(target, DAILY_LIMIT):
                break
            if ticket.id not in used and unmet[ticket.id]:
                selected.append(ticket)

    for ticket in selected:
        unmet[ticket.id] = max(0, unmet[ticket.id] - 1)
    days_after = remaining_days - 1
    future_capacity = min(days_after * DAILY_LIMIT, sum(min(days_after, unmet[ticket_id]) for ticket_id in available))
    return Portion(target, tuple(selected), sum(unmet.values()) - future_capacity)


def ensure_portion(cursor: Cursor, course: dict, member_id: UUID, day: date, common_id: UUID | None) -> dict | None:
    if course["state"] != "active" or day >= course["exam_date"] or day.isoweekday() not in course["weekdays"]:
        return None
    member = cursor.execute("""
        SELECT id FROM memberships WHERE id = %s AND group_id = %s AND state = 'active' AND role = 'student'
        FOR NO KEY UPDATE
    """, (member_id, course["group_id"])).fetchone()
    if member is None:
        return None
    saved = cursor.execute("""
        SELECT daily_target, assigned_count, projected_missing FROM daily_portions
        WHERE course_id = %s AND member_id = %s AND day = %s
    """, (course["id"], member_id, day)).fetchone()
    if saved:
        return saved

    rows = cursor.execute("""
        SELECT t.id, t.ordinal, r.id AS rubric_id, COALESCE(MAX(progress.passes), 0) AS passes
        FROM tickets AS t
        LEFT JOIN rubrics AS r ON r.id = t.approved_rubric_id AND r.state = 'approved'
        LEFT JOIN (
            SELECT a.ticket_id, count(DISTINCT a.day) AS passes
            FROM assignments AS a JOIN attempts AS p ON p.assignment_id = a.id
            WHERE a.course_id = %s AND a.member_id = %s AND a.day < %s AND p.status = 'graded'
            GROUP BY a.ticket_id
        ) AS progress ON progress.ticket_id = t.id
        WHERE t.course_id = %s GROUP BY t.id, r.id ORDER BY t.ordinal
    """, (course["id"], member_id, day, course["id"])).fetchall()
    tickets = [TicketProgress(**row) for row in rows]
    due = cursor.execute("""
        SELECT id, ticket_id FROM repeats
        WHERE course_id = %s AND member_id = %s AND state = 'pending' AND due_day <= %s
        ORDER BY due_day, id
    """, (course["id"], member_id, day)).fetchall()
    existing = cursor.execute("""
        SELECT ticket_id FROM assignments WHERE course_id = %s AND member_id = %s AND day = %s
    """, (course["id"], member_id, day)).fetchall()
    plan = plan_portion(
        tickets, [row["ticket_id"] for row in due], common_id,
        study_day_count(day, course["exam_date"], course["weekdays"]),
        existing_ids=[row["ticket_id"] for row in existing] if existing else None,
    )
    if not existing:
        repeat_ids = {row["ticket_id"]: row["id"] for row in due}
        for ticket in plan.tickets:
            repeat_id = repeat_ids.get(ticket.id)
            cursor.execute("""
                INSERT INTO assignments (course_id, group_id, member_id, day, ticket_id, rubric_id, kind, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'open')
            """, (course["id"], course["group_id"], member_id, day, ticket.id, ticket.rubric_id,
                  "repeat" if repeat_id else "daily"))
            if repeat_id:
                issue_repeat(cursor, course, repeat_id, day)

    return cursor.execute("""
        INSERT INTO daily_portions (course_id, group_id, member_id, day, daily_target, assigned_count, projected_missing)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING daily_target, assigned_count, projected_missing
    """, (course["id"], course["group_id"], member_id, day, plan.target, len(plan.tickets), plan.projected_missing)).fetchone()


def start_repeat(
    cursor: Cursor, course: dict, ticket_id: UUID, day: date, *,
    member_id: UUID | None = None, attempt_id: UUID | None = None,
) -> None:
    due = next_study_day(day + timedelta(days=2), course["exam_date"], course["weekdays"])
    if due is not None:
        cursor.execute("""
            INSERT INTO repeats (course_id, group_id, ticket_id, member_id, due_day, step, origin_attempt_id)
            VALUES (%s, %s, %s, %s, %s, 1, %s) ON CONFLICT DO NOTHING
        """, (course["id"], course["group_id"], ticket_id, member_id, due, attempt_id))


def issue_repeat(cursor: Cursor, course: dict, repeat_id: UUID, day: date) -> None:
    row = cursor.execute("""
        UPDATE repeats SET state = 'issued' WHERE id = %s AND state = 'pending'
        RETURNING ticket_id, member_id, step, origin_attempt_id, chain_id
    """, (repeat_id,)).fetchone()
    if row is None or row["step"] != 1:
        return
    due = next_study_day(day + timedelta(days=7), course["exam_date"], course["weekdays"])
    if due is not None:
        cursor.execute("""
            INSERT INTO repeats (course_id, group_id, ticket_id, member_id, due_day, step, chain_id, origin_attempt_id)
            VALUES (%s, %s, %s, %s, %s, 2, %s, %s)
        """, (course["id"], course["group_id"], row["ticket_id"], row["member_id"], due,
              row["chain_id"], row["origin_attempt_id"]))


def remember_weak_attempt(cursor: Cursor, attempt_id: UUID) -> None:
    row = cursor.execute("""
        SELECT c.id, c.group_id, c.weekdays, c.exam_date, a.day, a.ticket_id, a.member_id
        FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
        JOIN courses AS c ON c.id = a.course_id
        WHERE p.id = %s AND c.state IN ('active', 'paused')
    """, (attempt_id,)).fetchone()
    if row is not None:
        start_repeat(cursor, row, row["ticket_id"], row["day"], member_id=row["member_id"], attempt_id=attempt_id)
