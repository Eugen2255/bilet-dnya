import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app.telegram.delivery import GRADE_TEXT_LIMIT, grade_message


class GradeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.attempt_id = uuid4()
        now = datetime.now(timezone.utc)
        self.row = {
            "status": "graded", "dm_chat_id": 123, "ordinal": 2, "role": "student",
            "can_retry": True, "graded_at": now, "read_at": now, "appeal_state": None,
            "result": {
                "score": 2, "coveredIds": ["p1", "p2"], "missingIds": ["p3", "p4", "p5"],
                "errors": ["Фактическая ошибка"], "feedback": "Комментарий модели.",
            },
            "rubric_state": "approved", "answer": "Текст эталона.",
            "points": [{"id": f"p{i}", "text": f"Критерий {i} " + "📘" * 450} for i in range(1, 6)],
        }

    def grade(self, mini_app_url):
        cursor = Mock()
        cursor.execute.return_value.fetchone.return_value = self.row
        with patch.dict("os.environ", {"MINI_APP_URL": mini_app_url}):
            return grade_message(cursor, self.attempt_id, "graded").payload

    def test_long_grade_keeps_each_section_and_links_to_exact_attempt(self):
        result = self.grade("https://study.example.org/app/?theme=light")
        self.assertLessEqual(len(result["text"].encode("utf-16-le")) // 2, GRADE_TEXT_LIMIT)
        for text in ("Оценка 2/5", "Засчитано", "Упущено", "Фактическая ошибка",
                     "Комментарий модели.", "Эталон\nТекст эталона.", "Полный разбор доступен"):
            self.assertIn(text, result["text"])
        detail, appeal, report = [row[0] for row in result["reply_markup"]["inline_keyboard"]]
        url = urlsplit(detail["web_app"]["url"])
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "study.example.org", "/app/"))
        self.assertEqual(parse_qs(url.query), {"theme": ["light"], "attempt": [str(self.attempt_id)]})
        self.assertEqual(appeal["callback_data"], f"appeal:{self.attempt_id}")
        self.assertEqual(report["callback_data"], f"report:{self.attempt_id}")

    def test_long_grade_with_no_mini_app_retains_reference_preview(self):
        self.row["answer"] *= 400
        result = self.grade("")
        self.assertLessEqual(len(result["text"].encode("utf-16-le")) // 2, GRADE_TEXT_LIMIT)
        self.assertIn("Комментарий модели.", result["text"])
        self.assertIn("Эталон\nТекст эталона.", result["text"])
        self.assertIn("Разбор сокращён", result["text"])
        self.assertFalse(any("web_app" in row[0] for row in result["reply_markup"]["inline_keyboard"]))

    def test_revoked_reference_remains_hidden(self):
        self.row["rubric_state"] = "revoked"
        result = self.grade("https://study.example.org/app/")
        self.assertIn("Эталон проверяет преподаватель", result["text"])
        self.assertNotIn(self.row["answer"], result["text"])
        self.assertNotIn("Комментарий модели.", result["text"])
        for row in result["reply_markup"]["inline_keyboard"]:
            self.assertNotIn("web_app", row[0])
            self.assertNotEqual(row[0].get("callback_data"), f"report:{self.attempt_id}")
