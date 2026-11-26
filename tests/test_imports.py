import os
import shutil
import time
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID
from zipfile import ZipFile

import psycopg
from PIL import Image, ImageDraw, ImageFont

from app.imports.documents import MAX_FILE_BYTES, read_pages
from app.imports.errors import ImportProblem
from app.imports.questions import draft_questions
from app.imports.service import IMPORT_JOB_TYPE, Upload, create_import, expire_uploads, handle_import_job, read_import
from app.queue import claim_job
from test_worker import COURSE


EDITOR = UUID("30000000-0000-4000-8000-000000000001")
STUDENT_MEMBER = UUID("30000000-0000-4000-8000-000000000002")


def pdf_text(pages: int = 1) -> bytes:
    kids = " ".join(f"{i + 5} 0 R" for i in range(pages))
    text = b"BT /F1 14 Tf 48 780 Td (What is a primary key?) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(text)} >>\nstream\n".encode() + text + b"\nendstream",
        *[b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
          b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"] * pages,
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(data)


def docx_text() -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", """<w:document
            xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:body><w:p><w:r><w:t>Что такое ключ?</w:t></w:r></w:p></w:body>
            </w:document>""")
    return stream.getvalue()


def scan() -> Image.Image:
    image = Image.new("RGB", (1000, 300), "white")
    ImageDraw.Draw(image).text((60, 75), "What is a primary key?", font=ImageFont.load_default(size=56), fill="black")
    return image


class ImportFormatTests(unittest.TestCase):
    def extract(self, filename, data):
        return read_pages(filename, data, deadline=time.monotonic() + 25)

    def test_text_pdf_docx_and_limits(self):
        self.assertEqual(self.extract("notes.txt", "\ufeffПервичный   ключ\r\nВнешний ключ".encode()),
                         [{"locator": {"page": 1}, "text": "Первичный ключ\nВнешний ключ"}])
        self.assertEqual(self.extract("questions.pdf", pdf_text()),
                         [{"locator": {"page": 1}, "text": "What is a primary key?"}])
        self.assertEqual(self.extract("questions.docx", docx_text()),
                         [{"locator": {"paragraph": 1}, "text": "Что такое ключ?"}])
        for filename, input_data, code in [
            ("too_large.txt", b"x" * (MAX_FILE_BYTES + 1), "FILE_TOO_LARGE"),
            ("wrong.pdf", b"plain text", "UNSUPPORTED_FILE"),
            ("big.pdf", pdf_text(31), "TOO_MANY_PAGES"),
            ("big.txt", b"x" * 200_001, "TEXT_LIMIT"),
        ]:
            with self.subTest(code=code), self.assertRaises(ImportProblem) as error:
                self.extract(filename, input_data)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(error.exception.filename, filename)

    @unittest.skipUnless(shutil.which(os.getenv("TESSERACT_BIN", "tesseract")), "Requires Tesseract OCR")
    def test_ocr_image_and_scanned_pdf(self):
        image = scan()
        for filename, kind in [("scan.jpg", "JPEG"), ("scan.png", "PNG"), ("scan.pdf", "PDF")]:
            stream = BytesIO()
            image.save(stream, kind)
            with self.subTest(kind=kind):
                pages = self.extract(filename, stream.getvalue())
                self.assertIn("primary key", pages[0]["text"].lower())
        empty = Image.new("RGB", (800, 300), "white")
        stream = BytesIO()
        image.save(stream, "PDF", save_all=True, append_images=[empty])
        with self.assertRaises(ImportProblem) as error:
            self.extract("material.pdf", stream.getvalue())
        self.assertEqual((error.exception.code, error.exception.filename, error.exception.page),
                         ("UNREADABLE_FILE", "material.pdf", 2))

    def test_rule_based_numbering_and_grounded_model_suggestion(self):
        numbered = "Заголовок\n1. Что такое ключ?\nУточните ограничение.\n2) Что такое транзакция?"
        questions, warnings, used = draft_questions(numbered, "q.txt")
        self.assertEqual(questions[0]["text"], "Что такое ключ? Уточните ограничение.")
        self.assertTrue(warnings)
        self.assertFalse(used)
        mixed = "Заголовок\nЧто такое ключ?\nОпишите атомарность"
        questions, _, used = draft_questions(mixed, "q.txt", lambda _: '{"questions":["Что такое ключ?"]}')
        self.assertTrue(used)
        self.assertEqual(len(questions), 1)
        _, _, used = draft_questions(mixed, "q.txt", lambda _: '{"questions":["Выдуманный вопрос"]}')
        self.assertFalse(used)
        with self.assertRaises(ImportProblem) as error:
            draft_questions("я" * 501, "long.txt")
        self.assertEqual(error.exception.code, "QUESTION_TOO_LONG")


@unittest.skipUnless(os.getenv("BILET_TEST_DATABASE_URL"), "Requires a seeded test PostgreSQL")
class ImportJobTests(unittest.TestCase):
    def test_import_replay_review_role_checks_and_expiry(self):
        with psycopg.connect(os.environ["BILET_TEST_DATABASE_URL"], autocommit=True) as db, TemporaryDirectory() as temp:
            storage = Path(temp) / "uploads"
            with db.transaction(force_rollback=True):
                upload = Upload("notes.pdf", pdf_text())
                params = {"storage": storage, "question_text": "Что такое ключ?", "materials": (upload,)}
                ack = create_import(db, COURSE, EDITOR, "request-1", **params)
                self.assertEqual(ack, create_import(db, COURSE, EDITOR, "request-1", **params))
                with self.assertRaises(ImportProblem) as conflict:
                    create_import(db, COURSE, EDITOR, "request-1", storage=storage, question_text="Другой вопрос?")
                self.assertEqual(conflict.exception.code, "STATE_CONFLICT")
                with self.assertRaises(ImportProblem) as denied:
                    create_import(db, COURSE, STUDENT_MEMBER, "request-1", **params)
                self.assertEqual(denied.exception.code, "NOT_FOUND")
                import_id = UUID(ack["importId"])
                self.assertTrue((storage / str(import_id) / "0.upload").is_file())
                self.assertIsNone(read_import(db, import_id, STUDENT_MEMBER))
                job = claim_job(db, (IMPORT_JOB_TYPE,), 60)
                self.assertEqual(handle_import_job(db, job, storage), "done")
                self.assertIsNone(claim_job(db, (IMPORT_JOB_TYPE,), 60))
                self.assertEqual(read_import(db, import_id, EDITOR)["questions"],
                                 [{"ordinal": 1, "text": "Что такое ключ?"}])
                self.assertEqual(read_import(db, import_id, EDITOR)["status"], "review")
                self.assertFalse((storage / str(import_id)).exists())
                rows = db.execute("SELECT kind,pages FROM sources WHERE import_id = %s ORDER BY ordinal",
                                  (import_id,)).fetchall()
                self.assertEqual(rows[1], ("material", [{"locator": {"page": 1}, "text": "What is a primary key?"}]))
                self.assertEqual(ack, create_import(db, COURSE, EDITOR, "request-1", **params))

                bad = create_import(db, COURSE, EDITOR, "request-2", storage=storage,
                                    question_file=Upload("bad.pdf", b"%PDF-invalid"))
                self.assertEqual(handle_import_job(db, claim_job(db, (IMPORT_JOB_TYPE,), 60), storage), "failed")
                error = read_import(db, UUID(bad["importId"]), EDITOR)["error"]
                self.assertEqual((error["code"], error["filename"]), ("UNREADABLE_FILE", "bad.pdf"))
                self.assertFalse((storage / bad["importId"]).exists())

                total = create_import(db, COURSE, EDITOR, "request-3", storage=storage, question_text="Вопрос?",
                                      materials=(Upload("first.txt", b"x" * 110_000), Upload("second.txt", b"y" * 110_000)))
                self.assertEqual(handle_import_job(db, claim_job(db, (IMPORT_JOB_TYPE,), 60), storage), "failed")
                error = read_import(db, UUID(total["importId"]), EDITOR)["error"]
                self.assertEqual((error["code"], error["filename"]), ("TEXT_LIMIT", "second.txt"))

                analyzed = create_import(
                    db, COURSE, EDITOR, "request-4", storage=storage,
                    question_text="Заголовок\nЧто такое ключ?\nОпишите атомарность",
                )
                job = claim_job(db, (IMPORT_JOB_TYPE,), 60)
                self.assertEqual(handle_import_job(
                    db, job, storage, suggest=lambda _: '{"questions":["Что такое ключ?"]}', model="mock",
                ), "done")
                model, prompt = db.execute("SELECT model,prompt_version FROM imports WHERE id = %s",
                                           (UUID(analyzed["importId"]),)).fetchone()
                self.assertEqual((model, prompt), ("mock", "import-v1"))

                expired = UUID(create_import(db, COURSE, EDITOR, "request-5", **params)["importId"])
                db.execute("UPDATE imports SET uploads_expire_at = clock_timestamp() WHERE id = %s", (expired,))
                expire_uploads(db, storage)
                self.assertEqual(read_import(db, expired, EDITOR)["error"]["code"], "UPLOAD_EXPIRED")
                self.assertFalse((storage / str(expired)).exists())
