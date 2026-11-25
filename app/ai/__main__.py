import argparse
import json
import os

from dotenv import load_dotenv

from app.ai.client import ChatClient, ModelError
from app.ai.grading import Grader, RubricPoint


ANSWERS = {
    "correct": "Первичный ключ однозначно идентифицирует строку. Его значения уникальны и не могут быть NULL.",
    "partial": "Первичный ключ нужен, чтобы идентифицировать строку.",
    "wrong": "Первичный ключ идентифицирует строку, но в нём могут быть NULL и повторяющиеся значения.",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка доступа к модели на синтетическом билете CORE-01")
    parser.add_argument("case", nargs="?", default="correct", choices=ANSWERS)
    parser.add_argument("--list-models", action="store_true", help="вывести доступные ID моделей")
    args = parser.parse_args()
    load_dotenv()
    try:
        if args.list_models:
            client = ChatClient(
                os.getenv("MODEL_PRIMARY_URL", "").strip(), "", os.getenv("MODEL_PRIMARY_API_KEY", "").strip()
            )
            print(json.dumps(client.list_models(), ensure_ascii=True, indent=2))
            return
        grader = Grader.from_env()
        result = grader.grade(
            "Что такое первичный ключ?",
            "Первичный ключ однозначно идентифицирует строку. Значения уникальны, NULL недопустим.",
            [
                RubricPoint(id="p1", text="Идентифицирует строку"),
                RubricPoint(id="p2", text="Значения ключа уникальны"),
                RubricPoint(id="p3", text="Ключ не содержит NULL"),
            ],
            ANSWERS[args.case],
        )
    except (ModelError, ValueError) as exc:
        parser.exit(1, f"Проверка не выполнена. {exc}\n")
    print(json.dumps({
        "model": result.model, "promptVersion": result.prompt_version, **result.result
    }, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
