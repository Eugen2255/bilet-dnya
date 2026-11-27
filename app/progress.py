from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Cursor


def day_grades(cursor: Cursor, course_id: UUID, day: date, cutoff: datetime) -> list[dict]:
    return cursor.execute("""
        SELECT a.member_id, a.ticket_id, t.question, COALESCE(review.result, p.result) AS result,
            r.id AS rubric_id, r.points
        FROM assignments AS a JOIN memberships AS m ON m.id = a.member_id
        JOIN attempts AS p ON p.assignment_id = a.id
        LEFT JOIN appeals AS review ON review.attempt_id = p.id AND review.reviewed_at <= %s
        JOIN tickets AS t ON t.id = a.ticket_id JOIN rubrics AS r ON r.id = a.rubric_id
        WHERE a.course_id = %s AND a.day = %s AND m.role = 'student' AND m.state = 'active'
            AND p.status = 'graded' AND p.graded_at <= %s AND p.result IS NOT NULL
        ORDER BY t.ordinal, a.member_id
    """, (cutoff, course_id, day, cutoff)).fetchall()


def group_stats(grades: list[dict]) -> tuple[list[dict], list[dict]]:
    tickets = defaultdict(list)
    for row in grades:
        tickets[row["ticket_id"]].append(row)
    weak, topics = [], []
    for ticket_id, rows in tickets.items():
        average = sum(row["result"]["score"] for row in rows) / len(rows)
        if len(rows) >= 3 and average < 3:
            weak.append({"id": str(ticket_id), "count": len(rows), "mean": round(average, 2)})
        if len(rows) < 3:
            continue
        missing = Counter()
        errors = error_answers = 0
        for row in rows:
            keys = set(row["result"].get("missingIds", []))
            missing.update({point["text"] for point in row["points"] if point["id"] in keys})
            count = row["result"].get("errorCount", len(row["result"].get("errors") or []))
            errors += count
            error_answers += count > 0
        topics.append({
            "question": rows[0]["question"],
            "answers": len(rows),
            "rubricIds": sorted({str(row["rubric_id"]) for row in rows}),
            "missingPoints": [{"label": text, "count": count} for text, count in missing.most_common() if count >= 3],
            "factualErrorCount": errors,
            "factualErrorAnswers": error_answers,
        })
    return weak, topics


def active_students(cursor: Cursor, group_id: UUID) -> int:
    return cursor.execute("""
        SELECT count(*) AS count FROM memberships WHERE group_id = %s AND role = 'student' AND state = 'active'
    """, (group_id,)).fetchone()["count"]


def snapshot(cursor: Cursor, course: dict, day: date, cutoff: datetime) -> dict:
    grades = day_grades(cursor, course["id"], day, cutoff)
    weak, topics = group_stats(grades)
    ranks = cursor.execute("""
        SELECT m.id, m.alias, count(DISTINCT a.day) AS days,
            avg((COALESCE(review.result, p.result)->>'score')::numeric) AS mean_score
        FROM memberships AS m JOIN assignments AS a ON a.member_id = m.id
        JOIN attempts AS p ON p.assignment_id = a.id
        LEFT JOIN appeals AS review ON review.attempt_id = p.id AND review.reviewed_at <= %s
        WHERE a.course_id = %s AND a.day <= %s AND m.state = 'active'
            AND m.role = 'student' AND m.rank_opt_in
            AND p.status = 'graded' AND p.graded_at <= %s AND p.result IS NOT NULL
        GROUP BY m.id ORDER BY days DESC, mean_score DESC, m.alias, m.id
    """, (cutoff, course["id"], day, cutoff)).fetchall()
    return {
        "asOf": cutoff.isoformat(),
        "active": active_students(cursor, course["group_id"]),
        "responded": len({row["member_id"] for row in grades}),
        "graded": len(grades),
        "meanScore": round(sum(row["result"]["score"] for row in grades) / len(grades), 2) if grades else None,
        "weakTickets": weak,
        "topics": topics,
        "rating": [{
            "memberId": str(row["id"]), "alias": row["alias"], "days": row["days"],
            "meanScore": float(round(row["mean_score"], 2)),
        } for row in ranks],
    }


def streak_for(days: set[date], today: date, weekdays: list[int], exam_date: date | None) -> int:
    if not days or not weekdays:
        return 0
    day = min(today, exam_date - timedelta(days=1)) if exam_date else today
    first = min(days)
    streak = 0
    while day >= first:
        if day.isoweekday() in weekdays:
            if day in days:
                streak += 1
            elif day != today or streak:
                break
        day -= timedelta(days=1)
    return streak


def read_dashboard(
    cursor: Cursor, course_id: UUID, member_id: UUID, as_of: datetime | None = None
) -> dict | None:
    """Read the caller's progress. Editors additionally receive group totals for this course."""
    course = cursor.execute("""
        SELECT c.group_id, c.timezone, c.exam_date, c.weekdays, m.role, statement_timestamp() AS read_at
        FROM courses AS c JOIN memberships AS m ON m.group_id = c.group_id
        WHERE c.id = %s AND m.id = %s AND m.state = 'active'
    """, (course_id, member_id)).fetchone()
    if course is None:
        return None
    as_of = as_of or course["read_at"]
    today = as_of.astimezone(ZoneInfo(course["timezone"] or "UTC")).date()
    grades = cursor.execute("""
        SELECT a.ticket_id, a.day, p.id, COALESCE(review.result, p.result)->'score' AS score
        FROM assignments AS a JOIN attempts AS p ON p.assignment_id = a.id
        LEFT JOIN appeals AS review ON review.attempt_id = p.id AND review.reviewed_at <= %s
        WHERE a.course_id = %s AND a.member_id = %s AND p.status = 'graded'
            AND p.graded_at <= %s AND a.day <= %s AND p.result IS NOT NULL
        ORDER BY a.day DESC, p.submitted_at DESC, p.id DESC
    """, (as_of, course_id, member_id, as_of, today)).fetchall()
    by_ticket = defaultdict(list)
    for row in grades:
        by_ticket[row["ticket_id"]].append(row)
    tickets = cursor.execute("""
        SELECT id, question FROM tickets WHERE course_id = %s ORDER BY ordinal
    """, (course_id,)).fetchall()
    repeats = cursor.execute("""
        SELECT ticket_id, min(due_day) AS day FROM repeats
        WHERE course_id = %s AND member_id = %s AND state = 'pending' AND due_day < %s
        GROUP BY ticket_id
    """, (course_id, member_id, course["exam_date"])).fetchall()
    by_due = ({row["ticket_id"]: row["day"].isoformat() for row in repeats}
              if course["exam_date"] and today < course["exam_date"] else {})
    result = {
        "asOf": as_of.isoformat(),
        "streak": streak_for({row["day"] for row in grades}, today, course["weekdays"], course["exam_date"]),
        "tickets": [],
    }
    for ticket in tickets:
        history = by_ticket[ticket["id"]]
        result["tickets"].append({
            "id": str(ticket["id"]),
            "question": ticket["question"],
            "passes": min(2, len({row["day"] for row in history})),
            "lastScore": history[0]["score"] if history else None,
            "lastAttemptId": str(history[0]["id"]) if history else None,
            "nextReviewOn": by_due.get(ticket["id"]),
        })
    if course["role"] in {"organizer", "teacher"}:
        current = day_grades(cursor, course_id, today, as_of)
        weak, _ = group_stats(current)
        result["group"] = {
            "active": active_students(cursor, course["group_id"]),
            "responded": len({row["member_id"] for row in current}),
            "weakTickets": weak,
        }
    return result
