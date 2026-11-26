import os
import unittest
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from app.queue import claim_job
from app.schedule import handle_schedule_job, local_time_utc, schedule_courses


COURSE = UUID("40000000-0000-4000-8000-000000000001")
GROUP = UUID("10000000-0000-4000-8000-000000000001")
STUDENT = UUID("20000000-0000-4000-8000-000000000002")
DAY = date(2035, 1, 20)
MORNING = datetime(2035, 1, 20, 2, tzinfo=timezone.utc)
EVENING = datetime(2035, 1, 20, 13, tzinfo=timezone.utc)


class ScheduleTimeTests(unittest.TestCase):
    def test_dst_gap_and_fold(self):
        zone = ZoneInfo("America/New_York")
        self.assertEqual(
            local_time_utc(date(2026, 3, 8), time(2, 30), zone),
            datetime(2026, 3, 8, 7, tzinfo=timezone.utc),
        )
        self.assertEqual(
            local_time_utc(date(2026, 11, 1), time(1, 30), zone),
            datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
        )


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a seeded test PostgreSQL")
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["BILET_TEST_DATABASE_URL"]
        self.db = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        self.previous_chat = self.db.execute("SELECT dm_chat_id FROM users WHERE id = %s", (STUDENT,)).fetchone()["dm_chat_id"]
        self.addCleanup(self.clean_fixture)

    def clean_fixture(self):
        self.db.execute("DELETE FROM jobs WHERE payload->>'courseId' = %s", (str(COURSE),))
        self.db.execute("DELETE FROM group_days WHERE course_id = %s AND day IN (%s, %s)",
                        (COURSE, DAY, DAY + timedelta(days=1)))
        self.db.execute("DELETE FROM assignments WHERE course_id = %s AND day = %s", (COURSE, DAY))
        self.db.execute("DELETE FROM users WHERE telegram_user_id = 900000000025")
        self.db.execute("UPDATE users SET dm_chat_id = %s WHERE id = %s", (self.previous_chat, STUDENT))

    def make_due(self, kind, day=DAY):
        return self.db.execute("""
            UPDATE jobs SET run_at = clock_timestamp() - interval '1 minute'
            WHERE key = %s RETURNING id
        """, (f"{kind}:{COURSE}:{day}",)).fetchone()["id"]

    def test_daily_run_leases_delivery_and_cutoff(self):
        extra_user = self.db.execute("""
            INSERT INTO users (telegram_user_id) VALUES (900000000025) RETURNING id
        """).fetchone()["id"]
        self.db.execute("""
            INSERT INTO memberships (group_id, user_id, role, alias) VALUES (%s, %s, 'student', 'Тест')
        """, (GROUP, extra_user))
        self.db.execute("UPDATE users SET dm_chat_id = 900000000002 WHERE id = %s", (STUDENT,))
        schedule_courses(self.db, MORNING - timedelta(minutes=1))
        schedule_courses(self.db, MORNING - timedelta(minutes=1))
        jobs = self.db.execute("SELECT type, run_at FROM jobs WHERE payload->>'day' = %s", (DAY.isoformat(),)).fetchall()
        self.assertEqual({row["type"]: row["run_at"] for row in jobs}, {"morning": MORNING, "digest": EVENING})
        self.assertEqual(len(jobs), 2)

        morning_id = self.make_due("morning")
        with psycopg.connect(self.url, autocommit=True) as other:
            with self.db.transaction():
                self.db.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (morning_id,))
                self.assertIsNone(claim_job(other, ("morning",), 30))
            original = claim_job(other, ("morning",), 30)
            self.assertEqual(original.id, morning_id)
        self.db.execute("UPDATE jobs SET lease_until = clock_timestamp() - interval '1 second' WHERE id = %s", (morning_id,))
        recovered = claim_job(self.db, ("morning",), 30)
        self.assertEqual(recovered.id, original.id)
        self.assertEqual(handle_schedule_job(self.db, original, MORNING), "lease_lost")
        self.assertEqual(handle_schedule_job(self.db, recovered, MORNING), "done")
        self.assertIsNone(claim_job(self.db, ("morning",), 30))

        assignments = self.db.execute("""
            SELECT id FROM assignments WHERE course_id = %s AND day = %s ORDER BY id
        """, (COURSE, DAY)).fetchall()
        self.assertEqual(len(assignments), 2)
        for i, assignment in enumerate(assignments):
            self.db.execute("""
                INSERT INTO attempts (assignment_id, telegram_update_id, body, status, result, graded_at)
                VALUES (%s, %s, 'Пример', 'graded', jsonb_build_object('score', %s::int), %s)
            """, (assignment["id"], 900000000030 + i, 4 - i * 3, EVENING + timedelta(seconds=i)))

        self.make_due("digest")
        digest = claim_job(self.db, ("digest",), 30)
        self.assertEqual(handle_schedule_job(self.db, digest, EVENING + timedelta(minutes=5)), "done")
        summary = self.db.execute("""
            SELECT summary FROM group_days WHERE course_id = %s AND day = %s
        """, (COURSE, DAY)).fetchone()["summary"]
        self.assertEqual(summary, {
            "asOf": EVENING.isoformat(), "active": 2, "responded": 1, "graded": 1, "meanScore": 4.0,
            "weakTickets": [], "topics": [], "rating": [],
        })
        deliveries = self.db.execute("""
            SELECT type, payload FROM jobs WHERE payload->>'courseId' = %s AND type LIKE 'deliver%%'
        """, (str(COURSE),)).fetchall()
        self.assertCountEqual([row["type"] for row in deliveries],
                              ["deliver_morning", "deliver_daily_portion", "deliver_digest"])
        self.assertTrue(all("expiresAt" in row["payload"] for row in deliveries))

        next_day = DAY + timedelta(days=1)
        self.make_due("morning", next_day)
        late = claim_job(self.db, ("morning",), 30)
        self.assertEqual(handle_schedule_job(self.db, late, EVENING + timedelta(days=1)), "done")
        skipped = self.db.execute("SELECT result FROM jobs WHERE id = %s", (late.id,)).fetchone()["result"]
        self.assertEqual(skipped, {"skipped": "expired"})
        self.assertIsNone(self.db.execute("""
            SELECT id FROM group_days WHERE course_id = %s AND day = %s
        """, (COURSE, next_day)).fetchone())
        self.make_due("digest", next_day)
        missed_digest = claim_job(self.db, ("digest",), 30)
        self.assertEqual(handle_schedule_job(self.db, missed_digest, EVENING + timedelta(days=2)), "done")
        missed_day = self.db.execute("""
            SELECT summary FROM group_days WHERE course_id = %s AND day = %s
        """, (COURSE, next_day)).fetchone()
        self.assertEqual(missed_day["summary"]["responded"], 0)
        self.assertIsNone(self.db.execute("""
            SELECT 1 FROM jobs WHERE type = 'deliver_digest' AND payload->>'day' = %s
        """, (next_day.isoformat(),)).fetchone())
