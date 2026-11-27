import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.ai.calibrate import Dataset, evaluate
from app.ai.client import ModelError
from app.ai.grading import Grader


DATASET = Path(__file__).resolve().parents[1] / "evals" / "answer-calibration-v1.json"


class CalibrationTests(unittest.TestCase):
    def test_invalid_gold_annotations_are_rejected(self):
        data = json.loads(DATASET.read_text(encoding="utf-8"))
        data["cases"][0]["goldScore"] = 4
        with self.assertRaises(ValidationError):
            Dataset.model_validate(data)

    def test_provider_failure_remains_in_threshold_denominator(self):
        dataset = Dataset.model_validate_json(DATASET.read_bytes())

        class TestModel:
            model = "calibration-test"

            def complete(self, messages):
                request = json.loads(messages[-1]["content"])
                if request["answer"] == dataset.cases[0].answer:
                    raise ModelError("MODEL_UNAVAILABLE")
                return json.dumps({"coveredIds": [], "missingIds": [point["id"] for point in request["points"]],
                                   "errors": [], "feedback": "Проверено"})

        report = evaluate(dataset, Grader(TestModel()))
        totals = report["overall"]
        self.assertEqual((totals["total"], totals["rated"], totals["failed"]), (36, 35, 1))
        self.assertEqual(totals["withinOneRate"], round(totals["withinOne"] / 36, 4))
        self.assertFalse(report["targetMet"])
        self.assertEqual(report["cases"][0]["error"], "MODEL_UNAVAILABLE")
