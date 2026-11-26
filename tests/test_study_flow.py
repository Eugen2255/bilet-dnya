import os
import unittest
from datetime import datetime, timedelta, timezone
from itertools import count

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ai.client import ModelError
from app.ai.grading import Grader
from app.ai.jobs import handle_grade_job
from app.queue import claim_job, enqueue_delivery
from app.telegram.api import TelegramError
from app.telegram.jobs import TELEGRAM_JOB_TYPES, handle_reply_job, handle_update_job
from app.telegram.webhook import Update, store_update
from test_grading import FakeModel
from test_worker import COURSE, STUDENT


STUDENT_CHAT = 900000000002
OTHER_CHAT = 900000000001
GROUP_CHAT = -1009000000001


class FakeTelegram:
    username = "bilet_test_bot"

    def __init__(self):
        self.messages = []
        self.callbacks = []
        self.fail = False

    def send(self, payload):
        if self.fail:
            raise TelegramError()
        self.messages.append(payload)
        return len(self.messages)

    def answer_callback(self, payload):
        self.callbacks.append(payload)


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a seeded test PostgreSQL")
class StudyFlowTests(unittest.TestCase):
    def setUp(self):
        self.db = psycopg.connect(os.environ["BILET_TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        self.bot = FakeTelegram()
        self.sequence = count(900000002000)
        self.claimed = []
        self.previous_chat = self.db.execute(
            "SELECT dm_chat_id FROM users WHERE id = %s", (STUDENT,)
        ).fetchone()["dm_chat_id"]
        self.addCleanup(self.clean_fixture)
        self.day = self.db.execute("""
            SELECT (statement_timestamp() AT TIME ZONE timezone)::date AS day
            FROM courses WHERE id = %s
        """, (COURSE,)).fetchone()["day"]
        self.db.execute("UPDATE users SET dm_chat_id = %s WHERE id = %s", (STUDENT_CHAT, STUDENT))
        self.group_day = self.db.execute("""
            INSERT INTO group_days (course_id, day, ticket_id, state)
            SELECT course_id, %s, id, 'open' FROM tickets WHERE course_id = %s AND ordinal = 1
            RETURNING id
        """, (self.day, COURSE)).fetchone()["id"]

    def clean_fixture(self):
        self.db.execute("DELETE FROM jobs WHERE id = ANY (%s) OR key LIKE 'test-study-%%'", (self.claimed,))
        self.db.execute("""
            DELETE FROM jobs WHERE key IN (
                SELECT 'deliver_grade_delay:' || id FROM attempts
                WHERE telegram_update_id BETWEEN 900000002000 AND 900000002099
            )
        """)
        self.db.execute("DELETE FROM telegram_updates WHERE update_id BETWEEN 900000002000 AND 900000002099")
        self.db.execute("DELETE FROM assignments WHERE course_id = %s AND day = %s", (COURSE, self.day))
        self.db.execute("DELETE FROM group_days WHERE course_id = %s AND day = %s", (COURSE, self.day))
        self.db.execute("UPDATE users SET dm_chat_id = %s WHERE id = %s", (self.previous_chat, STUDENT))

    def claim(self, types):
        job = claim_job(self.db, types, 60)
        if job is not None:
            self.claimed.append(job.id)
        return job

    def send_all(self):
        delivery = tuple(kind for kind in TELEGRAM_JOB_TYPES if kind != "telegram_update")
        while job := self.claim(delivery):
            handle_reply_job(self.db, job, self.bot)

    def update(self, **payload):
        update = Update(update_id=next(self.sequence), **payload)
        store_update(self.db, update)
        store_update(self.db, update)
        self.assertEqual(handle_update_job(self.db, self.claim(("telegram_update",)), self.bot), "done")
        self.assertIsNone(self.claim(("telegram_update",)))
        self.send_all()

    def message(self, chat, text, reply_id=None):
        payload = {"chat": {"type": "private", "id": chat}, "from": {"id": chat}, "text": text}
        if reply_id is not None:
            payload["reply_to_message"] = {"message_id": reply_id}
        self.update(message=payload)

    def callback(self, chat, data):
        self.update(callback_query={
            "id": "case", "from": {"id": chat}, "data": data,
            "message": {"chat": {"type": "private", "id": chat}},
        })

    def scheduled(self, kind, *, expired=False, **extra):
        expires = datetime.now(timezone.utc) + timedelta(seconds=-30 if expired else 180)
        with self.db.cursor() as cursor:
            enqueue_delivery(cursor, f"test-study-{kind}-{expired}", kind, {
                "courseId": str(COURSE), "groupDayId": str(self.group_day), "day": self.day.isoformat(),
                "chatId": GROUP_CHAT, "expiresAt": expires.isoformat(), **extra,
            })

    def test_answer_and_result_recovery(self):
        self.message(STUDENT_CHAT, "/rating on")
        self.assertIn("может появиться", self.bot.messages[-1]["text"])
        self.message(STUDENT_CHAT, "/rating off")
        self.assertIn("скрыт", self.bot.messages[-1]["text"])
        self.scheduled("deliver_morning")
        self.send_all()
        self.assertEqual(self.bot.messages[-1]["chat_id"], GROUP_CHAT)
        self.assertEqual(self.bot.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["text"], "Ответить")

        self.message(STUDENT_CHAT, "/today")
        choice = self.bot.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assignment = self.db.execute("""
            SELECT id, member_id FROM assignments WHERE course_id = %s AND day = %s
        """, (COURSE, self.day)).fetchone()
        self.scheduled("deliver_daily_portion", chatId=STUDENT_CHAT,
                       assignmentId=str(assignment["id"]), memberId=str(assignment["member_id"]))
        self.send_all()
        self.assertEqual(self.bot.messages[-1]["chat_id"], STUDENT_CHAT)
        self.assertEqual(self.bot.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], choice)
        self.callback(OTHER_CHAT, choice)
        self.assertIn("недоступен", self.bot.callbacks[-1]["text"])
        self.callback(STUDENT_CHAT, choice)
        self.assertIs(self.bot.messages[-1]["reply_markup"]["force_reply"], True)
        prompt_id = self.db.execute("""
            SELECT dm_message_id FROM assignments WHERE course_id = %s AND day = %s
        """, (COURSE, self.day)).fetchone()["dm_message_id"]
        self.assertIsNotNone(prompt_id)

        self.message(OTHER_CHAT, "Попытка за другого", prompt_id)
        self.message(STUDENT_CHAT, " Первичный ключ уникален. ", prompt_id)
        self.assertIn("принят", self.bot.messages[-1]["text"])
        self.message(STUDENT_CHAT, "Второй ответ", prompt_id)
        self.assertIn("уже принят", self.bot.messages[-1]["text"])
        attempts = self.db.execute("""
            SELECT p.id, p.body FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
            WHERE a.course_id = %s AND a.day = %s
        """, (COURSE, self.day)).fetchall()
        self.assertEqual([row["body"] for row in attempts], ["Первичный ключ уникален."])
        self.db.execute("UPDATE jobs SET run_at = statement_timestamp() WHERE key = %s",
                        (f"deliver_grade_delay:{attempts[0]['id']}",))
        self.send_all()
        self.assertIn("задерживается", self.bot.messages[-1]["text"])

        grade = self.claim(("grade",))
        self.assertIsNone(self.claim(("grade",)))
        failed = Grader(FakeModel(ModelError("MODEL_FAILURE", retryable=False)))
        self.assertEqual(handle_grade_job(self.db, grade.id, grade.lease_until, failed), "failed")
        self.send_all()
        retry = self.bot.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.callback(OTHER_CHAT, retry)
        self.assertIsNone(self.claim(("grade",)))
        self.callback(STUDENT_CHAT, retry)
        grade = self.claim(("grade",))
        model = Grader(FakeModel('{"coveredIds":["p2"],"missingIds":["p1","p3"],"errors":[],"feedback":"Разбор"}'))
        self.assertEqual(handle_grade_job(self.db, grade.id, grade.lease_until, model), "done")
        outcome = self.claim(("deliver_grade",))
        self.bot.fail = True
        self.assertEqual(handle_reply_job(self.db, outcome, self.bot), "delivery_unknown")
        self.bot.fail = False
        self.message(STUDENT_CHAT, "/today")
        self.assertIn("Оценка 2/5", self.bot.messages[-1]["text"])
        self.assertIn("Эталон", self.bot.messages[-1]["text"])
        self.assertEqual(self.bot.messages[-1]["chat_id"], STUDENT_CHAT)

    def test_expired_post_and_group_summary(self):
        self.scheduled("deliver_morning", expired=True)
        self.send_all()
        self.assertFalse(self.bot.messages)
        self.scheduled("deliver_morning")
        self.bot.fail = True
        morning = self.claim(("deliver_morning",))
        self.assertEqual(handle_reply_job(self.db, morning, self.bot), "delivery_unknown")
        self.assertIsNone(self.claim(("deliver_morning",)))
        self.bot.fail = False
        self.db.execute("UPDATE group_days SET summary = %s WHERE id = %s",
                        (Jsonb({"active": 2, "responded": 1, "graded": 1, "meanScore": 4}), self.group_day))
        self.scheduled("deliver_digest")
        self.send_all()
        self.assertEqual(self.bot.messages[-1]["chat_id"], GROUP_CHAT)
        self.assertIn("1 из 2", self.bot.messages[-1]["text"])
        self.assertIn("4/5", self.bot.messages[-1]["text"])
