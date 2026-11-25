import os
import unittest
from datetime import timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ai.client import ModelError
from app.ai.grading import Grader
from app.ai.jobs import handle_grade_job
from test_grading import FakeModel


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a seeded test PostgreSQL")
class GradeJobTests(unittest.TestCase):
    def test_retries_terminal_failure_and_success_on_same_attempt(self):
        with psycopg.connect(os.environ["BILET_TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row) as db:
            assignment, attempt, job = uuid4(), uuid4(), uuid4()
            payload = {"attemptId": str(attempt)}
            try:
                db.execute("""
                    INSERT INTO assignments (id, course_id, group_id, member_id, ticket_id, day, rubric_id, kind, state)
                    VALUES (%s, '40000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000001',
                        '30000000-0000-4000-8000-000000000002', '50000000-0000-4000-8000-000000000001',
                        '2035-01-22', '60000000-0000-4000-8000-000000000001', 'daily', 'open')
                """, (assignment,))
                db.execute("""
                    INSERT INTO attempts (id, assignment_id, telegram_update_id, body, status)
                    VALUES (%s, %s, 900000000020, 'Тестовый ответ', 'submitted')
                """, (attempt, assignment))
                db.execute("""
                    INSERT INTO jobs (id, key, type, payload) VALUES (%s, %s, 'grade', %s)
                """, (job, f"grade:{attempt}", Jsonb(payload)))

                def claim(tries):
                    return db.execute("""
                        UPDATE jobs SET state = 'running', tries = %s,
                            lease_until = clock_timestamp() + interval '40 seconds'
                        WHERE id = %s RETURNING lease_until
                    """, (tries, job)).fetchone()["lease_until"]

                failing = Grader(FakeModel(ModelError("MODEL_HTTP_429", retry_after=25)))
                for tries, expected_delay in [(1, 25), (2, 60)]:
                    lease = claim(tries)
                    self.assertEqual(handle_grade_job(db, job, lease, failing), "queued")
                    row = db.execute("""
                        SELECT lease_until, extract(epoch FROM run_at - clock_timestamp()) AS seconds
                        FROM jobs WHERE id = %s
                    """, (job,)).fetchone()
                    self.assertIsNone(row["lease_until"])
                    self.assertAlmostEqual(float(row["seconds"]), expected_delay, delta=2)
                self.assertEqual(handle_grade_job(db, job, claim(3), failing), "failed")
                self.assertEqual(db.execute("SELECT status FROM attempts WHERE id = %s", (attempt,)).fetchone()["status"],
                                 "failed")

                working = Grader(FakeModel(
                    '{"coveredIds":["p1","p2","p3"],"missingIds":[],"errors":[],"feedback":"Верно"}'
                ))
                lease = claim(1)
                self.assertEqual(handle_grade_job(db, job, lease - timedelta(seconds=1), working), "lease_lost")
                self.assertEqual(handle_grade_job(db, job, lease, working), "done")
                self.assertEqual(handle_grade_job(db, job, lease, working), "lease_lost")
                stored = db.execute("""
                    SELECT status, result, model, prompt_version FROM attempts WHERE id = %s
                """, (attempt,)).fetchone()
                self.assertEqual((stored["status"], stored["result"]["score"], stored["model"], stored["prompt_version"]),
                                 ("graded", 5, "test", "grade-v1"))
                count = db.execute("""
                    SELECT count(*) AS total FROM jobs WHERE type = 'deliver_grade' AND payload = %s
                """, (Jsonb(payload),)).fetchone()["total"]
                self.assertEqual(count, 2)
            finally:
                db.execute("DELETE FROM jobs WHERE payload = %s", (Jsonb(payload),))
                db.execute("DELETE FROM assignments WHERE id = %s", (assignment,))
