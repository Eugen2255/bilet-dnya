import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import Mock, patch

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ai.client import ModelError
from app.ai.grading import Grader
from app.ai.jobs import handle_appeal_job
from app.main import app
from app.practice import ensure_portion, issue_repeat, remember_weak_attempt
from app.progress import read_dashboard, snapshot
from app.queue import claim_job
from app.rubrics.service import list_tickets, update_rubric
from app.schedule import save_digest
from app.telegram.delivery import grade_message
from app.telegram.jobs import handle_reply_job, handle_update_job
from app.telegram.webhook import Update, store_update
from test_grading import FakeModel
from test_miniapp import BOT_TEST_TOKEN, ORIGIN, signed_data
from test_study_flow import FakeTelegram


ADMIN = 900000010000
STUDENTS = [900000010001, 900000010002, 900000010003, 900000010004]
POINTS = [{"id": f"p{i}", "text": f"Секретный критерий {i}"} for i in range(1, 4)]
REFERENCE = "Исходный эталон"
STRONG = json.dumps({"coveredIds": ["p1", "p2", "p3"], "missingIds": [], "errors": [], "feedback": "Верно"})
WEAK = json.dumps({"coveredIds": [], "missingIds": ["p1", "p2", "p3"], "errors": [], "feedback": "Дополните ответ"})


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a dedicated test PostgreSQL")
class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["BILET_TEST_DATABASE_URL"]
        self.db = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        self.now = self.db.execute("SELECT clock_timestamp() AS now").fetchone()["now"].astimezone(timezone.utc)
        self.day = self.now.date()
        self.bot = FakeTelegram()
        self.sequence = count(900000010100)
        self.job_ids, self.update_ids, self.attempt_ids = [], [], []
        self.group = self.db.execute("""
            INSERT INTO study_groups (telegram_chat_id) VALUES (-1009000010000) RETURNING id
        """).fetchone()["id"]
        self.addCleanup(self.remove_fixture)
        self.members = {}
        for chat in [ADMIN, *STUDENTS]:
            user = self.db.execute("""
                INSERT INTO users (telegram_user_id, dm_chat_id) VALUES (%s, %s) RETURNING id
            """, (chat, chat)).fetchone()["id"]
            self.members[chat] = self.db.execute("""
                INSERT INTO memberships (group_id, user_id, role, alias, rank_opt_in)
                VALUES (%s, %s, %s, %s, true) RETURNING id
            """, (self.group, user, "organizer" if chat == ADMIN else "student", str(chat))).fetchone()["id"]
        self.course_id = self.db.execute("""
            INSERT INTO courses (group_id, title, exam_date, timezone, morning_at, evening_at, weekdays)
            VALUES (%s, 'Курс проверки апелляций', %s, 'UTC', '08:00', '20:00', '{1,2,3,4,5,6,7}') RETURNING id
        """, (self.group, self.day + timedelta(days=40))).fetchone()["id"]
        self.ticket = self.db.execute("""
            INSERT INTO tickets (course_id, ordinal, question, state)
            VALUES (%s, 1, 'Как работает первичный ключ?', 'approved') RETURNING id
        """, (self.course_id,)).fetchone()["id"]
        self.rubric = self.db.execute("""
            INSERT INTO rubrics (ticket_id, version, state, answer, source_status, points)
            VALUES (%s, 1, 'approved', %s, 'unsourced', %s) RETURNING id
        """, (self.ticket, REFERENCE, Jsonb(POINTS))).fetchone()["id"]
        self.db.execute("UPDATE tickets SET approved_rubric_id = %s WHERE id = %s", (self.rubric, self.ticket))
        self.course = self.db.execute("UPDATE courses SET state = 'active' WHERE id = %s RETURNING *",
                                      (self.course_id,)).fetchone()

    def remove_fixture(self):
        self.db.execute("""
            DELETE FROM jobs WHERE id = ANY (%s) OR payload->>'attemptId' = ANY (%s)
                OR payload->>'chat_id' = ANY (%s)
        """, (self.job_ids, [str(attempt) for attempt in self.attempt_ids], [str(chat) for chat in STUDENTS]))
        self.db.execute("DELETE FROM telegram_updates WHERE update_id = ANY (%s)", (self.update_ids,))
        self.db.execute("DELETE FROM study_groups WHERE id = %s", (self.group,))
        self.db.execute("DELETE FROM users WHERE telegram_user_id = ANY (%s)", ([ADMIN, *STUDENTS],))

    def assignment(self, chat, score=None, *, days_ago=0, age=timedelta(minutes=5)):
        assignment = self.db.execute("""
            INSERT INTO assignments (course_id, group_id, member_id, day, ticket_id, rubric_id, kind, state, dm_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, 'daily', %s, %s) RETURNING id
        """, (self.course_id, self.group, self.members[chat], self.day - timedelta(days=days_ago),
              self.ticket, self.rubric, "open" if score is None else "answered", next(self.sequence))).fetchone()["id"]
        if score is None:
            return assignment
        attempt = self.db.execute("""
            INSERT INTO attempts (assignment_id, telegram_update_id, body, status, result, graded_at, model, prompt_version)
            VALUES (%s, %s, 'Тестовый ответ', 'graded', %s, %s, 'original-model', 'grade-v1') RETURNING id
        """, (assignment, next(self.sequence), Jsonb({"score": score, "feedback": "Первый разбор"}),
              self.now - age)).fetchone()["id"]
        self.attempt_ids.append(attempt)
        if score < 3:
            with self.db.transaction(), self.db.cursor() as cursor:
                remember_weak_attempt(cursor, attempt)
        return attempt

    def claim(self, kind):
        job = claim_job(self.db, (kind,), 60)
        self.assertIsNotNone(job, kind)
        self.job_ids.append(job.id)
        return job

    def update(self, **data):
        update_id = next(self.sequence)
        self.update_ids.append(update_id)
        update = Update(update_id=update_id, **data)
        store_update(self.db, update)
        store_update(self.db, update)
        self.assertEqual(handle_update_job(self.db, self.claim("telegram_update"), self.bot), "done")

    def callback(self, chat, data):
        self.update(callback_query={"id": "case", "from": {"id": chat}, "data": data,
                                    "message": {"chat": {"type": "private", "id": chat}}})
        self.assertEqual(handle_reply_job(self.db, self.claim("telegram_callback"), self.bot), "done")
        return self.bot.callbacks[-1]["text"]

    def grade_payload(self, attempt):
        with self.db.cursor() as cursor:
            return grade_message(cursor, attempt, None).payload

    def review(self, attempt, response):
        job = self.claim("appeal")
        self.assertEqual(job.payload, {"attemptId": str(attempt)})
        model = FakeModel(response, "review-model")
        model.complete = Mock(wraps=model.complete)
        state = handle_appeal_job(self.db, job.id, job.lease_until, Grader(model))
        self.assertEqual(handle_appeal_job(self.db, job.id, job.lease_until, Grader(model)), "lease_lost")
        return state, model.complete

    def miniapp_attempt(self, chat, attempt):
        with patch.dict(os.environ, {"DATABASE_URL": self.url, "TELEGRAM_BOT_TOKEN": BOT_TEST_TOKEN,
                                     "MINI_APP_URL": ORIGIN + "/app/"}), TestClient(app, base_url=ORIGIN) as client:
            login = client.post("/v1/auth/telegram", headers={"Origin": ORIGIN},
                                json={"initData": signed_data(chat, datetime.now(timezone.utc))})
            self.assertEqual(login.status_code, 200, login.text)
            return client.get(f"/v1/attempts/{attempt}")

    def approve_replacement(self):
        return update_rubric(self.db, self.ticket, self.members[ADMIN], base_version=1,
                             answer="Исправленный эталон", points=POINTS, approve=True, acknowledge_unsourced=True)

    def test_appeal_uses_assigned_version_and_updates_current_grade_only(self):
        chat = STUDENTS[0]
        attempt = self.assignment(chat, 1)
        data = {button["callback_data"] for row in self.grade_payload(attempt)["reply_markup"]["inline_keyboard"]
                for button in row}
        self.assertEqual(data, {f"appeal:{attempt}", f"report:{attempt}"})
        self.assertIn("недоступна", self.callback(STUDENTS[1], f"appeal:{attempt}"))
        self.assertIn("принята", self.callback(chat, f"appeal:{attempt}"))
        self.assertIn("уже", self.callback(chat, f"appeal:{attempt}"))
        expired = self.assignment(STUDENTS[1], 5, days_ago=1, age=timedelta(hours=25))
        self.assertIn("24 часов", self.callback(STUDENTS[1], f"appeal:{expired}"))

        with self.db.transaction(), self.db.cursor() as cursor:
            cutoff = cursor.execute("SELECT clock_timestamp() AS cutoff").fetchone()["cutoff"]
            before = snapshot(cursor, self.course, self.day, cutoff)
            save_digest(cursor, self.course, self.day, cutoff, None)
        self.approve_replacement()
        state, complete = self.review(attempt, STRONG)
        self.assertEqual(state, "done")
        sent = json.loads(complete.call_args.args[0][1]["content"])
        self.assertEqual(sent, {"question": "Как работает первичный ключ?", "reference": REFERENCE,
                                "points": POINTS, "answer": "Тестовый ответ"})
        stored = self.db.execute("""
            SELECT p.result->'score' AS original, p.model AS first_model, a.result->'score' AS current,
                a.model AS review_model, a.prompt_version FROM attempts AS p
            JOIN appeals AS a ON a.attempt_id = p.id WHERE p.id = %s
        """, (attempt,)).fetchone()
        self.assertEqual(stored, {"original": 1, "first_model": "original-model", "current": 5,
                                  "review_model": "review-model", "prompt_version": "grade-v1"})
        self.assertEqual(handle_reply_job(self.db, self.claim("deliver_grade"), self.bot), "done")
        self.assertIn("Апелляция рассмотрена", self.bot.messages[-1]["text"])
        self.assertIn("Оценка 5/5", self.bot.messages[-1]["text"])
        self.assertEqual(self.miniapp_attempt(chat, attempt).json()["score"], 5)
        self.assertEqual(self.miniapp_attempt(STUDENTS[1], attempt).status_code, 404)
        with self.db.cursor() as cursor:
            self.assertEqual(snapshot(cursor, self.course, self.day, cutoff), before)
            latest = cursor.execute("SELECT clock_timestamp() AS cutoff").fetchone()["cutoff"]
            self.assertEqual(snapshot(cursor, self.course, self.day, latest)["meanScore"], 5)
            self.assertEqual(read_dashboard(cursor, self.course_id, self.members[chat])["tickets"][0]["lastScore"], 5)
        self.assertEqual(self.db.execute("SELECT summary FROM group_days WHERE course_id = %s",
                                         (self.course_id,)).fetchone()["summary"], before)
        self.assertEqual(self.db.execute("SELECT state FROM repeats WHERE origin_attempt_id = %s",
                                         (attempt,)).fetchone()["state"], "closed")

    def test_appeal_keeps_issued_repeat_and_retains_other_weak_evidence(self):
        chat = STUDENTS[0]
        first = self.assignment(chat, 1, days_ago=2)
        second = self.assignment(chat, 2, days_ago=1)
        pending = self.db.execute("SELECT id FROM repeats WHERE origin_attempt_id = %s", (first,)).fetchone()["id"]
        with self.db.transaction(), self.db.cursor() as cursor:
            issue_repeat(cursor, self.course, pending, self.day)
        for attempt in (first, second):
            self.assertIn("принята", self.callback(chat, f"appeal:{attempt}"))
            self.assertEqual(self.review(attempt, STRONG)[0], "done")
            states = self.db.execute("""
                SELECT state, origin_attempt_id FROM repeats WHERE course_id = %s ORDER BY step
            """, (self.course_id,)).fetchall()
            self.assertEqual(states[0], {"state": "issued", "origin_attempt_id": first})
            self.assertEqual(states[1], {"state": "pending" if attempt == first else "closed", "origin_attempt_id": second})

        high = self.assignment(chat, 5)
        self.assertIn("принята", self.callback(chat, f"appeal:{high}"))
        self.assertEqual(self.review(high, WEAK)[0], "done")
        repeat = self.db.execute("SELECT due_day, state FROM repeats WHERE origin_attempt_id = %s", (high,)).fetchone()
        self.assertEqual(repeat, {"due_day": self.day + timedelta(days=2), "state": "pending"})

    def test_failed_review_preserves_original_grade(self):
        chat = STUDENTS[0]
        attempt = self.assignment(chat, 5)
        self.assertIn("принята", self.callback(chat, f"appeal:{attempt}"))
        for expected in ("queued", "queued", "failed"):
            self.assertEqual(self.review(attempt, ModelError("MODEL_HTTP_429", retry_after=1))[0], expected)
            self.db.execute("UPDATE jobs SET run_at = now() WHERE key = %s", (f"appeal:{attempt}",))
        payload = self.grade_payload(attempt)
        self.assertIn("Исходная оценка сохранена", payload["text"])
        self.assertIn("Оценка 5/5", payload["text"])
        self.assertNotIn(f"appeal:{attempt}", str(payload["reply_markup"]))

    def test_three_independent_reports_hide_rubric_and_reopen_current_unanswered_assignment(self):
        reporter_attempts = [self.assignment(chat, 5) for chat in STUDENTS[:3]]
        repeated_reporter = self.assignment(STUDENTS[0], 5, days_ago=1)
        reader = STUDENTS[3]
        open_assignment = self.assignment(reader)
        old_assignment = self.assignment(reader, days_ago=1)
        self.assertIn("свой ответ", self.callback(reader, f"report:{reporter_attempts[0]}"))
        self.assertIn("принята", self.callback(STUDENTS[0], f"report:{reporter_attempts[0]}"))
        self.assertIn("уже", self.callback(STUDENTS[0], f"report:{repeated_reporter}"))
        self.assertIn("принята", self.callback(STUDENTS[1], f"report:{reporter_attempts[1]}"))
        self.assertIn("приостановлен", self.callback(STUDENTS[2], f"report:{reporter_attempts[2]}"))
        self.assertEqual(self.db.execute("SELECT state FROM rubrics WHERE id = %s", (self.rubric,)).fetchone()["state"],
                         "revoked")
        self.assertEqual(self.db.execute("SELECT count(*) AS total FROM rubric_reports WHERE rubric_id = %s",
                                         (self.rubric,)).fetchone()["total"], 3)
        assignment = self.db.execute("SELECT * FROM assignments WHERE id = %s", (open_assignment,)).fetchone()
        self.assertEqual(assignment["state"], "revoked")
        self.assertIn("проверяет преподаватель", self.callback(reader, f"ticket:{open_assignment}"))
        self.assertNotIn(REFERENCE, self.grade_payload(reporter_attempts[0])["text"])
        detail = self.miniapp_attempt(STUDENTS[0], reporter_attempts[0]).json()
        self.assertEqual((detail["score"], detail["reference"], detail["covered"], detail["missing"]), (5, None, None, None))
        editor = list_tickets(self.db, self.course_id, self.members[ADMIN])["items"][0]
        self.assertTrue(editor["reviewRequired"])
        self.assertIsNone(editor["approvedVersion"])

        with self.db.transaction(), self.db.cursor() as cursor:
            plan = ensure_portion(cursor, self.course, self.members[reader], self.day + timedelta(days=1), None)
            self.assertFalse(plan["assigned_count"])
            self.assertGreater(plan["projected_missing"], 0)
        self.assertEqual(self.approve_replacement()["version"], 2)
        approved = self.db.execute("SELECT approved_rubric_id FROM tickets WHERE id = %s",
                                   (self.ticket,)).fetchone()["approved_rubric_id"]
        self.assertNotEqual(approved, self.rubric)
        current = self.db.execute("SELECT state, rubric_id, dm_message_id FROM assignments WHERE id = %s",
                                  (open_assignment,)).fetchone()
        self.assertEqual(current, {"state": "open", "rubric_id": approved, "dm_message_id": assignment["dm_message_id"]})
        self.assertIn("Ответьте", self.callback(reader, f"ticket:{open_assignment}"))
        self.assertEqual(self.db.execute("SELECT state FROM assignments WHERE id = %s",
                                         (old_assignment,)).fetchone()["state"], "revoked")
        self.assertEqual(self.db.execute("""
            SELECT rubric_id, state FROM assignments WHERE id = (SELECT assignment_id FROM attempts WHERE id = %s)
        """, (reporter_attempts[0],)).fetchone(), {"rubric_id": self.rubric, "state": "answered"})
