import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.imports.documents import MAX_FILE_BYTES
from app.imports.service import Upload, create_import, discard_uploads, read_import, storage_path
from app.miniapp.courses import (
    EDITOR_ROLES, ORGANIZER, activate, as_course, groups_for, idempotency_key, organizer_course, related_member,
    require_member, save_schedule, set_paused,
)
from app.miniapp.errors import ApiError
from app.miniapp.security import (
    SESSION_AGE, SESSION_COOKIE, Database, Reader, Writer, check_origin, create_session, verify_init_data,
)
from app.operations import delete_courses
from app.progress import read_dashboard
from app.rubrics.service import confirm_import, list_tickets, read_source_chunk, update_rubric
from app.telegram.api import BotAPI, TelegramError
from app.telegram.onboarding import issue_start_url


router = APIRouter(prefix="/v1")
Key = Annotated[str, Header(alias="Idempotency-Key")]
DayTime = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
MAX_MULTIPART = 6 * MAX_FILE_BYTES + 256 * 1024


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Payload):
    initData: str = Field(min_length=1, max_length=12000)


class CourseTitle(Payload):
    title: str = Field(min_length=1, max_length=120)


class DeleteCourse(Payload):
    confirmTitle: str = Field(min_length=1, max_length=120)


class Schedule(Payload):
    examDate: date | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    morningAt: DayTime | None = None
    eveningAt: DayTime | None = None
    weekdays: list[StrictInt] | None = Field(default=None, min_length=1, max_length=7)


class Confirm(Payload):
    questions: list[dict] = Field(min_length=1, max_length=60)


class Rubric(Payload):
    baseVersion: StrictInt = Field(ge=0)
    answer: str = Field(max_length=10_000)
    points: list[dict] = Field(max_length=5)
    approve: StrictBool
    acknowledgeUnsourced: StrictBool = False


class Activate(Payload):
    acknowledgeLoad: StrictBool = False


class Invite(Payload):
    memberId: UUID


@router.post("/auth/telegram")
def login(payload: Login, request: Request, response: Response, db: Database):
    check_origin(request)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise ApiError("SERVICE_UNAVAILABLE", "Вход через Telegram пока недоступен.", 503)
    user = verify_init_data(payload.initData, bot_token, datetime.now(timezone.utc))
    user_id, token, csrf = create_session(db, user)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_AGE, secure=True, httponly=True, samesite="lax", path="/v1")
    return {"userId": str(user_id), "csrfToken": csrf}


@router.get("/groups")
def groups(db: Database, user: Reader):
    return groups_for(db, user.user_id)


@router.get("/groups/{group_id}/members")
def group_members(group_id: UUID, db: Database, user: Reader):
    require_member(db, user.user_id, group_id, ORGANIZER)
    rows = db.execute("""
        SELECT id, alias FROM memberships
        WHERE group_id = %s AND state = 'active' AND role = 'student' ORDER BY alias, id
    """, (group_id,)).fetchall()
    return {"items": [{"id": str(row["id"]), "alias": row["alias"]} for row in rows]}


@router.post("/groups/{group_id}/courses", status_code=201)
def add_course(group_id: UUID, payload: CourseTitle, db: Database, user: Writer):
    title = payload.title.replace("\x00", "").strip()
    if not title:
        raise ApiError("INVALID_INPUT", "Укажите название предмета.")
    with db.transaction():
        require_member(db, user.user_id, group_id, ORGANIZER)
        row = db.execute("INSERT INTO courses (group_id, title) VALUES (%s,%s) RETURNING *", (group_id, title)).fetchone()
        return as_course(row)


@router.get("/courses/{course_id}")
def read_course(course_id: UUID, db: Database, user: Reader):
    member = related_member(db, user.user_id, "course", course_id)
    course = db.execute("SELECT * FROM courses WHERE id = %s", (course_id,)).fetchone()
    if not course or (course["state"] == "draft" and member["role"] not in EDITOR_ROLES):
        raise ApiError("NOT_FOUND", "Объект недоступен.", 404)
    return as_course(course)


@router.patch("/courses/{course_id}")
def patch_course(course_id: UUID, payload: Schedule, db: Database, user: Writer):
    return save_schedule(db, user.user_id, course_id, payload.model_dump(exclude_unset=True))


@router.delete("/courses/{course_id}")
def remove_course(course_id: UUID, payload: DeleteCourse, db: Database, user: Writer):
    with db.transaction(), db.cursor() as cursor:
        course = organizer_course(db, user.user_id, course_id)
        if payload.confirmTitle != course["title"]:
            raise ApiError("INVALID_INPUT", "Введите точное название курса.")
        imports = delete_courses(cursor, [course_id])
    for import_id in imports:
        discard_uploads(storage_path(), import_id)
    return {"status": "deleted"}


@router.post("/courses/{course_id}/activate")
def start_course(course_id: UUID, payload: Activate, db: Database, user: Writer, key: Key = ""):
    return activate(db, user.user_id, course_id, key, payload.acknowledgeLoad)


@router.post("/courses/{course_id}/pause")
def pause_course(course_id: UUID, db: Database, user: Writer):
    return set_paused(db, user.user_id, course_id, True)


@router.post("/courses/{course_id}/resume")
def resume_course(course_id: UUID, db: Database, user: Writer):
    return set_paused(db, user.user_id, course_id, False)


def get_bot_name() -> str:
    configured = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    try:
        name = configured or (BotAPI(token).username if token else "")
    except TelegramError:
        raise ApiError("SERVICE_UNAVAILABLE", "Telegram временно недоступен.", 503) from None
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", name):
        raise ApiError("SERVICE_UNAVAILABLE", "Имя Telegram-бота не настроено.", 503)
    return name


@router.post("/groups/{group_id}/teacher-invites", status_code=201)
def invite_teacher(group_id: UUID, payload: Invite, db: Database, user: Writer):
    require_member(db, user.user_id, group_id, ORGANIZER)
    name = get_bot_name()
    with db.transaction(), db.cursor() as cursor:
        target = cursor.execute("""
            SELECT id FROM memberships WHERE id = %s AND group_id = %s AND role = 'student'
                AND state = 'active' FOR NO KEY UPDATE
        """, (payload.memberId, group_id)).fetchone()
        if not target:
            raise ApiError("NOT_FOUND", "Активный студент не найден в этой группе.", 404)
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        url = issue_start_url(cursor, group_id, name, expires, "teacher", payload.memberId)
    return {"startUrl": url, "expiresAt": expires.isoformat()}


async def limited_stream(request: Request):
    length = 0
    async for block in request.stream():
        length += len(block)
        if length > MAX_MULTIPART:
            raise MultiPartException("BODY_TOO_LARGE")
        yield block


async def uploaded(value: UploadFile) -> Upload:
    if value.size is not None and value.size > MAX_FILE_BYTES:
        raise ApiError("FILE_TOO_LARGE", "Максимальный размер файла — 10 МБ.", 413, filename=value.filename)
    data = await value.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ApiError("FILE_TOO_LARGE", "Максимальный размер файла — 10 МБ.", 413, filename=value.filename)
    return Upload(value.filename or "", data)


@router.post("/courses/{course_id}/imports", status_code=202)
async def add_import(course_id: UUID, request: Request, db: Database, user: Writer, key: Key = ""):
    member = await run_in_threadpool(related_member, db, user.user_id, "course", course_id, EDITOR_ROLES)
    idempotency_key(key)
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise ApiError("INVALID_INPUT", "Загрузите вопросы в форме multipart/form-data.")
    try:
        form = await MultiPartParser(request.headers, limited_stream(request), max_files=6, max_fields=2,
                                     max_part_size=MAX_FILE_BYTES).parse()
    except MultiPartException as exc:
        if exc.message == "BODY_TOO_LARGE" or "maximum size" in exc.message:
            raise ApiError("FILE_TOO_LARGE", "Превышен допустимый объём загрузки.", 413) from None
        raise ApiError("INVALID_INPUT", "Не удалось прочитать форму. Проверьте число файлов и полей.") from None
    except MultipartParseError:
        raise ApiError("INVALID_INPUT", "Не удалось прочитать форму загрузки.") from None
    try:
        if set(form) - {"questionText", "questionFile", "files", "files[]"}:
            raise ApiError("INVALID_INPUT", "В форме появилось неизвестное поле.")
        questions = form.getlist("questionText") + form.getlist("questionFile")
        materials = form.getlist("files") + form.getlist("files[]")
        if len(questions) != 1 or len(materials) > 5 or not all(isinstance(file, UploadFile) for file in materials):
            raise ApiError("INVALID_INPUT", "Добавьте текст либо один файл с вопросами и до пяти материалов.")
        question = questions[0]
        if (not isinstance(question, (str, UploadFile))
                or (isinstance(question, str) and not form.getlist("questionText"))
                or (isinstance(question, UploadFile) and not form.getlist("questionFile"))):
            raise ApiError("INVALID_INPUT", "Проверьте поле с вопросами.")
        files = tuple([await uploaded(value) for value in materials])
        question_text = question if isinstance(question, str) else None
        question_file = await uploaded(question) if isinstance(question, UploadFile) else None
        return await run_in_threadpool(
            create_import, db, course_id, member["id"], key, storage=storage_path(),
            question_text=question_text, question_file=question_file, materials=files,
        )
    finally:
        await form.close()


@router.get("/imports/{import_id}")
def get_import(import_id: UUID, db: Database, user: Reader):
    member = related_member(db, user.user_id, "import", import_id, EDITOR_ROLES)
    result = read_import(db, import_id, member["id"])
    if result is None:
        raise ApiError("NOT_FOUND", "Объект недоступен.", 404)
    return result


@router.post("/imports/{import_id}/confirm", status_code=202)
def validate_questions(import_id: UUID, payload: Confirm, db: Database, user: Writer, key: Key = ""):
    member = related_member(db, user.user_id, "import", import_id, EDITOR_ROLES)
    return confirm_import(db, import_id, member["id"], key, payload.questions)


@router.get("/courses/{course_id}/tickets")
def tickets(course_id: UUID, db: Database, user: Reader):
    member = related_member(db, user.user_id, "course", course_id, EDITOR_ROLES)
    return list_tickets(db, course_id, member["id"])


@router.put("/tickets/{ticket_id}/rubric")
def save_rubric(ticket_id: UUID, payload: Rubric, db: Database, user: Writer):
    member = related_member(db, user.user_id, "ticket", ticket_id, EDITOR_ROLES)
    return update_rubric(db, ticket_id, member["id"], base_version=payload.baseVersion,
                         answer=payload.answer, points=payload.points, approve=payload.approve,
                         acknowledge_unsourced=payload.acknowledgeUnsourced)


@router.get("/chunks/{chunk_id}")
def source(chunk_id: UUID, db: Database, user: Reader):
    member = related_member(db, user.user_id, "chunk", chunk_id, EDITOR_ROLES)
    return read_source_chunk(db, chunk_id, member["id"])


@router.get("/courses/{course_id}/dashboard")
def dashboard(course_id: UUID, db: Database, user: Reader):
    member = related_member(db, user.user_id, "course", course_id)
    course = db.execute("SELECT state FROM courses WHERE id = %s", (course_id,)).fetchone()
    if not course or (course["state"] == "draft" and member["role"] not in EDITOR_ROLES):
        raise ApiError("NOT_FOUND", "Объект недоступен.", 404)
    with db.cursor() as cursor:
        return read_dashboard(cursor, course_id, member["id"])


@router.get("/attempts/{attempt_id}")
def attempt(attempt_id: UUID, db: Database, user: Reader):
    row = db.execute("""
        SELECT p.status, COALESCE(review.result, p.result) AS result, r.answer, r.points, r.state AS rubric_state
        FROM attempts AS p JOIN assignments AS a ON a.id = p.assignment_id
        JOIN memberships AS m ON m.id = a.member_id JOIN rubrics AS r ON r.id = a.rubric_id
        LEFT JOIN appeals AS review ON review.attempt_id = p.id
        WHERE p.id = %s AND m.user_id = %s AND m.state = 'active'
    """, (attempt_id, user.user_id)).fetchone()
    if row is None:
        raise ApiError("NOT_FOUND", "Объект недоступен.", 404)
    result = row["result"] or {}
    if row["rubric_state"] == "revoked":
        return {"status": row["status"], "score": result.get("score"), "covered": None, "missing": None,
                "errors": None, "feedback": "Эталон проверяет преподаватель.", "reference": None}
    points = {p["id"]: p["text"] for p in row["points"]}
    return {
        "status": row["status"], "score": result.get("score"),
        "covered": [points[key] for key in result.get("coveredIds", []) if key in points] if result else None,
        "missing": [points[key] for key in result.get("missingIds", []) if key in points] if result else None,
        "errors": result.get("errors") if result else None, "feedback": result.get("feedback"),
        "reference": row["answer"] if result and row["rubric_state"] == "approved" else None,
    }
