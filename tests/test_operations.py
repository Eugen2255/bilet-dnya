import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
from time import monotonic, perf_counter, sleep
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ai.grading import Grader
from app.main import app
from app.operations import metrics, purge_expired, replay_restored
from app.queue import claim_job
from app.telegram.jobs import handle_reply_job, handle_update_job
from app.telegram.webhook import Update, store_update
from app.worker import run
from test_grading import FakeModel
from test_miniapp import BOT_TEST_TOKEN, ORIGIN, signed_data
from test_study_flow import FakeTelegram


ADMIN = 900000011000
STUDENTS = list(range(900000011001, 900000011011))
CHAT = -1009000011000
POINTS = [{"id": f"p{i}", "text": f"Критерий {i}"} for i in range(1, 4)]
GRADE = json.dumps({"coveredIds": ["p1", "p2", "p3"], "missingIds": [], "errors": [], "feedback": "Разбор"})


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a dedicated PostgreSQL")
class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["BILET_TEST_DATABASE_URL"]
        self.db = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage = Path(temp.name) / "uploads"
        self.ledger = Path(temp.name) / "privacy" / "ledger.sqlite3"
        env = patch.dict(os.environ, {"DATABASE_URL": self.url, "IMPORT_STORAGE": str(self.storage),
                                     "PRIVACY_LEDGER_PATH": str(self.ledger), "TELEGRAM_BOT_TOKEN": BOT_TEST_TOKEN,
                                     "TELEGRAM_WEBHOOK_SECRET": "test-webhook", "MINI_APP_URL": ORIGIN + "/app/"})
        env.start()
        self.addCleanup(env.stop)
        self.now = self.db.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        self.day = self.now.date()
        self.counter = count(900000011100)
        self.bot = FakeTelegram()
        self.group = self.db.execute("INSERT INTO study_groups (telegram_chat_id) VALUES (%s) RETURNING id",
                                     (CHAT,)).fetchone()["id"]
        self.addCleanup(self.clear_fixture)
        self.members = {}
        self.users = {}

    def clear_fixture(self):
        self.db.execute("""
            DELETE FROM jobs WHERE payload->>'courseId' IN (SELECT id::text FROM courses WHERE group_id = %s)
                OR payload->>'attemptId' IN (SELECT p.id::text FROM attempts AS p JOIN assignments AS a
                                             ON a.id = p.assignment_id WHERE a.group_id = %s)
                OR key LIKE 'telegram_update:9000000111%%' OR key LIKE 'telegram_reply:9000000111%%'
                OR key LIKE 'telegram_callback:9000000111%%'
        """, (self.group, self.group))
        self.db.execute("DELETE FROM telegram_updates WHERE update_id BETWEEN 900000011100 AND 900000011199")
        self.db.execute("DELETE FROM study_groups WHERE id = %s", (self.group,))
        self.db.execute("DELETE FROM users WHERE telegram_user_id = ANY (%s)", ([ADMIN, *STUDENTS],))

    def member(self, chat, role="student"):
        self.users[chat] = self.db.execute("""
            INSERT INTO users (telegram_user_id, dm_chat_id) VALUES (%s, %s) RETURNING id
        """, (chat, chat)).fetchone()["id"]
        self.members[chat] = self.db.execute("""
            INSERT INTO memberships (user_id, group_id, role, alias) VALUES (%s, %s, %s, %s) RETURNING id
        """, (self.users[chat], self.group, role, f"participant-{chat}")).fetchone()["id"]

    def course(self, exam=None):
        course = self.db.execute("""
            INSERT INTO courses (group_id, title, exam_date, timezone, morning_at, evening_at, weekdays)
            VALUES (%s, 'Тестовый курс удаления', %s, 'UTC', '08:00', '20:00', '{1,2,3,4,5,6,7}') RETURNING id
        """, (self.group, exam or self.day + timedelta(days=40))).fetchone()["id"]
        ticket = self.db.execute("INSERT INTO tickets (course_id, ordinal, question) VALUES (%s, 1, 'Вопрос') RETURNING id",
                                 (course,)).fetchone()["id"]
        rubric = self.db.execute("""
            INSERT INTO rubrics (ticket_id, version, state, answer, source_status, points)
            VALUES (%s, 1, 'approved', 'Эталон', 'unsourced', %s) RETURNING id
        """, (ticket, Jsonb(POINTS))).fetchone()["id"]
        self.db.execute("UPDATE tickets SET approved_rubric_id = %s WHERE id = %s", (rubric, ticket))
        if exam is None:
            self.db.execute("UPDATE courses SET state = 'active' WHERE id = %s", (course,))
        return course, ticket, rubric

    def assignment(self, course, ticket, rubric, chat, *, graded=False):
        message = next(self.counter)
        assignment = self.db.execute("""
            INSERT INTO assignments (course_id, group_id, member_id, day, ticket_id, rubric_id, kind, state, dm_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, 'daily', %s, %s) RETURNING id
        """, (course, self.group, self.members[chat], self.day, ticket, rubric, "answered" if graded else "open",
              message)).fetchone()["id"]
        if not graded:
            return message
        return self.db.execute("""
            INSERT INTO attempts (assignment_id, telegram_update_id, body, status, result, graded_at)
            VALUES (%s, %s, 'private fixture', 'graded', %s, now()) RETURNING id
        """, (assignment, next(self.counter), Jsonb({"score": 1, "feedback": "private fixture", "errors": ["text"]}))).fetchone()["id"]

    def telegram(self, **payload):
        store_update(self.db, Update(update_id=next(self.counter), **payload))
        job = claim_job(self.db, ("telegram_update",), 60)
        self.assertEqual(handle_update_job(self.db, job, self.bot), "done")
        while job := claim_job(self.db, ("telegram_reply", "telegram_callback"), 60):
            self.assertEqual(handle_reply_job(self.db, job, self.bot), "done")

    def test_delete_requires_own_confirmation_and_replays_on_restore(self):
        first, other = STUDENTS[:2]
        for chat in (first, other):
            self.member(chat)
        course, ticket, rubric = self.course()
        attempt = self.assignment(course, ticket, rubric, first, graded=True)
        self.db.execute("INSERT INTO rubric_reports (rubric_id, member_id, created_at) VALUES (%s, %s, now())",
                        (rubric, self.members[first]))
        self.db.execute("INSERT INTO group_days (course_id, day, state, summary) VALUES (%s, %s, 'summarized', %s)",
                        (course, self.day, Jsonb({"meanScore": 1, "rating": [{"memberId": str(self.members[first]), "alias": "private"}]})))
        self.telegram(message={"chat": {"type": "private", "id": first}, "from": {"id": first}, "text": "/delete_me"})
        token = self.bot.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        for sender, word in [(other, "недоступно"), (first, "удалены")]:
            self.telegram(callback_query={"id": "confirm", "data": token, "from": {"id": sender},
                                          "message": {"chat": {"type": "private", "id": sender}}})
            self.assertIn(word, self.bot.callbacks[-1]["text"])
        self.assertIsNone(self.db.execute("SELECT id FROM attempts WHERE id = %s", (attempt,)).fetchone())
        self.assertIsNone(self.db.execute("SELECT * FROM rubric_reports WHERE rubric_id = %s", (rubric,)).fetchone())
        self.assertEqual(self.db.execute("SELECT summary FROM group_days WHERE course_id = %s", (course,)).fetchone()["summary"],
                         {"meanScore": 1, "rating": []})
        # Reintroduce the old account as an earlier snapshot would.
        self.db.execute("INSERT INTO users (id, telegram_user_id) VALUES (%s, %s)", (self.users[first], first))
        self.assertEqual(replay_restored(self.db, self.ledger), 1)
        self.assertEqual(replay_restored(self.db, self.ledger), 0)
        self.member(first)
        self.assertEqual(replay_restored(self.db, self.ledger), 0)

    def test_retention_and_explicit_course_delete(self):
        chat = STUDENTS[0]
        self.member(chat)
        self.member(ADMIN, "organizer")
        course, ticket, rubric = self.course(exam=self.day - timedelta(days=6))
        attempt = self.assignment(course, ticket, rubric, chat, graded=True)
        self.assertEqual(purge_expired(self.db, self.storage, as_of=self.now)["scrubbed_answers"], 0)
        self.assertEqual(purge_expired(self.db, self.storage, as_of=self.now + timedelta(days=1))["scrubbed_answers"], 1)
        self.assertEqual(self.db.execute("SELECT body, result FROM attempts WHERE id = %s", (attempt,)).fetchone(),
                         {"body": None, "result": {"score": 1, "errorCount": 1}})
        self.assertEqual(purge_expired(self.db, self.storage, as_of=self.now + timedelta(days=24))["removed_courses"], 1)
        selected, _, _ = self.course()
        for user, status in [(chat, 403), (ADMIN, 200)]:
            with TestClient(app, base_url=ORIGIN) as client:
                login = client.post("/v1/auth/telegram", headers={"Origin": ORIGIN},
                                    json={"initData": signed_data(user, datetime.now(timezone.utc))})
                self.assertEqual(login.status_code, 200)
                headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrfToken"]}
                if user == ADMIN:
                    bad = client.request("DELETE", f"/v1/courses/{selected}", json={"confirmTitle": "Wrong"}, headers=headers)
                    self.assertEqual(bad.status_code, 400)
                deleted = client.request("DELETE", f"/v1/courses/{selected}",
                                         json={"confirmTitle": "Тестовый курс удаления"}, headers=headers)
                self.assertEqual(deleted.status_code, status, deleted.text)

    def test_ten_parallel_webhook_answers_record_visible_latencies(self):
        course, ticket, rubric = self.course()
        updates = []
        for chat in STUDENTS:
            self.member(chat)
            prompt = self.assignment(course, ticket, rubric, chat)
            updates.append({"update_id": next(self.counter), "message": {
                "chat": {"id": chat, "type": "private"}, "from": {"id": chat}, "text": "Синтетический ответ",
                "reply_to_message": {"message_id": prompt},
            }})
        stop = Event()
        model = Grader(FakeModel(GRADE))
        worker = Thread(target=run, args=(self.url, model, stop, self.bot), daemon=True)
        worker.start()
        try:
            gate = Barrier(len(updates))
            with TestClient(app, base_url=ORIGIN) as client, ThreadPoolExecutor(max_workers=len(updates)) as pool:
                def post(update):
                    gate.wait(timeout=5)
                    return client.post("/telegram/webhook", json=update,
                                       headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook"}).status_code

                self.assertEqual(list(pool.map(post, updates)), [200] * 10)
            until = monotonic() + 20
            while monotonic() < until:
                with self.db.cursor() as cursor:
                    sample = metrics(cursor)["delivery"]
                if sample["delivered"] == 10 and sample["acknowledged"] == 10:
                    break
                sleep(0.1)
            self.assertEqual((sample["submitted"], sample["acknowledged"], sample["delivered"]), (10, 10, 10), sample)
            with TestClient(app, base_url=ORIGIN) as client, ThreadPoolExecutor(max_workers=10) as pool:
                login = client.post("/v1/auth/telegram", headers={"Origin": ORIGIN},
                                    json={"initData": signed_data(STUDENTS[0], datetime.now(timezone.utc))})
                self.assertEqual(login.status_code, 200, login.text)
                gate = Barrier(10)

                def read_map(_):
                    gate.wait(timeout=5)
                    started = perf_counter()
                    response = client.get(f"/v1/courses/{course}/dashboard")
                    elapsed_ms = round((perf_counter() - started) * 1000)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["tickets"][0]["lastScore"], 5)
                    return elapsed_ms

                # For 10 observations, the nearest-rank 95th percentile is the maximum.
                map_p95 = max(pool.map(read_map, range(10)))
            print(f"Synthetic 10-answer P95: acknowledgement {sample['acknowledgement_p95_ms']} ms, "
                  f"grade delivery {sample['grade_delivery_p95_ms']} ms, dashboard {map_p95} ms")
        finally:
            stop.set()
            worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
