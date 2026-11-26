from collections.abc import Iterator
from typing import Callable
from uuid import UUID

from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ai.client import ModelError
from app.imports.service import finish_job, lease_is_current
from app.queue import Job, enqueue_delivery
from app.rubrics.drafts import RUBRIC_PROMPT_VERSION, generate_draft
from app.rubrics.service import RUBRIC_JOB_TYPE


CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150
GENERATION_DELAYS = (15, 60)


def split_material(pages: list[dict]) -> Iterator[tuple[int, dict, str]]:
    ordinal = 0
    for page in pages:
        body = page["text"]
        for start in range(0, len(body), CHUNK_SIZE - CHUNK_OVERLAP):
            end = start + CHUNK_SIZE
            if body[start:end].strip():
                ordinal += 1
                yield ordinal, page["locator"], body[start:end]
            if end >= len(body):
                break


def find_chunks(cursor: Cursor, course_id: UUID, import_id: UUID, question: str) -> list[dict]:
    return cursor.execute("""
        WITH queries AS (
            SELECT exact, replace(exact::text, ' & ', ' | ')::tsquery AS broad
            FROM (SELECT plainto_tsquery('russian', %s) AS exact) AS terms
        )
        SELECT ch.id, ch.body FROM chunks AS ch JOIN sources AS s ON s.id = ch.source_id
        CROSS JOIN queries AS q
        WHERE ch.course_id = %s AND s.import_id = %s AND s.kind = 'material'
            AND numnode(q.broad) > 0 AND ch.search_terms @@ q.broad
        ORDER BY (ch.search_terms @@ q.exact) DESC, ts_rank_cd(ch.search_terms, q.broad, 32) DESC,
            s.ordinal, ch.ordinal
        LIMIT 5
    """, (question, course_id, import_id)).fetchall()


def handle_index_job(connection: Connection, job: Job) -> str:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        if not lease_is_current(cursor, job):
            return "lease_lost"
        try:
            import_id = UUID(job.payload["importId"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return finish_job(cursor, job, "failed", {"error": "INVALID_INDEX_JOB"})
        target = cursor.execute("""
            SELECT course_id FROM imports WHERE id = %s AND state = 'generating' FOR NO KEY UPDATE
        """, (import_id,)).fetchone()
        if target is None:
            return finish_job(cursor, job, "done", {"skipped": "import_unavailable"})
        sources = cursor.execute("""
            SELECT id, pages FROM sources WHERE import_id = %s AND course_id = %s AND kind = 'material'
            ORDER BY ordinal
        """, (import_id, target["course_id"])).fetchall()
        for source in sources:
            cursor.executemany("""
                INSERT INTO chunks (source_id, course_id, ordinal, locator, body)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT (source_id, ordinal) DO NOTHING
            """, (
                (source["id"], target["course_id"], ordinal, Jsonb(locator), body)
                for ordinal, locator, body in split_material(source["pages"])
            ))
        tickets = cursor.execute("""
            SELECT id FROM tickets WHERE import_id = %s AND course_id = %s ORDER BY ordinal
        """, (import_id, target["course_id"])).fetchall()
        if not tickets:
            cursor.execute("""
                UPDATE imports SET state = 'failed', error = %s WHERE id = %s
            """, (Jsonb({"code": "NO_TICKETS", "message": "Подтверждённый набор билетов недоступен."}), import_id))
            return finish_job(cursor, job, "failed", {"error": "NO_TICKETS"})
        for ticket in tickets:
            enqueue_delivery(
                cursor, f"{RUBRIC_JOB_TYPE}:{ticket['id']}", RUBRIC_JOB_TYPE,
                {"importId": str(import_id), "ticketId": str(ticket["id"])},
            )
        return finish_job(cursor, job, "done", {"tickets": len(tickets)})


def handle_rubric_job(
    connection: Connection, job: Job, *,
    suggest: Callable[[list[dict[str, str]]], str] | None = None,
    model: str | None = None,
) -> str:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        if not lease_is_current(cursor, job):
            return "lease_lost"
        try:
            import_id, ticket_id = UUID(job.payload["importId"]), UUID(job.payload["ticketId"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return finish_job(cursor, job, "failed", {"error": "INVALID_RUBRIC_JOB"})
        ticket = cursor.execute("""
            SELECT t.question, t.course_id, EXISTS (SELECT 1 FROM rubrics WHERE ticket_id = t.id) AS has_rubric
            FROM tickets AS t JOIN imports AS i ON i.id = t.import_id AND i.course_id = t.course_id
            WHERE t.id = %s AND i.id = %s AND i.state = 'generating'
        """, (ticket_id, import_id)).fetchone()
        if ticket is None:
            return finish_job(cursor, job, "done", {"skipped": "ticket_unavailable"})
        chunks = (find_chunks(cursor, ticket["course_id"], import_id, ticket["question"])
                  if suggest is not None and not ticket["has_rubric"] else [])

    draft, problem = None, None
    if suggest is not None and not ticket["has_rubric"]:
        try:
            draft = generate_draft(ticket["question"], chunks, suggest)
        except ModelError as error:
            problem = error

    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        if not lease_is_current(cursor, job):
            return "lease_lost"
        active = cursor.execute("""
            SELECT 1 FROM imports WHERE id = %s AND state = 'generating' FOR NO KEY UPDATE
        """, (import_id,)).fetchone()
        if not active:
            return finish_job(cursor, job, "done", {"skipped": "import_unavailable"})
        current = cursor.execute("""
            SELECT ordinal FROM tickets WHERE id = %s AND import_id = %s FOR UPDATE
        """, (ticket_id, import_id)).fetchone()
        if not current:
            return finish_job(cursor, job, "done", {"skipped": "ticket_unavailable"})
        if not cursor.execute("SELECT 1 FROM rubrics WHERE ticket_id = %s LIMIT 1", (ticket_id,)).fetchone():
            if problem is not None and problem.retryable:
                tries = cursor.execute("SELECT tries FROM jobs WHERE id = %s", (job.id,)).fetchone()["tries"]
                if tries <= len(GENERATION_DELAYS):
                    delay = max(GENERATION_DELAYS[max(tries - 1, 0)], problem.retry_after or 0)
                    cursor.execute("""
                        UPDATE jobs SET state = 'queued', lease_until = NULL,
                            run_at = clock_timestamp() + make_interval(secs => %s), result = %s WHERE id = %s
                    """, (delay, Jsonb({"error": problem.code}), job.id))
                    return "queued"
            store_draft(cursor, import_id, ticket_id, current["ordinal"], draft, model)

        cursor.execute("""
            UPDATE imports SET state = 'ready' WHERE id = %s AND NOT EXISTS (
                SELECT 1 FROM tickets AS t WHERE t.import_id = %s
                    AND NOT EXISTS (SELECT 1 FROM rubrics AS r WHERE r.ticket_id = t.id)
            )
        """, (import_id, import_id))
        return finish_job(cursor, job, "done", {"ticketId": str(ticket_id)})


def store_draft(
    cursor: Cursor, import_id: UUID, ticket_id: UUID, ordinal: int, draft: dict | None, model: str | None,
) -> None:
    if draft is None:
        answer, points, status = "", [], "unsourced"
        warning = f"Билет №{ordinal}. Заполните эталон вручную."
    else:
        answer, points, status = draft["answer"], draft["points"], draft["sourceStatus"]
        warning = f"Билет №{ordinal}. Для части пунктов нет подтверждённого источника." if status == "unsourced" else None
    cursor.execute("""
        INSERT INTO rubrics (ticket_id, version, state, answer, points, source_status, model, prompt_version)
        VALUES (%s, 1, 'draft', %s, %s, %s, %s, %s)
    """, (ticket_id, answer, Jsonb(points), status, model if draft else None, RUBRIC_PROMPT_VERSION if draft else None))
    if warning:
        cursor.execute("UPDATE imports SET warnings = warnings || %s WHERE id = %s", (Jsonb([warning]), import_id))
