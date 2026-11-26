import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.imports.documents import MAX_CHARS, MAX_FILE_BYTES, MAX_IMPORT_SECONDS, detect_format, read_pages
from app.imports.errors import ImportProblem
from app.imports.questions import QUESTION_PROMPT_VERSION, draft_questions
from app.queue import Job, enqueue_delivery


logger = logging.getLogger(__name__)
IMPORT_JOB_TYPE = "parse_import"
IMPORT_LEASE_SECONDS = MAX_IMPORT_SECONDS + 60
MAX_MATERIALS = 5


@dataclass(frozen=True)
class Upload:
    filename: str
    data: bytes


def storage_path() -> Path:
    return Path(os.getenv("IMPORT_STORAGE", "data/uploads"))


def filename_for(upload: Upload) -> str:
    name = PurePosixPath(upload.filename.replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or len(name) > 255 or any(ord(ch) < 32 for ch in name):
        raise ImportProblem("INVALID_INPUT", "Укажите имя файла длиной до 255 символов.")
    return name


def create_import(
    connection: Connection,
    course_id: UUID,
    member_id: UUID,
    idempotency_key: str,
    *,
    storage: Path,
    question_text: str | None = None,
    question_file: Upload | None = None,
    materials: tuple[Upload, ...] = (),
) -> dict:
    if not idempotency_key or len(idempotency_key) > 128 or not idempotency_key.isascii() or not idempotency_key.isprintable():
        raise ImportProblem("INVALID_INPUT", "Нужен Idempotency-Key длиной до 128 символов.")
    if (question_text is None) == (question_file is None) or len(materials) > MAX_MATERIALS:
        raise ImportProblem("INVALID_INPUT", "Добавьте текст либо один файл с вопросами и до пяти файлов материалов.")

    mode = "text" if question_text is not None else "file"
    first_upload = Upload("questions.txt", question_text.encode()) if question_text is not None else question_file
    uploads = (first_upload, *materials)
    refs = []
    for index, upload in enumerate(uploads):
        name = filename_for(upload)
        detect_format(name, upload.data)
        refs.append({
            "ordinal": index, "kind": "questions" if index == 0 else "material",
            "filename": name, "sha256": sha256(upload.data).hexdigest(),
        })
    fingerprint = sha256(json.dumps({"mode": mode, "files": refs}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        course = cursor.execute("""
            SELECT c.group_id, c.state FROM courses AS c JOIN memberships AS m ON m.group_id = c.group_id
            WHERE c.id = %s AND m.id = %s AND m.state = 'active' AND m.role IN ('organizer', 'teacher')
        """, (course_id, member_id)).fetchone()
        if course is None:
            raise ImportProblem("NOT_FOUND", "Курс недоступен.")
        proposed_id = uuid4()
        row = cursor.execute("""
            INSERT INTO imports (id, course_id, group_id, member_id, idempotency_key, request_hash, upload_refs)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (course_id, member_id, idempotency_key) DO NOTHING
            RETURNING id
        """, (proposed_id, course_id, course["group_id"], member_id, idempotency_key,
              fingerprint, Jsonb(refs))).fetchone()
        if row is None:
            previous = cursor.execute("""
                SELECT id, request_hash FROM imports WHERE course_id = %s AND member_id = %s AND idempotency_key = %s
            """, (course_id, member_id, idempotency_key)).fetchone()
            if previous is None or previous["request_hash"] != fingerprint:
                raise ImportProblem("STATE_CONFLICT", "Этот ключ уже использован для другой загрузки.")
            return {"importId": str(previous["id"]), "status": "queued"}
        if course["state"] == "closed":
            raise ImportProblem("STATE_CONFLICT", "Курс закрыт.")

        folder = storage / str(proposed_id)
        try:
            storage.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder.mkdir(mode=0o700)
            for ordinal, upload in enumerate(uploads):
                with (folder / f"{ordinal}.upload").open("xb") as file:
                    file.write(upload.data)
        except OSError:
            discard_uploads(storage, proposed_id)
            raise ImportProblem("STORAGE_UNAVAILABLE", "Временное хранилище файлов недоступно.") from None
        enqueue_delivery(cursor, f"{IMPORT_JOB_TYPE}:{proposed_id}", IMPORT_JOB_TYPE, {"importId": str(proposed_id)})
    return {"importId": str(proposed_id), "status": "queued"}


def read_import(connection: Connection, import_id: UUID, member_id: UUID) -> dict | None:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute("""
            SELECT i.id, i.state, i.draft_questions, i.warnings, i.error
            FROM imports AS i JOIN memberships AS m ON m.group_id = i.group_id
            WHERE i.id = %s AND m.id = %s AND m.state = 'active' AND m.role IN ('organizer', 'teacher')
        """, (import_id, member_id)).fetchone()
    if row is None:
        return None
    return {"importId": str(row["id"]), "status": row["state"], "questions": row["draft_questions"] or [],
            "warnings": row["warnings"], "error": row["error"]}


def lease_is_current(cursor: Cursor, job: Job) -> bool:
    return cursor.execute("""
        SELECT id FROM jobs WHERE id = %s AND type = 'parse_import' AND state = 'running'
            AND lease_until = %s AND lease_until > clock_timestamp()
        FOR UPDATE
    """, (job.id, job.lease_until)).fetchone() is not None


def finish_job(cursor: Cursor, job: Job, state: str, result: dict) -> str:
    cursor.execute("""
        UPDATE jobs SET state = %s, lease_until = NULL, result = %s WHERE id = %s
    """, (state, Jsonb(result), job.id))
    return state


def handle_import_job(connection: Connection, job: Job, storage: Path, *, suggest=None, model: str | None = None) -> str:
    try:
        import_id = UUID(job.payload["importId"])
    except (TypeError, AttributeError, KeyError, ValueError):
        with connection.transaction(), connection.cursor() as cursor:
            return finish_job(cursor, job, "failed", {"error": "INVALID_IMPORT_JOB"}) if lease_is_current(cursor, job) else "lease_lost"

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        if not lease_is_current(cursor, job):
            return "lease_lost"
        row = cursor.execute("""
            UPDATE imports SET state = 'processing' WHERE id = %s AND state IN ('queued', 'processing')
                AND upload_refs IS NOT NULL AND uploads_expire_at > clock_timestamp()
            RETURNING course_id, upload_refs
        """, (import_id,)).fetchone()
        if row is None:
            return finish_job(cursor, job, "done", {"skipped": "import_unavailable"})

    records, questions, warnings, model_used, error = [], [], [], False, None
    try:
        records = extract_all(storage, import_id, row["upload_refs"])
        question_text = "\n".join(page["text"] for page in records[0]["pages"])
        questions, warnings, model_used = draft_questions(question_text, records[0]["filename"], suggest)
    except ImportProblem as problem:
        error = problem.detail()

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        if not lease_is_current(cursor, job):
            return "lease_lost"
        finished = cursor.execute("""
            UPDATE imports SET state = %s, draft_questions = %s, warnings = %s, error = %s,
                model = %s, prompt_version = %s, upload_refs = NULL
            WHERE id = %s AND state = 'processing' AND uploads_expire_at > clock_timestamp()
            RETURNING id
        """, ("failed" if error else "review", Jsonb(questions) if not error else None, Jsonb(warnings),
              Jsonb(error) if error else None, model if model_used else None,
              QUESTION_PROMPT_VERSION if model_used else None, import_id)).fetchone()
        if finished is None:
            return finish_job(cursor, job, "done", {"skipped": "import_unavailable"})
        if error is None:
            for source in records:
                cursor.execute("""
                    INSERT INTO sources (course_id, import_id, ordinal, kind, filename, sha256, pages)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (row["course_id"], import_id, source["ordinal"], source["kind"], source["filename"],
                      source["sha256"], Jsonb(source["pages"])))
        status = finish_job(cursor, job, "failed" if error else "done",
                            {"error": error["code"]} if error else {"importId": str(import_id)})
    discard_uploads(storage, import_id)
    return status


def extract_all(storage: Path, import_id: UUID, refs: list[dict]) -> list[dict]:
    deadline = time.monotonic() + MAX_IMPORT_SECONDS
    records, total = [], 0
    for ref in refs:
        path = storage / str(import_id) / f"{ref['ordinal']}.upload"
        try:
            with path.open("rb") as file:
                data = file.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise ImportProblem("UNREADABLE_FILE", "Временный файл недоступен. Загрузите его заново.",
                                filename=ref["filename"]) from None
        if sha256(data).hexdigest() != ref["sha256"]:
            raise ImportProblem("UNREADABLE_FILE", "Временный файл повреждён. Загрузите его заново.",
                                filename=ref["filename"])
        pages = read_pages(ref["filename"], data, deadline=deadline)
        total += sum(len(page["text"]) for page in pages)
        if total > MAX_CHARS:
            raise ImportProblem("TEXT_LIMIT", "Суммарный объём извлечённого текста превышает 200 000 знаков.",
                                filename=ref["filename"])
        records.append({**ref, "pages": pages})
    return records


def discard_uploads(storage: Path, import_id: UUID) -> None:
    try:
        shutil.rmtree(storage / str(import_id))
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove temporary upload directory for import %s", import_id)


def expire_uploads(connection: Connection, storage: Path) -> None:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        events = cursor.execute("""
            UPDATE imports SET state = 'failed', upload_refs = NULL, error = %s
            WHERE upload_refs IS NOT NULL AND uploads_expire_at <= clock_timestamp()
                AND state IN ('queued', 'processing')
            RETURNING id
        """, (Jsonb({"code": "UPLOAD_EXPIRED", "message": "Файл хранился 24 часа. Загрузите его заново."}),)).fetchall()
    for event in events:
        discard_uploads(storage, event["id"])
    discard_old_upload_directories(storage)


def discard_old_upload_directories(storage: Path) -> None:
    threshold = time.time() - 24 * 60 * 60
    try:
        folders = list(storage.iterdir())
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Could not list temporary upload directories")
        return
    for folder in folders:
        try:
            import_id = UUID(folder.name)
            if str(import_id) == folder.name and not folder.is_symlink() and folder.is_dir() and folder.stat().st_mtime <= threshold:
                discard_uploads(storage, import_id)
        except (OSError, ValueError):
            continue
