import json
import os
import unittest
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.practice import TicketProgress, ensure_portion, plan_portion, remember_weak_attempt, study_day_count
from app.progress import read_dashboard
from app.schedule import day_window, open_day, save_digest, schedule_courses
from app.telegram.delivery import format_digest
from test_worker import COURSE


FIRST = date(2035, 1, 1)
FRIDAY = FIRST + timedelta(days=(5 - FIRST.isoweekday()) % 7)
WEEKDAYS = [1, 2, 3, 4, 5]


class PortionTests(unittest.TestCase):
    def test_target_limit_priority_and_unavailable_forecast(self):
        tickets = [TicketProgress(UUID(int=i), i, UUID(int=i + 100), 0) for i in range(1, 61)]
        lesson = plan_portion(tickets, [], tickets[10].id, 30)
        self.assertEqual((lesson.target, len(lesson.tickets), lesson.projected_missing), (4, 4, 0))
        self.assertEqual(lesson.tickets[0].id, tickets[10].id)
        self.assertEqual(study_day_count(FRIDAY, FRIDAY + timedelta(days=7), WEEKDAYS), 5)

        blocked = TicketProgress(tickets[0].id, 1, None, 0)
        plan = plan_portion([blocked, *tickets[1:]], [tickets[-1].id, blocked.id], tickets[10].id, 1)
        self.assertEqual((plan.target, len(plan.tickets), plan.projected_missing), (120, 8, 112))
        self.assertEqual(plan.tickets[0].id, tickets[-1].id)
        self.assertNotIn(blocked, plan.tickets)

        complete = TicketProgress(tickets[0].id, 1, tickets[0].rubric_id, 2)
        self.assertEqual(plan_portion([complete], [], complete.id, 1).tickets, ())
        self.assertEqual(plan_portion([complete], [complete.id], complete.id, 1).tickets, (complete,))


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a seeded test PostgreSQL")
class PracticeIntegrationTests(unittest.TestCase):
    def test_repeat_chain_late_user_snapshot_and_exam(self):
        with psycopg.connect(os.environ["BILET_TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row) as db:
            with db.transaction(force_rollback=True), db.cursor() as cursor:
                exam = FRIDAY + timedelta(days=28)
                cursor.execute("""
                    UPDATE courses SET exam_date = %s, timezone = 'UTC', weekdays = %s::smallint[]
                    WHERE id = %s
                """, (exam, WEEKDAYS, COURSE))
                course = cursor.execute("""
                    SELECT c.*, g.telegram_chat_id FROM courses AS c JOIN study_groups AS g ON g.id = c.group_id
                    WHERE c.id = %s
                """, (COURSE,)).fetchone()
                existing = cursor.execute("""
                    SELECT id FROM memberships WHERE group_id = %s AND role = 'student'
                """, (course["group_id"],)).fetchone()["id"]
                middle = [self.add_student(cursor, course, i) for i in (61, 62)]
                cutoff = datetime.combine(FRIDAY, time(20), timezone.utc)
                result = open_day(cursor, course, FRIDAY, cutoff)
                self.assertEqual(result["assignments"], 3)

                late = self.add_student(cursor, course, 63)
                common = cursor.execute("SELECT ticket_id FROM group_days WHERE course_id = %s AND day = %s",
                                        (COURSE, FRIDAY)).fetchone()["ticket_id"]
                plan = ensure_portion(cursor, course, late, FRIDAY, common)
                self.assertEqual(plan, ensure_portion(cursor, course, late, FRIDAY, common))
                self.assertEqual((plan["daily_target"], plan["assigned_count"]), (1, 1))
                for member, uid in zip([existing, *middle, late], range(900000003001, 900000003005), strict=True):
                    is_late = member == late
                    attempt = cursor.execute("""
                        INSERT INTO attempts (assignment_id, telegram_update_id, body, status, result, graded_at)
                        SELECT id, %s, 'Личный текст', 'graded', %s, %s FROM assignments
                        WHERE course_id = %s AND member_id = %s AND day = %s RETURNING id
                    """, (uid, Jsonb({"score": 5 if is_late else 2, "missingIds": ["p1"], "errors": ["Личный фрагмент"]}),
                          cutoff + timedelta(seconds=1 if is_late else -1), COURSE, member, FRIDAY)).fetchone()["id"]
                    if member == existing:
                        remember_weak_attempt(cursor, attempt)
                        remember_weak_attempt(cursor, attempt)
                cursor.execute("UPDATE memberships SET rank_opt_in = true WHERE id = %s", (middle[0],))

                save_digest(cursor, course, FRIDAY, cutoff, None)
                day = cursor.execute("SELECT summary FROM group_days WHERE course_id = %s AND day = %s",
                                     (COURSE, FRIDAY)).fetchone()
                summary = day["summary"]
                self.assertEqual((summary["active"], summary["responded"], summary["meanScore"]), (4, 3, 2))
                self.assertEqual(summary["weakTickets"], [{"id": str(common), "count": 3, "mean": 2.0}])
                self.assertEqual(summary["topics"][0]["factualErrorCount"], 3)
                self.assertNotIn("Личный", json.dumps(summary["topics"], ensure_ascii=False))
                self.assertEqual([r["memberId"] for r in summary["rating"]], [str(middle[0])])
                self.assertIn("Тест-61", format_digest(summary, {str(middle[0])}))
                self.assertNotIn("Тест-61", format_digest(summary, set()))
                self.assertEqual(save_digest(cursor, course, FRIDAY, cutoff + timedelta(minutes=1), None),
                                 {"skipped": "already_summarized"})

                late_map = read_dashboard(cursor, COURSE, late, cutoff + timedelta(minutes=1))
                self.assertEqual((late_map["tickets"][0]["passes"], late_map["tickets"][0]["lastScore"]), (1, 5))
                self.assertNotIn("group", late_map)
                self.assertIsNone(read_dashboard(cursor, COURSE, UUID(int=0), cutoff))
                previous = cursor.execute("SELECT summary FROM group_days WHERE course_id = %s AND day = %s",
                                          (COURSE, FRIDAY)).fetchone()["summary"]
                self.assertEqual(previous, summary)

                monday = FRIDAY + timedelta(days=3)
                tuesday = monday + timedelta(days=1)
                pending = cursor.execute("""
                    SELECT due_day, step FROM repeats WHERE course_id = %s AND state = 'pending'
                """, (COURSE,)).fetchall()
                self.assertEqual(pending, [{"due_day": monday, "step": 1}] * 2)
                open_day(cursor, course, tuesday, cutoff + timedelta(days=4))
                second = cursor.execute("""
                    SELECT due_day, step FROM repeats WHERE course_id = %s AND state = 'pending'
                """, (COURSE,)).fetchall()
                self.assertEqual(second, [{"due_day": tuesday + timedelta(days=7), "step": 2}] * 2)
                repeated = cursor.execute("""
                    SELECT a.kind, d.ticket_id FROM assignments AS a JOIN group_days AS d
                        ON a.course_id = d.course_id AND a.day = d.day
                    WHERE a.course_id = %s AND a.member_id = %s AND a.day = %s AND a.ticket_id = %s
                """, (COURSE, existing, tuesday, common)).fetchone()
                self.assertEqual(repeated, {"kind": "repeat", "ticket_id": common})
                self.assertIsNone(day_window(course, exam, ZoneInfo("UTC")))
                self.assertIsNone(ensure_portion(cursor, course, existing, exam, common))
                schedule_courses(db, datetime.combine(exam, time(10), timezone.utc))
                self.assertEqual(cursor.execute("""
                    SELECT count(*) AS count FROM repeats WHERE course_id = %s AND state = 'pending'
                """, (COURSE,)).fetchone()["count"], 0)

    @staticmethod
    def add_student(cursor, course, number):
        user = cursor.execute("INSERT INTO users (telegram_user_id) VALUES (%s) RETURNING id",
                              (900000000000 + number,)).fetchone()["id"]
        return cursor.execute("""
            INSERT INTO memberships (group_id, user_id, role, alias)
            VALUES (%s, %s, 'student', %s) RETURNING id
        """, (course["group_id"], user, f"Тест-{number}")).fetchone()["id"]
