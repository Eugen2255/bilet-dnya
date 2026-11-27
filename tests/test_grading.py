import json
import os
import unittest
from unittest.mock import MagicMock, patch

from app.ai.client import ChatClient, ModelError
from app.ai.grading import Grader, RubricPoint


POINTS = [RubricPoint(id=f"p{i}", text=f"Критерий {i}") for i in range(1, 5)]


class FakeModel:
    def __init__(self, response, model="test"):
        self.response = response
        self.model = model

    def complete(self, messages):
        if isinstance(self.response, ModelError):
            raise self.response
        return self.response


class GradingTests(unittest.TestCase):
    def grade(self, model, fallback=None):
        return Grader(model, fallback).grade("Вопрос", "Эталон", POINTS, "Ответ")

    def test_grade_uses_coverage_and_deduplicated_errors(self):
        model = FakeModel('{"coveredIds":["p1","p2","p3"],"missingIds":["p4"],'
                          '"errors":["Фактическая ошибка", "фактическая  ошибка"],"feedback":"Разбор"}')
        result = self.grade(model)
        self.assertEqual(result.result["score"], 3)
        self.assertEqual(result.result["errors"], ["Фактическая ошибка"])
        self.assertEqual((result.model, result.prompt_version), ("test", "grade-v1"))

    def test_invalid_coverage_is_rejected(self):
        for covered, missing in [(["p1", "p1"], ["p3", "p4"]), (["p0"], ["p2", "p3", "p4"])]:
            with self.subTest(covered=covered), self.assertRaisesRegex(ModelError, "INVALID_GRADE_POINT_IDS"):
                self.grade(FakeModel(json.dumps({
                    "coveredIds": covered, "missingIds": missing, "errors": [], "feedback": "Разбор",
                })))

    def test_fallback_handles_invalid_json(self):
        model = FakeModel("обычный текст")
        fallback = FakeModel('{"coveredIds":["p1","p2"],"missingIds":["p3","p4"],'
                             '"errors":[],"feedback":"Разбор"}', model="backup")
        result = self.grade(model, fallback)
        self.assertEqual((result.model, result.result["score"]), ("backup", 3))

    def test_chat_client_sends_only_completion_payload(self):
        endpoint = "https://test.invalid/api/chat/completions"
        client = ChatClient(endpoint, "model-id", "test-key")
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        }).encode()
        with patch("app.ai.client.build_opener", return_value=opener):
            self.assertEqual(client.complete([{"role": "user", "content": "Ответ"}]), "{}")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.full_url, endpoint)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)
        self.assertEqual(json.loads(request.data), {
            "model": "model-id", "messages": [{"role": "user", "content": "Ответ"}],
            "temperature": 0, "max_tokens": 500, "stream": False,
        })

    def test_qwen_thinking_option_is_sent_only_when_configured(self):
        env = {"MODEL_PRIMARY_URL": "https://test.invalid/api/chat/completions", "MODEL_PRIMARY_ID": "old-model",
               "MODEL_PRIMARY_THINKING": "false"}
        with patch.dict(os.environ, env):
            client = ChatClient.from_env(model_id="calibrated-model")
        response = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        with patch.object(ChatClient, "_request_json", return_value=response) as request:
            client.complete([{"role": "user", "content": "Тест"}])
        body = request.call_args.args[1]
        self.assertEqual(body["model"], "calibrated-model")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
