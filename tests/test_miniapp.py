import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.imports.service import handle_import_job
from app.main import app
from app.miniapp.errors import ApiError
from app.miniapp.security import verify_init_data
from app.queue import claim_job
from app.rubrics.generation import handle_index_job, handle_rubric_job
from app.telegram.jobs import handle_update_job
from app.telegram.webhook import Update, store_update


BOT_TEST_TOKEN = "123:test-token"
ORIGIN = "https://study.example.invalid"
ADMIN, STUDENT, OUTSIDER = 900000009001, 900000009002, 900000009003
CHAT = -1009000009001


def signed_data(user_id: int, at: datetime) -> str:
    fields = {
        "query_id": "course-project-test",
        "user": json.dumps({"id": user_id, "first_name": "Тест"}, ensure_ascii=False),
        "auth_date": str(int(at.timestamp())),
        "signature": "telegram-test-signature-field",
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.digest(b"WebAppData", BOT_TEST_TOKEN.encode(), "sha256")
    fields["hash"] = hmac.new(key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class InitDataTests(unittest.TestCase):
    def test_fresh_tampered_duplicate_and_expired_data(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        data = signed_data(STUDENT, now)
        self.assertEqual(verify_init_data(data, BOT_TEST_TOKEN, now), STUDENT)
        for invalid in (data.replace(str(STUDENT), str(OUTSIDER)), data + "&auth_date=1", data + "&unknown=value"):
            with self.subTest(invalid=invalid[-30:]), self.assertRaises(ApiError) as error:
                verify_init_data(invalid, BOT_TEST_TOKEN, now)
            self.assertEqual(error.exception.code, "AUTH_INVALID")
        for time in (now - timedelta(minutes=6), now + timedelta(seconds=1)):
            with self.assertRaises(ApiError) as error:
                verify_init_data(signed_data(STUDENT, time), BOT_TEST_TOKEN, now)
            self.assertEqual(error.exception.code, "AUTH_EXPIRED")


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a dedicated test PostgreSQL")
class MiniAppTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["BILET_TEST_DATABASE_URL"]
        self.db = psycopg.connect(self.url, autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage = Path(temp.name)
        env = patch.dict(os.environ, {
            "DATABASE_URL": self.url, "TELEGRAM_BOT_TOKEN": BOT_TEST_TOKEN,
            "TELEGRAM_BOT_USERNAME": "bilet_test_bot", "MINI_APP_URL": ORIGIN + "/app/",
            "IMPORT_STORAGE": temp.name,
        })
        env.start()
        self.addCleanup(env.stop)
        self.group = self.db.execute("""
            INSERT INTO study_groups (telegram_chat_id, title) VALUES (%s, 'Группа БД') RETURNING id
        """, (CHAT,)).fetchone()["id"]
        self.addCleanup(self.remove_fixture)
        self.people = {}
        self.jobs = []
        for telegram_id, role in [(ADMIN, "organizer"), (STUDENT, "student")]:
            user = self.db.execute("""
                INSERT INTO users (telegram_user_id, dm_chat_id) VALUES (%s, %s) RETURNING id
            """, (telegram_id, telegram_id)).fetchone()["id"]
            self.people[telegram_id] = self.db.execute("""
                INSERT INTO memberships (group_id, user_id, role, alias)
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (self.group, user, role, "Настя" if role == "student" else "Организатор")).fetchone()["id"]

    def remove_fixture(self):
        self.db.execute("DELETE FROM jobs WHERE id = ANY (%s) OR key LIKE 'telegram_reply:900000009100:%%'", (self.jobs,))
        self.db.execute("DELETE FROM telegram_updates WHERE update_id = 900000009100")
        self.db.execute("DELETE FROM study_groups WHERE id = %s", (self.group,))
        self.db.execute("DELETE FROM users WHERE telegram_user_id = ANY (%s)", ([ADMIN, STUDENT, OUTSIDER],))

    def client(self):
        client = TestClient(app, base_url=ORIGIN)
        self.addCleanup(client.close)
        return client

    def login(self, telegram_id: int):
        client = self.client()
        response = client.post("/v1/auth/telegram", headers={"Origin": ORIGIN},
                               json={"initData": signed_data(telegram_id, datetime.now(timezone.utc))})
        self.assertEqual(response.status_code, 200, response.text)
        parts = response.headers["set-cookie"].lower()
        for flag in ["secure", "httponly", "samesite=lax", "max-age=28800"]:
            self.assertIn(flag, parts)
        return client, {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrfToken"]}

    def check_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code, response.text)

    def claim(self, kind):
        job = claim_job(self.db, (kind,), 60)
        self.assertIsNotNone(job)
        self.jobs.append(job.id)
        return job

    def test_miniapp_backend_workflow_and_permissions(self):
        anonymous = self.client()
        self.assertIn("Билет дня", anonymous.get("/app/").text)
        self.assertEqual(anonymous.get("/app/app.js").status_code, 200)
        self.check_error(anonymous.get("/v1/groups"), 401, "AUTH_EXPIRED")
        self.check_error(anonymous.post("/v1/auth/telegram", headers={"Origin": ORIGIN},
            json={"initData": signed_data(ADMIN, datetime.now(timezone.utc) - timedelta(minutes=6))}), 401, "AUTH_EXPIRED")

        organizer, write = self.login(ADMIN)
        student, student_write = self.login(STUDENT)
        outsider, outsider_write = self.login(OUTSIDER)
        group_path = "/v1/groups/" + str(self.group)
        self.assertEqual(outsider.get("/v1/groups").json(), {"items": []})
        groups = organizer.get("/v1/groups")
        self.assertEqual(groups.json()["items"][0]["role"], "organizer")
        self.assertEqual(groups.headers["Cache-Control"], "no-store")
        self.check_error(student.post(group_path + "/courses", headers=student_write, json={"title": "БД"}), 403, "FORBIDDEN")
        self.check_error(outsider.post(group_path + "/courses", headers=outsider_write, json={"title": "БД"}), 404, "NOT_FOUND")
        self.check_error(organizer.post(group_path + "/courses", headers={"Origin": ORIGIN}, json={"title": "БД"}), 403, "FORBIDDEN")
        self.check_error(organizer.post(group_path + "/courses", headers={**write, "Origin": "https://other.invalid"},
                                        json={"title": "БД"}), 403, "FORBIDDEN")

        response = organizer.post(group_path + "/courses", headers=write, json={"title": "Базы данных"})
        self.assertEqual(response.status_code, 201, response.text)
        course = response.json()
        course_path = "/v1/courses/" + course["id"]
        self.assertEqual(organizer.get(course_path).json()["status"], "draft")
        self.check_error(student.get(course_path), 404, "NOT_FOUND")
        self.check_error(outsider.get(course_path + "/dashboard"), 404, "NOT_FOUND")
        self.check_error(organizer.patch(course_path, headers=write, json={"morningAt": "23:59", "eveningAt": "08:00"}),
                         400, "INVALID_INPUT")
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        settings = {
            "examDate": (tomorrow + timedelta(days=1)).isoformat(), "timezone": "UTC",
            "morningAt": "09:00", "eveningAt": "20:00", "weekdays": [tomorrow.isoweekday()],
        }
        self.assertEqual(organizer.patch(course_path, headers=write, json=settings).status_code, 200)
        activation = {**write, "Idempotency-Key": "activate-tg03"}
        self.check_error(organizer.post(course_path + "/activate", headers=activation, json={}), 409, "STATE_CONFLICT")

        files = [("questionText", (None, "Что такое ключ?")), ("files[]", ("source.txt", b"Source notes", "text/plain"))]
        import_headers = {**write, "Idempotency-Key": "upload-tg03"}
        response = organizer.post(course_path + "/imports", headers=import_headers, files=files)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(organizer.post(course_path + "/imports", headers=import_headers, files=files).json(), response.json())
        import_path = "/v1/imports/" + response.json()["importId"]
        self.assertEqual(handle_import_job(self.db, self.claim("parse_import"), self.storage), "done")
        review = organizer.get(import_path).json()
        self.assertEqual(review["status"], "review")
        self.assertEqual(review["questions"][0]["text"], "Что такое ключ?")
        self.check_error(student.get(import_path), 403, "FORBIDDEN")
        confirm = organizer.post(import_path + "/confirm", headers={**write, "Idempotency-Key": "confirm-tg03"},
                                 json={"questions": review["questions"]})
        self.assertEqual(confirm.status_code, 202, confirm.text)
        self.assertEqual(handle_index_job(self.db, self.claim("index_import")), "done")
        self.assertEqual(handle_rubric_job(self.db, self.claim("generate_rubric")), "done")
        self.assertEqual(organizer.get(import_path).json()["status"], "ready")

        item = organizer.get(course_path + "/tickets").json()["items"][0]
        rubric_path = "/v1/tickets/" + item["id"] + "/rubric"
        rubric = {
            "baseVersion": item["version"], "answer": "Свойства ключа.", "approve": True, "acknowledgeUnsourced": True,
            "points": [{"id": f"p{i}", "text": label, "citation": None}
                       for i, label in enumerate(("Идентифицирует", "Уникален", "Нет NULL"), 1)],
        }
        self.check_error(student.put(rubric_path, headers=student_write, json=rubric), 403, "FORBIDDEN")
        self.assertEqual(organizer.put(rubric_path, headers=write, json=rubric).json()["status"], "approved")
        self.check_error(organizer.put(rubric_path, headers=write, json=rubric), 409, "STATE_CONFLICT")
        self.check_error(organizer.post(course_path + "/activate", headers=activation, json={}), 409, "LOAD_ACK_REQUIRED")
        active = organizer.post(course_path + "/activate", headers=activation, json={"acknowledgeLoad": True})
        self.assertEqual(active.json(), {"status": "active", "dailyTarget": 2}, active.text)
        self.assertEqual(organizer.post(course_path + "/activate", headers=activation,
                                        json={"acknowledgeLoad": True}).json(), active.json())
        self.assertEqual(organizer.post(course_path + "/pause", headers=write).json()["status"], "paused")
        self.assertEqual(organizer.post(course_path + "/resume", headers=write).json()["status"], "active")

        assignment = self.db.execute("""
            INSERT INTO assignments (course_id, group_id, member_id, ticket_id, day, rubric_id, kind, state)
            SELECT course_id, %s, %s, id, current_date, approved_rubric_id, 'daily','answered'
            FROM tickets WHERE id = %s RETURNING id
        """, (self.group, self.people[STUDENT], UUID(item["id"]))).fetchone()["id"]
        result = Jsonb({"score": 5, "coveredIds": ["p1", "p2", "p3"], "missingIds": [], "errors": [], "feedback": "Верно."})
        attempt = self.db.execute("""
            INSERT INTO attempts (assignment_id, telegram_update_id, body, status, graded_at, result)
            VALUES (%s, 900000009101, 'Ответ ученика', 'graded', clock_timestamp(), %s) RETURNING id
        """, (assignment, result)).fetchone()["id"]
        cards = student.get(course_path + "/dashboard").json()
        self.assertEqual(cards["tickets"][0]["passes"], 1)
        self.assertNotIn("group", cards)
        self.assertEqual(organizer.get(course_path + "/dashboard").json()["group"]["responded"], 1)
        self.assertEqual(student.get(f"/v1/attempts/{attempt}").json()["covered"], ["Идентифицирует", "Уникален", "Нет NULL"])
        self.check_error(organizer.get(f"/v1/attempts/{attempt}"), 404, "NOT_FOUND")

    def test_teacher_invite_is_bound_to_connected_member(self):
        organizer, write = self.login(ADMIN)
        response = organizer.post(f"/v1/groups/{self.group}/teacher-invites",
                                  headers=write, json={"memberId": str(self.people[STUDENT])})
        self.assertEqual(response.status_code, 201, response.text)
        secret = parse_qs(urlsplit(response.json()["startUrl"]).query)["start"][0]

        class TestBot:
            def get_member(self, chat_id, user_id):
                return {"status": "member"}

        store_update(self.db, Update(update_id=900000009100, message={
            "chat": {"id": STUDENT, "type": "private"}, "from": {"id": STUDENT}, "text": "/start " + secret,
        }))
        self.assertEqual(handle_update_job(self.db, self.claim("telegram_update"), TestBot()), "done")
        student, _ = self.login(STUDENT)
        self.assertEqual(student.get("/v1/groups").json()["items"][0]["role"], "teacher")
        self.assertEqual(self.db.execute("""
            SELECT count(*) AS count FROM action_tokens
            WHERE group_id = %s AND kind = 'teacher' AND consumed_at IS NOT NULL
        """, (self.group,)).fetchone()["count"], 1)
