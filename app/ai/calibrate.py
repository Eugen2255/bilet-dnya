import argparse
import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from time import perf_counter, sleep
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.ai.client import ChatClient, ModelError
from app.ai.grading import PROMPT_VERSION, Grader, RubricPoint


TARGET = 0.85


class Fixture(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class RubricFixture(Fixture):
    question: str = Field(min_length=1, max_length=500)
    reference: str = Field(min_length=1)
    points: list[RubricPoint] = Field(min_length=3, max_length=5)


class Case(Fixture):
    id: str = Field(min_length=1)
    rubric: str
    category: Literal["paraphrase", "partial", "factual_error"]
    answer: str = Field(min_length=1, max_length=1000)
    goldScore: int = Field(ge=0, le=5)
    goldCovered: list[str]
    goldErrors: list[str]


class Dataset(Fixture):
    version: Literal[1]
    annotationSource: str = Field(min_length=1)
    rubrics: dict[str, RubricFixture]
    cases: list[Case] = Field(min_length=30)

    @model_validator(mode="after")
    def check_annotations(self) -> "Dataset":
        ids = set()
        for case in self.cases:
            if case.id in ids or case.rubric not in self.rubrics:
                raise ValueError(f"Unknown rubric or duplicate case ID: {case.id}")
            ids.add(case.id)
            points = self.rubrics[case.rubric].points
            allowed = {point.id for point in points}
            covered = set(case.goldCovered)
            errors = {" ".join(text.split()).casefold() for text in case.goldErrors}
            if (len(allowed) != len(points) or len(covered) != len(case.goldCovered) or covered - allowed
                    or len(errors) != len(case.goldErrors) or "" in errors):
                raise ValueError(f"Invalid rubric annotation: {case.id}")
            expected = max(0, (10 * len(covered) + len(points)) // (2 * len(points)) - len(errors))
            if case.goldScore != expected:
                raise ValueError(f"Gold score disagrees with the annotation: {case.id}")
        return self


def score_case(grader: Grader, rubric: RubricFixture, case: Case) -> dict:
    started = perf_counter()
    result = None
    code = None
    for attempt in range(2):
        try:
            result = grader.grade(rubric.question, rubric.reference, rubric.points, case.answer)
        except ModelError as error:
            if attempt == 0 and error.code == "MODEL_HTTP_429" and (error.retry_after or 0) <= 20:
                sleep(error.retry_after or 2)
                continue
            code = error.code
        break
    return {
        "id": case.id, "category": case.category, "expected": case.goldScore,
        "actual": result.result["score"] if result else None,
        "latencyMs": round((perf_counter() - started) * 1000), "error": code,
    }


def stats(rows: list[dict]) -> dict:
    rated = [row for row in rows if row["actual"] is not None]
    deviations = [abs(row["actual"] - row["expected"]) for row in rated]
    latencies = sorted(row["latencyMs"] for row in rated)
    exact = deviations.count(0)
    within_one = sum(delta <= 1 for delta in deviations)
    return {
        "total": len(rows), "rated": len(rated), "failed": len(rows) - len(rated),
        "exact": exact, "withinOne": within_one,
        "withinOneRate": round(within_one / len(rows), 4),
        "meanAbsoluteError": round(sum(deviations) / len(deviations), 3) if deviations else None,
        "latencyP95Ms": latencies[ceil(0.95 * len(latencies)) - 1] if latencies else None,
    }


def evaluate(dataset: Dataset, grader: Grader, pause: float = 0) -> dict:
    rows = []
    for case in dataset.cases:
        if rows and pause:
            sleep(pause)
        rows.append(score_case(grader, dataset.rubrics[case.rubric], case))
    overall = stats(rows)
    return {
        "promptVersion": PROMPT_VERSION, "model": grader.primary.model,
        "annotationSource": dataset.annotationSource, "targetWithinOneRate": TARGET,
        "targetMet": overall["withinOne"] / overall["total"] >= TARGET,
        "overall": overall,
        "byCategory": {category: stats([row for row in rows if row["category"] == category])
                       for category in sorted({row["category"] for row in rows})},
        "cases": rows,
    }


def calibration_client(profile: str, model_id: str | None) -> ChatClient:
    if profile == "review":
        primary = ChatClient.from_env()
        review = os.getenv("MODEL_REVIEW_ID", "").strip() or primary.model
        return replace(primary, model=model_id or review)
    prefix = f"MODEL_{profile.upper()}"
    return ChatClient.from_env(prefix, model_id=model_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare one model with an annotated synthetic rubric dataset")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--profile", choices=("primary", "review", "fallback"), default="primary")
    parser.add_argument("--model-id", help="Model ID override for this run")
    parser.add_argument("--thinking", choices=("on", "off"), help="Request a specific Qwen thinking mode for this run")
    parser.add_argument("--pause", type=float, default=0, help="Seconds between requests")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.pause <= 60:
        parser.error("--pause must be between 0 and 60 seconds")
    try:
        source = args.dataset.read_bytes()
        dataset = Dataset.model_validate_json(source)
    except (OSError, ValidationError) as error:
        parser.exit(1, f"Invalid calibration dataset ({type(error).__name__})\n")
    checksum = hashlib.sha256(source).hexdigest()
    if args.validate_only:
        print(json.dumps({"cases": len(dataset.cases), "rubrics": len(dataset.rubrics), "datasetSha256": checksum}))
        return
    if args.output and args.output.exists():
        parser.error("Use a new path for --output")
    load_dotenv()
    try:
        client = calibration_client(args.profile, args.model_id)
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    if args.thinking:
        client = replace(client, thinking=args.thinking == "on")
    report = {
        "measuredAt": datetime.now(timezone.utc).isoformat(), "datasetSha256": checksum, "profile": args.profile,
        "thinking": client.thinking,
        **evaluate(dataset, Grader(client), args.pause),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
