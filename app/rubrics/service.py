import json
from hashlib import sha256
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.imports.documents import clean_text
from app.imports.errors import ImportProblem
from app.imports.questions import MAX_QUESTIONS, MAX_QUESTION_CHARS
from app.queue import enqueue_delivery
from app.rubrics.drafts import apply_citations, normalize_rubric


INDEX_JOB_TYPE = "index_import"
RUBRIC_JOB_TYPE = "generate_rubric"


def confirmed_questions(value: object) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_QUESTIONS:
        raise ImportProblem("INVALID_INPUT", "Подтвердите от одного до 60 вопросов.")
    by_ordinal = {}
    for item in value:
        if not isinstance(item, dict) or type(item.get("ordinal")) is not int or not isinstance(item.get("text"), str):
            raise ImportProblem("INVALID_INPUT", "Укажите номер и текст каждого вопроса.")
        ordinal, text = item["ordinal"], " ".join(clean_text(item["text"]).split())
        if ordinal in by_ordinal or not 1 <= ordinal <= len(value) or not 1 <= len(text) <= MAX_QUESTION_CHARS:
            raise ImportProblem("INVALID_INPUT", "Проверьте подряд идущие номера и формулировки до 500 знаков.")
        by_ordinal[ordinal] = text
    return [{"ordinal": ordinal, "text": text} for ordinal, text in sorted(by_ordinal.items())]


def confirm_import(connection: Connection, import_id: UUID, member_id: UUID, idempotency_key: str, questions: list) -> dict:
    if (not idempotency_key or len(idempotency_key) > 128
            or not idempotency_key.isascii() or not idempotency_key.isprintable()):
        raise ImportProblem("INVALID_INPUT", "Нужен Idempotency-Key длиной до 128 символов.")
    edited = confirmed_questions(questions)
    fingerprint = sha256(json.dumps(questions, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute("""
            SELECT i.course_id, i.state, i.confirmed_by, i.confirm_key, i.confirm_hash,
                c.state AS course_state, c.schedule_from
            FROM imports AS i JOIN courses AS c ON c.id = i.course_id
            JOIN memberships AS m ON m.group_id = i.group_id
            WHERE i.id = %s AND m.id = %s AND m.state = 'active' AND m.role IN ('organizer', 'teacher')
            FOR NO KEY UPDATE OF c, i
        """, (import_id, member_id)).fetchone()
        if row is None:
            raise ImportProblem("NOT_FOUND", "Импорт недоступен.")
        if row["confirm_key"] is not None:
            if (row["confirmed_by"] == member_id and row["confirm_key"] == idempotency_key
                    and row["confirm_hash"] == fingerprint):
                return {"status": "generating"}
            raise ImportProblem("STATE_CONFLICT", "Этот список уже подтверждён.")
        if row["state"] != "review" or row["course_state"] != "draft" or row["schedule_from"] is not None:
            raise ImportProblem("STATE_CONFLICT", "Подтвердите готовый импорт до запуска курса.")
        course_id = row["course_id"]
        if cursor.execute("""
            SELECT 1 FROM imports WHERE course_id = %s AND state = 'generating'
        """, (course_id,)).fetchone():
            raise ImportProblem("STATE_CONFLICT", "Для курса уже готовятся эталоны.")
        if cursor.execute("SELECT 1 FROM assignments WHERE course_id = %s LIMIT 1", (course_id,)).fetchone():
            raise ImportProblem("STATE_CONFLICT", "В курсе уже есть выданные задания.")

        cursor.execute("DELETE FROM tickets WHERE course_id = %s", (course_id,))
        cursor.executemany("""
            INSERT INTO tickets (course_id, import_id, ordinal, question)
            VALUES (%s, %s, %s, %s)
        """, [(course_id, import_id, item["ordinal"], item["text"]) for item in edited])
        cursor.execute("""
            UPDATE imports SET state = 'generating', confirmed_by = %s, confirm_key = %s, confirm_hash = %s,
                draft_questions = %s WHERE id = %s
        """, (member_id, idempotency_key, fingerprint, Jsonb(edited), import_id))
        enqueue_delivery(cursor, f"{INDEX_JOB_TYPE}:{import_id}", INDEX_JOB_TYPE, {"importId": str(import_id)})
    return {"status": "generating"}


def list_tickets(connection: Connection, course_id: UUID, member_id: UUID) -> dict | None:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        visible = cursor.execute("""
            SELECT 1 FROM courses AS c JOIN memberships AS m ON m.group_id = c.group_id
            WHERE c.id = %s AND m.id = %s AND m.state = 'active' AND m.role IN ('organizer', 'teacher')
        """, (course_id, member_id)).fetchone()
        if not visible:
            return None
        rows = cursor.execute("""
            SELECT t.id, t.question, latest.version, latest.state, latest.answer, latest.points, latest.source_status,
                approved.version AS approved_version
            FROM tickets AS t
            LEFT JOIN LATERAL (
                SELECT version, state, answer, points, source_status FROM rubrics
                WHERE ticket_id = t.id ORDER BY version DESC LIMIT 1
            ) AS latest ON true
            LEFT JOIN rubrics AS approved ON approved.id = t.approved_rubric_id
            WHERE t.course_id = %s ORDER BY t.ordinal
        """, (course_id,)).fetchall()
    return {"items": [{
        "id": str(row["id"]), "question": row["question"], "version": row["version"] or 0,
        "status": row["state"] or "draft", "approvedVersion": row["approved_version"],
        "rubric": {"answer": row["answer"], "sourceStatus": row["source_status"], "points": row["points"]}
        if row["version"] else None,
    } for row in rows]}


def read_source_chunk(connection: Connection, chunk_id: UUID, member_id: UUID) -> dict | None:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute("""
            SELECT ch.id, ch.locator, ch.body, s.filename
            FROM chunks AS ch JOIN sources AS s ON s.id = ch.source_id
            JOIN courses AS c ON c.id = ch.course_id JOIN memberships AS m ON m.group_id = c.group_id
            WHERE ch.id = %s AND m.id = %s AND m.state = 'active' AND m.role IN ('organizer', 'teacher')
        """, (chunk_id, member_id)).fetchone()
    return ({**row, "id": str(row["id"])} if row is not None else None)


def update_rubric(
    connection: Connection, ticket_id: UUID, member_id: UUID, *,
    base_version: int, answer: str, points: list, approve: bool,
    acknowledge_unsourced: bool = False,
) -> dict:
    if type(base_version) is not int or base_version < 0 or type(approve) is not bool:
        raise ImportProblem("INVALID_INPUT", "Укажите исходную версию и режим сохранения.")
    answer, points = normalize_rubric(answer, points, complete=approve)
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        ticket = cursor.execute("""
            SELECT t.course_id, c.state, m.user_id FROM tickets AS t JOIN courses AS c ON c.id = t.course_id
            JOIN memberships AS m ON m.group_id = c.group_id
            WHERE t.id = %s AND m.id = %s AND m.state = 'active' AND m.role IN ('organizer', 'teacher')
            FOR UPDATE OF t
        """, (ticket_id, member_id)).fetchone()
        if ticket is None:
            raise ImportProblem("NOT_FOUND", "Билет недоступен.")
        if ticket["state"] == "closed":
            raise ImportProblem("STATE_CONFLICT", "Курс закрыт.")
        latest = cursor.execute("SELECT COALESCE(max(version), 0) AS version FROM rubrics WHERE ticket_id = %s",
                                (ticket_id,)).fetchone()["version"]
        if base_version != latest:
            raise ImportProblem("STATE_CONFLICT", "Версия устарела. Обновите билет перед сохранением.")

        ids = []
        for point in points:
            citation = point["citation"]
            if isinstance(citation, dict):
                try:
                    ids.append(UUID(citation["chunkId"]))
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
        fragments = cursor.execute("""
            SELECT ch.id, ch.body FROM chunks AS ch JOIN sources AS s ON s.id = ch.source_id
            WHERE ch.course_id = %s AND s.kind = 'material' AND ch.id = ANY (%s)
        """, (ticket["course_id"], ids)).fetchall() if ids else []
        checked, source_status = apply_citations(points, {str(row["id"]): row["body"] for row in fragments}, strict=True)
        if approve and source_status == "unsourced" and acknowledge_unsourced is not True:
            raise ImportProblem("STATE_CONFLICT", "Подтвердите утверждение эталона без полного набора источников.")

        status, version = ("approved" if approve else "draft"), latest + 1
        rubric = cursor.execute("""
            INSERT INTO rubrics (ticket_id, version, state, answer, source_status, points, author_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (ticket_id, version, status, answer, source_status, Jsonb(checked), ticket["user_id"])).fetchone()["id"]
        if approve:
            cursor.execute("UPDATE tickets SET approved_rubric_id = %s, state = 'approved' WHERE id = %s",
                           (rubric, ticket_id))
    return {"version": version, "status": status, "sourceStatus": source_status}
