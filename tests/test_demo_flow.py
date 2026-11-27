import json
import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.ai.digest import DIGEST_JOB_TYPE, handle_digest_note_job
from app.ai.grading import Grader
from app.ai.jobs import handle_grade_job
from app.imports.service import handle_import_job
from app.main import app
from app.queue import claim_job
from app.rubrics.generation import handle_index_job, handle_rubric_job
from app.schedule import day_window, handle_schedule_job, schedule_courses
from app.telegram.jobs import TELEGRAM_JOB_TYPES, handle_reply_job, handle_update_job
from test_digest import NoteModel
from test_grading import FakeModel
from test_imports import pdf_text, scan
from test_miniapp import BOT_TEST_TOKEN, ORIGIN, signed_data
from test_study_flow import FakeTelegram


GROUP_CHAT = -1009000012050
ADMIN = 900000012050
STUDENTS = (900000012051, 900000012052, 900000012053)
WEBHOOK_SECRET = "synthetic-demo-webhook"
FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "demo"
POINTS = ("A primary key identifies a row.", "A primary key is unique.", "A primary key cannot contain NULL.")
EDITED_QUESTION = "What is a primary key and what constraints apply?"


class DemoTelegram(FakeTelegram):
    def get_member(self, chat_id, user_id):
        if chat_id == GROUP_CHAT:
            if user_id == ADMIN:
                return {"status": "administrator"}
            if user_id in STUDENTS:
                return {"status": "member"}
        return {"status": "left"}


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a dedicated test PostgreSQL")
@unittest.skipUnless(shutil.which(os.getenv("TESSERACT_BIN", "tesseract")), "Requires Tesseract for the demo photo")
class DemoFlowTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["BILET_TEST_DATABASE_URL"]
        self.db = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        self.job_ids, self.update_ids = [], []
        self.counter = count(900000012200)
        self.group = self.course_id = self.import_id = None
        self.addCleanup(self.remove_fixture)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage = Path(temp.name) / "uploads"
        env = patch.dict(os.environ, {
            "DATABASE_URL": self.url, "TELEGRAM_BOT_TOKEN": BOT_TEST_TOKEN, "TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "MINI_APP_URL": ORIGIN + "/app/", "IMPORT_STORAGE": str(self.storage),
        })
        env.start()
        self.addCleanup(env.stop)
        self.http = TestClient(app, base_url=ORIGIN)
        self.addCleanup(self.http.close)
        self.bot = DemoTelegram()

    def remove_fixture(self):
        self.db.execute("""
            DELETE FROM jobs WHERE id = ANY (%s) OR payload->>'courseId' = %s OR payload->>'importId' = %s
                OR payload->>'attemptId' IN (SELECT p.id::text FROM attempts AS p JOIN assignments AS a
                                             ON a.id = p.assignment_id WHERE a.course_id = %s)
        """, (self.job_ids, str(self.course_id), str(self.import_id), self.course_id))
        self.db.execute("DELETE FROM telegram_updates WHERE update_id = ANY (%s)", (self.update_ids,))
        self.db.execute("DELETE FROM study_groups WHERE telegram_chat_id = %s", (GROUP_CHAT,))
        self.db.execute("DELETE FROM users WHERE telegram_user_id = ANY (%s)", ([ADMIN, *STUDENTS],))

    def claim(self, *kinds):
        job = claim_job(self.db, kinds, 60)
        self.assertIsNotNone(job, kinds)
        self.job_ids.append(job.id)
        return job

    def drain_telegram(self):
        outgoing = tuple(kind for kind in TELEGRAM_JOB_TYPES if kind != "telegram_update")
        start = len(self.bot.messages)
        while job := claim_job(self.db, outgoing, 60):
            self.job_ids.append(job.id)
            self.assertEqual(handle_reply_job(self.db, job, self.bot), "done")
        return self.bot.messages[start:]

    def update(self, *, duplicate=False, **event):
        update_id = next(self.counter)
        self.update_ids.append(update_id)
        payload = {"update_id": update_id, **event}
        for _ in range(2 if duplicate else 1):
            response = self.http.post("/telegram/webhook", json=payload,
                                      headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(handle_update_job(self.db, self.claim("telegram_update"), self.bot), "done")
        self.assertIsNone(claim_job(self.db, ("telegram_update",), 60))
        return self.drain_telegram()

    def command(self, user, text, *, group=False, reply_to=None, duplicate=False):
        chat = {"type": "supergroup", "id": GROUP_CHAT, "title": "Демо Билет дня"} if group else {"type": "private", "id": user}
        message = {"chat": chat, "from": {"id": user}, "text": text}
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        return self.update(message=message, duplicate=duplicate)

    def login(self, telegram_id):
        client = TestClient(app, base_url=ORIGIN)
        self.addCleanup(client.close)
        response = client.post("/v1/auth/telegram", headers={"Origin": ORIGIN},
                               json={"initData": signed_data(telegram_id, datetime.now(timezone.utc))})
        self.assertEqual(response.status_code, 200, response.text)
        return client, {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrfToken"]}

    @staticmethod
    def button(message):
        return message["reply_markup"]["inline_keyboard"][0][0]

    def start_url_argument(self, message):
        return parse_qs(urlsplit(self.button(message)["url"]).query)["start"][0]

    def onboard(self):
        bind = self.start_url_argument(self.command(ADMIN, "/bind", group=True)[0])
        self.group = self.db.execute("SELECT id FROM study_groups WHERE telegram_chat_id = %s",
                                     (GROUP_CHAT,)).fetchone()["id"]
        bound = self.command(ADMIN, f"/start {bind}")
        join = self.start_url_argument(next(message for message in bound if message["chat_id"] == GROUP_CHAT))
        for student in STUDENTS:
            replies = self.command(student, f"/start {join}")
            self.assertIn("подключились", replies[0]["text"])
        admin, headers = self.login(ADMIN)
        self.assertEqual(admin.get("/v1/groups").json()["items"][0]["role"], "organizer")
        return admin, headers

    def prepare_course(self, admin, headers, day, zone):
        response = admin.post(f"/v1/groups/{self.group}/courses", headers=headers, json={"title": "Демо Базы данных"})
        self.assertEqual(response.status_code, 201, response.text)
        self.course_id = UUID(response.json()["id"])
        path = f"/v1/courses/{self.course_id}"
        settings = {"examDate": (day + timedelta(days=16)).isoformat(), "timezone": zone.key,
                    "morningAt": "00:30", "eveningAt": "23:30", "weekdays": list(range(1, 8))}
        self.assertEqual(admin.patch(path, headers=headers, json=settings).status_code, 200)
        activate = {**headers, "Idempotency-Key": "demo-activate"}
        self.assertEqual(admin.post(path + "/activate", headers=activate, json={}).status_code, 409)

        photo = BytesIO()
        scan().save(photo, "JPEG")
        response = admin.post(path + "/imports", headers={**headers, "Idempotency-Key": "demo-upload"}, files=[
            ("questionFile", ("questions.pdf", pdf_text(), "application/pdf")),
            ("files[]", ("photo.jpg", photo.getvalue(), "image/jpeg")),
            ("files[]", ("materials.txt", (FIXTURES / "materials.txt").read_bytes(), "text/plain")),
        ])
        self.assertEqual(response.status_code, 202, response.text)
        self.import_id = UUID(response.json()["importId"])
        import_path = f"/v1/imports/{self.import_id}"
        self.assertEqual(handle_import_job(self.db, self.claim("parse_import"), self.storage), "done")
        review = admin.get(import_path).json()
        self.assertEqual((review["status"], review["questions"][0]["text"]), ("review", "What is a primary key?"))
        photo_pages = self.db.execute("SELECT pages FROM sources WHERE import_id = %s AND filename = 'photo.jpg'",
                                      (self.import_id,)).fetchone()["pages"]
        self.assertIn("primary key", photo_pages[0]["text"].lower())
        review["questions"][0]["text"] = EDITED_QUESTION
        response = admin.post(import_path + "/confirm", headers={**headers, "Idempotency-Key": "demo-confirm"},
                              json={"questions": review["questions"]})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(handle_index_job(self.db, self.claim("index_import")), "done")
        self.assertEqual(handle_rubric_job(self.db, self.claim("generate_rubric"), suggest=self.rubric_response,
                                          model="demo-rubric"), "done")
        self.assertEqual(admin.get(import_path).json()["status"], "ready")
        ticket = admin.get(path + "/tickets").json()["items"][0]
        self.assertEqual(ticket["question"], EDITED_QUESTION)
        self.assertEqual(ticket["rubric"]["sourceStatus"], "sourced")
        citation = ticket["rubric"]["points"][0]["citation"]
        chunk = admin.get(f"/v1/chunks/{citation['chunkId']}").json()
        self.assertEqual(chunk["filename"], "materials.txt")
        self.assertIn(citation["quote"], chunk["body"])
        approved = admin.put(f"/v1/tickets/{ticket['id']}/rubric", headers=headers, json={
            "baseVersion": ticket["version"], "approve": True, "answer": ticket["rubric"]["answer"],
            "points": ticket["rubric"]["points"],
        })
        self.assertEqual(approved.json()["status"], "approved", approved.text)
        started = admin.post(path + "/activate", headers=activate, json={})
        self.assertEqual(started.json(), {"status": "active", "dailyTarget": 1}, started.text)
        return path, UUID(ticket["id"])

    @staticmethod
    def rubric_response(messages):
        request = json.loads(messages[-1]["content"])
        fragment = next(row for row in request["fragments"] if all(point in row["text"] for point in POINTS))
        return json.dumps({"answer": " ".join(POINTS), "points": [
            {"id": f"p{i}", "text": point, "citation": {"chunkId": fragment["id"], "quote": point}}
            for i, point in enumerate(POINTS, 1)
        ]})

    def run_schedule(self, kind, day, at):
        schedule_courses(self.db, at)
        updated = self.db.execute("""
            UPDATE jobs SET run_at = clock_timestamp() WHERE key = %s AND state = 'queued' RETURNING id
        """, (f"{kind}:{self.course_id}:{day}",)).fetchone()
        self.assertIsNotNone(updated, (kind, day))
        job = self.claim(kind)
        self.assertEqual(job.id, updated["id"])
        self.assertEqual(handle_schedule_job(self.db, job, at, digest_model=True), "done")

    def answer_tickets(self, day):
        short_answers = ("Ключ идентифицирует строку.", "По этому ключу находят запись.", "Ключ обозначает строку таблицы.")
        model = Grader(FakeModel(json.dumps({"coveredIds": ["p1"], "missingIds": ["p2", "p3"], "errors": [],
                                            "feedback": "Добавьте уникальность и ограничение NULL."}), "demo-grade"))
        for student, answer in zip(STUDENTS, short_answers, strict=True):
            portion = self.command(student, "/today")
            choice = self.button(portion[0])["callback_data"]
            self.update(callback_query={"id": "demo-ticket", "data": choice, "from": {"id": student},
                                        "message": {"chat": {"id": student, "type": "private"}}})
            assignment = self.db.execute("SELECT dm_message_id FROM assignments WHERE id = %s AND day = %s",
                                         (UUID(choice.split(":", 1)[1]), day)).fetchone()
            self.assertIsNotNone(assignment["dm_message_id"])
            acknowledgement = self.command(student, answer, reply_to=assignment["dm_message_id"], duplicate=True)
            self.assertIn("Ответ принят", acknowledgement[0]["text"])
            grade = self.claim("grade")
            self.assertEqual(handle_grade_job(self.db, grade.id, grade.lease_until, model), "done")
            result = self.drain_telegram()[0]["text"]
            self.assertIn("Оценка 2/5", result)
            self.assertIn("Эталон", result)
        attempts = self.db.execute("""
            SELECT p.id, p.acknowledged_at, p.delivered_at FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
            WHERE a.course_id = %s ORDER BY p.submitted_at
        """, (self.course_id,)).fetchall()
        self.assertEqual(len(attempts), len(STUDENTS))
        self.assertTrue(all(row["acknowledged_at"] and row["delivered_at"] for row in attempts))
        return [(row["id"], answer) for row, answer in zip(attempts, short_answers, strict=True)]

    def test_organizer_and_students_complete_the_demo(self):
        admin, headers = self.onboard()
        now = self.db.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        zone = ZoneInfo("Asia/Novosibirsk")
        if now.astimezone(zone).hour not in range(2, 22):
            zone = ZoneInfo("UTC")
        day = now.astimezone(zone).date()
        path, ticket = self.prepare_course(admin, headers, day, zone)
        course = self.db.execute("SELECT * FROM courses WHERE id = %s", (self.course_id,)).fetchone()
        window = day_window(course, day, zone)
        # Move the activation of this synthetic course before today's morning window.
        self.db.execute("UPDATE courses SET schedule_from = %s WHERE id = %s", (window.morning, self.course_id))
        self.run_schedule("morning", day, window.morning + timedelta(seconds=1))
        morning = self.drain_telegram()
        self.assertTrue(any(message["chat_id"] == GROUP_CHAT and EDITED_QUESTION in message["text"] for message in morning))
        self.run_checkpoints(admin, path, ticket, course, day, zone)

    def run_checkpoints(self, admin, path, ticket, course, day, zone):
        attempts = self.answer_tickets(day)
        student, _ = self.login(STUDENTS[0])
        card = student.get(path + "/dashboard").json()["tickets"][0]
        self.assertEqual((card["passes"], card["lastScore"]), (1, 2))
        self.assertEqual(student.get(f"/v1/attempts/{attempts[0][0]}").json()["score"], 2)
        self.assertEqual(student.get(f"/v1/attempts/{attempts[1][0]}").status_code, 404)
        self.assertEqual(admin.get(path + "/dashboard").json()["group"]["responded"], 3)

        evening = day_window(course, day, zone).evening
        self.run_schedule("digest", day, evening + timedelta(seconds=1))
        job = self.claim(DIGEST_JOB_TYPE)
        self.assertEqual(handle_digest_note_job(self.db, job, NoteModel()), "done")
        summary = self.drain_telegram()[0]
        self.assertEqual(summary["chat_id"], GROUP_CHAT)
        self.assertIn("Участников ответило: 3 из 3", summary["text"])
        self.assertIn("Средний балл: 2/5", summary["text"])
        self.assertIn("Типичная ошибка", summary["text"])
        for _, body in attempts:
            self.assertNotIn(body, summary["text"])

        first, second = day + timedelta(days=2), day + timedelta(days=9)
        for lesson, step in ((first, 1), (second, 2)):
            morning = day_window(course, lesson, zone).morning
            self.run_schedule("morning", lesson, morning + timedelta(seconds=1))
            rows = self.db.execute("SELECT kind, ticket_id FROM assignments WHERE course_id = %s AND day = %s",
                                   (self.course_id, lesson)).fetchall()
            self.assertEqual(rows, [{"kind": "repeat", "ticket_id": ticket}] * len(STUDENTS))
            group_repeat = self.db.execute("""
                SELECT due_day, state FROM repeats WHERE course_id = %s AND member_id IS NULL AND step = %s
            """, (self.course_id, step)).fetchone()
            self.assertEqual(group_repeat, {"due_day": lesson, "state": "issued"})
        print(f"Demo verified: PDF + OCR, 3/3 answers, mean 2/5, personal and group repeats {first} and {second}")
