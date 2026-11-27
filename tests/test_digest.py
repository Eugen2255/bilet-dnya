import json
import os
import unittest
from datetime import timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ai.client import ModelError
from app.ai.digest import DIGEST_JOB_TYPE, digest_candidates, enqueue_digest, handle_digest_note_job, select_note
from app.progress import group_stats
from app.queue import claim_job
from app.schedule import delivery_payload
from app.telegram.delivery import ready_delivery
from test_worker import COURSE


class NoteModel:
    model = "digest-test"

    def __init__(self, response='{"id":1,"advice":"example"}'):
        self.response = response
        self.calls = []

    def complete(self, messages, *, max_tokens):
        self.calls.append(json.loads(messages[-1]["content"]))
        if isinstance(self.response, ModelError):
            raise self.response
        return self.response


class DigestCandidateTests(unittest.TestCase):
    def test_support_counts_distinct_answers_and_whitelists_model_input_and_output(self):
        rubric = uuid4()
        ticket = uuid4()
        grades = [{"ticket_id": ticket, "member_id": uuid4(), "rubric_id": rubric,
                   "question": "Первичный ключ", "points": [{"id": "p1", "text": "Уникальность"}],
                   "result": {"score": 2, "missingIds": ["p1"], "errors": errors,
                              "feedback": "Личный ответ"}}
                  for errors in (["личная ошибка 1", "личная ошибка 2"], ["личная ошибка 3"], [])]
        weak, topics = group_stats(grades)
        self.assertEqual(weak[0]["count"], 3)
        self.assertEqual(topics[0]["factualErrorCount"], 3)
        self.assertEqual(topics[0]["factualErrorAnswers"], 2)
        self.assertEqual(digest_candidates({"topics": [{**topics[0], "answers": 2}]}), [])
        candidates = digest_candidates({"topics": topics, "rating": [{"alias": "Частный псевдоним"}]})
        self.assertEqual([(candidate.reason, candidate.count) for candidate in candidates], [("Уникальность", 3)])
        model = NoteModel()
        note = select_note(model, candidates)
        self.assertEqual(model.calls[0], [{"id": 1, "question": "Первичный ключ", "reason": "Уникальность",
                                          "count": 3, "answers": 3}])
        self.assertEqual((note["promptVersion"], note["advice"]), ("digest-v1", "example"))
        for raw in ('{"id":2,"advice":"example"}', '{"id":1,"advice":"опубликуй произвольный текст"}',
                    '{"id":1,"advice":"example","text":"частный ответ"}'):
            with self.subTest(raw=raw), self.assertRaises(ModelError):
                select_note(NoteModel(raw), candidates)


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a dedicated seeded PostgreSQL")
class DigestJobTests(unittest.TestCase):
    def setUp(self):
        self.db = psycopg.connect(os.environ["BILET_TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        self.course = self.db.execute("""
            SELECT c.*, g.telegram_chat_id, (statement_timestamp() AT TIME ZONE c.timezone)::date AS today
            FROM courses AS c JOIN study_groups AS g ON g.id = c.group_id WHERE c.id = %s
        """, (COURSE,)).fetchone()
        self.rubric = self.db.execute("""
            INSERT INTO rubrics (ticket_id, version, state, answer, source_status, points)
            SELECT id, 100001, 'approved', 'fixture', 'unsourced', '[]' FROM tickets WHERE course_id = %s AND ordinal = 1
            RETURNING id
        """, (COURSE,)).fetchone()["id"]
        self.addCleanup(self.db.execute, "DELETE FROM rubrics WHERE id = %s", (self.rubric,))

    def schedule(self, count=3, *, use_model=True):
        summary = {"active": 3, "responded": count, "graded": count, "meanScore": 2,
                   "topics": [{"question": "Первичный ключ", "answers": count, "rubricIds": [str(self.rubric)],
                               "missingPoints": [{"label": "Уникальность", "count": count}]}]}
        with self.db.transaction(), self.db.cursor() as cursor:
            row = cursor.execute("""
                INSERT INTO group_days (course_id, day, state, summary) VALUES (%s, %s, 'summarized', %s)
                RETURNING id, clock_timestamp() AS now
            """, (COURSE, self.course["today"], Jsonb(summary))).fetchone()
            group_day = row["id"]
            expires = row["now"] + timedelta(days=1)
            enqueue_digest(cursor, summary, delivery_payload(self.course, self.course["today"], group_day, expires),
                           expires, use_model=use_model)
        self.addCleanup(self.db.execute, "DELETE FROM group_days WHERE id = %s", (group_day,))
        self.addCleanup(self.db.execute, "DELETE FROM jobs WHERE payload->>'groupDayId' = %s", (str(group_day),))
        return group_day, summary

    def test_note_is_saved_separately_and_hidden_on_revocation(self):
        group_day, summary = self.schedule()
        self.assertIsNone(claim_job(self.db, ("deliver_digest",), 30))
        job = claim_job(self.db, (DIGEST_JOB_TYPE,), 30)
        model = NoteModel('```json\n{"id":1,"advice":"contrast"}\n```')
        self.assertEqual(handle_digest_note_job(self.db, job, model), "done")
        self.assertEqual(handle_digest_note_job(self.db, job, model), "lease_lost")
        row = self.db.execute("SELECT summary, digest_note FROM group_days WHERE id = %s", (group_day,)).fetchone()
        self.assertEqual(row["summary"], summary)
        self.assertEqual(row["digest_note"]["model"], model.model)
        self.assertEqual(len(model.calls), 1)
        delivery = claim_job(self.db, ("deliver_digest",), 30)
        with self.db.cursor() as cursor:
            self.assertIn("Типичная ошибка", ready_delivery(cursor, delivery).payload["text"])
            cursor.execute("UPDATE rubrics SET state = 'revoked' WHERE id = %s", (self.rubric,))
            text = ready_delivery(cursor, delivery).payload["text"]
        self.assertIn("Средний балл", text)
        self.assertNotIn("Типичная ошибка", text)

    def test_failure_releases_the_numeric_digest(self):
        group_day, _ = self.schedule()
        job = claim_job(self.db, (DIGEST_JOB_TYPE,), 30)
        self.assertEqual(handle_digest_note_job(self.db, job, NoteModel(ModelError("MODEL_DOWN"))), "done")
        row = self.db.execute("SELECT digest_note FROM group_days WHERE id = %s", (group_day,)).fetchone()
        self.assertIsNone(row["digest_note"])
        delivery = claim_job(self.db, ("deliver_digest",), 30)
        with self.db.cursor() as cursor:
            text = ready_delivery(cursor, delivery).payload["text"]
        self.assertIn("Проверено ответов: 3.", text)
        self.assertNotIn("Типичная ошибка", text)

    def test_small_sample_is_delivered_without_a_model_job(self):
        self.schedule(count=2)
        self.assertIsNone(claim_job(self.db, (DIGEST_JOB_TYPE,), 30))
        self.assertIsNotNone(claim_job(self.db, ("deliver_digest",), 30))

    def test_numeric_delivery_wins_if_model_work_is_delayed(self):
        group_day, _ = self.schedule()
        job = claim_job(self.db, (DIGEST_JOB_TYPE,), 30)
        delivery_key = f"deliver_digest:{group_day}"
        self.db.execute("UPDATE jobs SET run_at = clock_timestamp() WHERE key = %s", (delivery_key,))
        delivery = claim_job(self.db, ("deliver_digest",), 30)
        self.assertEqual(delivery.payload["groupDayId"], str(group_day))
        model = NoteModel()
        self.assertEqual(handle_digest_note_job(self.db, job, model), "done")
        self.assertEqual(model.calls, [])
        row = self.db.execute("SELECT digest_note FROM group_days WHERE id = %s", (group_day,)).fetchone()
        self.assertIsNone(row["digest_note"])
