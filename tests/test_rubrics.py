import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ai.client import ModelError
from app.imports.errors import ImportProblem
from app.imports.service import Upload, create_import, handle_import_job, read_import
from app.queue import claim_job
from app.rubrics.drafts import generate_draft
from app.rubrics.generation import handle_index_job, handle_rubric_job, split_material
from app.rubrics.service import confirm_import, list_tickets, read_source_chunk, update_rubric
from test_imports import EDITOR, STUDENT_MEMBER


GROUP = UUID("10000000-0000-4000-8000-000000000001")
PASSAGES = [
    "Первичный ключ однозначно идентифицирует строку.",
    "Значения ключа уникальны.",
    "NULL в первичном ключе недопустим.",
]


class RubricDraftTests(unittest.TestCase):
    def test_overlap_and_locator(self):
        text = "x" * 1050 + "y" * 150 + "z" * 300
        chunks = list(split_material([
            {"locator": {"page": 4}, "text": text},
            {"locator": {"paragraph": 22}, "text": "Ещё один фрагмент."},
        ]))
        self.assertEqual([len(body) for _, _, body in chunks], [1200, 450, 18])
        self.assertEqual(chunks[0][2][-150:], chunks[1][2][:150])
        self.assertEqual([locator for _, locator, _ in chunks], [{"page": 4}, {"page": 4}, {"paragraph": 22}])

    def test_fabricated_and_out_of_context_citations_are_removed(self):
        allowed, unknown = uuid4(), uuid4()
        raw = {
            "answer": "Уникальный идентификатор строки.",
            "points": [
                {"text": PASSAGES[0], "citation": {"chunkId": str(allowed), "quote": PASSAGES[0]}},
                {"text": PASSAGES[1], "citation": {"chunkId": str(allowed), "quote": "Другая цитата"}},
                {"text": PASSAGES[2], "citation": {"chunkId": str(unknown), "quote": PASSAGES[2]}},
            ],
        }
        draft = generate_draft("Первичный ключ", [{"id": allowed, "body": " ".join(PASSAGES)}],
                               lambda _: json.dumps(raw))
        self.assertEqual(draft["sourceStatus"], "unsourced")
        self.assertEqual([bool(point["citation"]) for point in draft["points"]], [True, False, False])


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a seeded test PostgreSQL")
class RubricIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.db = psycopg.connect(os.environ["BILET_TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row)
        self.addCleanup(self.db.close)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage = Path(temp.name)
        txn = self.db.transaction(force_rollback=True)
        txn.__enter__()
        self.addCleanup(txn.__exit__, None, None, None)

    def course(self, title):
        return self.db.execute("""
            INSERT INTO courses (group_id, title, exam_date, timezone, morning_at, evening_at, weekdays)
            VALUES (%s, %s, current_date + 30, 'UTC', '09:00', '20:00', ARRAY[1,2,3,4,5,6,7]::smallint[])
            RETURNING id
        """, (GROUP, title)).fetchone()["id"]

    def imported(self, course, key, *, material=True):
        materials = (Upload("lecture.txt", " ".join(PASSAGES).encode()),) if material else ()
        ack = create_import(self.db, course, EDITOR, key, storage=self.storage,
                            question_text="Что такое первичный ключ?", materials=materials)
        self.assertEqual(handle_import_job(self.db, claim_job(self.db, ("parse_import",), 60), self.storage), "done")
        return UUID(ack["importId"])

    def test_edit_generate_approve_and_keep_old_assignments(self):
        course = self.course("Основы баз данных")
        import_id = self.imported(course, "target")
        questions = [
            {"ordinal": 1, "text": "Объясните первичный ключ"},
            {"ordinal": 2, "text": "Назовите стадии компиляции"},
        ]
        ack = confirm_import(self.db, import_id, EDITOR, "confirm-1", questions)
        self.assertEqual(ack, confirm_import(self.db, import_id, EDITOR, "confirm-1", questions))
        with self.assertRaises(ImportProblem) as conflict:
            confirm_import(self.db, import_id, EDITOR, "confirm-1", questions[:1])
        self.assertEqual(conflict.exception.code, "STATE_CONFLICT")
        self.assertEqual(handle_index_job(self.db, claim_job(self.db, ("index_import",), 60)), "done")
        self.assertEqual(len(list_tickets(self.db, course, EDITOR)["items"]), 2)
        self.assertIsNone(list_tickets(self.db, course, STUDENT_MEMBER))

        chunks = self.db.execute("SELECT id, locator FROM chunks WHERE course_id = %s", (course,)).fetchall()
        self.assertEqual(len(chunks), 1)
        fragment = chunks[0]["id"]
        self.assertEqual(read_source_chunk(self.db, fragment, EDITOR)["locator"], {"page": 1})
        self.assertIsNone(read_source_chunk(self.db, fragment, STUDENT_MEMBER))

        another = self.course("Другой курс той же группы")
        other_import = self.imported(another, "other")
        foreign = self.db.execute("""
            INSERT INTO chunks (source_id, course_id, ordinal, locator, body)
            SELECT id, course_id, 1, '{"page":1}', %s FROM sources WHERE import_id = %s AND kind = 'material'
            RETURNING id
        """, (" ".join(PASSAGES), other_import)).fetchone()["id"]

        def fake_model(messages):
            context = json.loads(messages[-1]["content"])
            if "компиляции" in context["question"]:
                self.assertEqual(context["fragments"], [])
            else:
                self.assertEqual([row["id"] for row in context["fragments"]], [str(fragment)])
            return json.dumps({
                "answer": "Короткий ответ.", "points": [
                    {"text": text, "citation": {"chunkId": str(fragment), "quote": text}} for text in PASSAGES
                ],
            })

        for status in ("generating", "ready"):
            job = claim_job(self.db, ("generate_rubric",), 60)
            self.assertEqual(handle_rubric_job(self.db, job, suggest=fake_model, model="test-qwen"), "done")
            self.assertEqual(read_import(self.db, import_id, EDITOR)["status"], status)
        self.assertIsNone(claim_job(self.db, ("generate_rubric",), 60))
        good, unsourced = list_tickets(self.db, course, EDITOR)["items"]
        self.assertEqual(good["rubric"]["sourceStatus"], "sourced")
        self.assertEqual(unsourced["rubric"]["sourceStatus"], "unsourced")
        self.assertTrue(all(not p["citation"] for p in unsourced["rubric"]["points"]))

        good_id, other_id = UUID(good["id"]), UUID(unsourced["id"])
        good_points, unsourced_points = good["rubric"]["points"], unsourced["rubric"]["points"]
        invalid = [*good_points[1:], {**good_points[0], "citation": {"chunkId": str(foreign), "quote": PASSAGES[0]}}]
        with self.assertRaises(ImportProblem) as invalid_source:
            update_rubric(self.db, good_id, EDITOR, base_version=1, answer="Ответ.", points=invalid, approve=True)
        self.assertEqual(invalid_source.exception.code, "INVALID_CITATION")
        with self.assertRaises(ImportProblem) as required_ack:
            update_rubric(self.db, other_id, EDITOR, base_version=1, answer="Ответ.", points=unsourced_points,
                          approve=True)
        self.assertEqual(required_ack.exception.code, "STATE_CONFLICT")

        approved = update_rubric(self.db, good_id, EDITOR, base_version=1, answer="Первый ответ.", points=good_points,
                                 approve=True)
        self.assertEqual(approved, {"version": 2, "status": "approved", "sourceStatus": "sourced"})
        update_rubric(self.db, other_id, EDITOR, base_version=1, answer="Ответ.", points=unsourced_points,
                      approve=True, acknowledge_unsourced=True)
        self.db.execute("UPDATE courses SET state = 'closed' WHERE group_id = %s AND state = 'active'", (GROUP,))
        self.db.execute("UPDATE courses SET state = 'active' WHERE id = %s", (course,))
        assignment = self.db.execute("""
            INSERT INTO assignments (course_id, group_id, member_id, ticket_id, day, rubric_id, kind, state)
            SELECT course_id, %s, %s, id, current_date, approved_rubric_id, 'daily', 'open'
            FROM tickets WHERE id = %s RETURNING id, rubric_id
        """, (GROUP, STUDENT_MEMBER, good_id)).fetchone()
        later = update_rubric(self.db, good_id, EDITOR, base_version=2, answer="Исправленный ответ.",
                              points=good_points, approve=True)
        self.assertEqual(later["version"], 3)
        with self.assertRaises(ImportProblem) as stale:
            update_rubric(self.db, good_id, EDITOR, base_version=2, answer="Устаревший ответ.",
                          points=good_points, approve=True)
        self.assertEqual(stale.exception.code, "STATE_CONFLICT")
        old = self.db.execute("""
            SELECT a.rubric_id, r.answer FROM assignments AS a JOIN rubrics AS r ON r.id = a.rubric_id WHERE a.id = %s
        """, (assignment["id"],)).fetchone()
        self.assertEqual(old, {"rubric_id": assignment["rubric_id"], "answer": "Первый ответ."})
        with self.assertRaises(psycopg.errors.CheckViolation), self.db.transaction():
            self.db.execute("UPDATE rubrics SET answer = 'Подмена' WHERE id = %s", (old["rubric_id"],))
        self.assertEqual(confirm_import(self.db, import_id, EDITOR, "confirm-1", questions), ack)
        fresh = self.imported(course, "replace-after-activation")
        with self.assertRaises(ImportProblem) as active:
            confirm_import(self.db, fresh, EDITOR, "confirm-2", questions)
        self.assertEqual(active.exception.code, "STATE_CONFLICT")

    def test_generation_waits_after_rate_limit_then_leaves_manual_draft(self):
        course = self.course("Без материалов")
        import_id = self.imported(course, "rate-test", material=False)
        confirm_import(self.db, import_id, EDITOR, "rate-confirm", [{"ordinal": 1, "text": "Откуда берётся ключ?"}])
        self.assertEqual(handle_index_job(self.db, claim_job(self.db, ("index_import",), 60)), "done")

        def limited(_):
            raise ModelError("MODEL_HTTP_429", retry_after=25)

        job = claim_job(self.db, ("generate_rubric",), 60)
        self.assertEqual(handle_rubric_job(self.db, job, suggest=limited), "queued")
        delay = self.db.execute("SELECT extract(epoch FROM run_at-clock_timestamp()) AS delay FROM jobs WHERE id = %s",
                                (job.id,)).fetchone()["delay"]
        self.assertAlmostEqual(float(delay), 25, delta=2)
        self.db.execute("UPDATE jobs SET tries = 2, run_at = clock_timestamp() WHERE id = %s", (job.id,))
        self.assertEqual(handle_rubric_job(self.db, claim_job(self.db, ("generate_rubric",), 60), suggest=limited),
                         "done")
        item = list_tickets(self.db, course, EDITOR)["items"][0]
        self.assertEqual(item["rubric"], {"answer": "", "sourceStatus": "unsourced", "points": []})
        result = read_import(self.db, import_id, EDITOR)
        self.assertEqual(result["status"], "ready")
        self.assertIn("вручную", result["warnings"][-1])
