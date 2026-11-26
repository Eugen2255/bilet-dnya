import os
from secrets import compare_digest
from typing import Annotated

import psycopg
from fastapi import APIRouter, Header, HTTPException
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from app.queue import enqueue_delivery


router = APIRouter()


class Update(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    update_id: int = Field(ge=0, le=2**63 - 1)


def store_update(connection: Connection, update: Update) -> None:
    with connection.transaction(), connection.cursor() as cursor:
        row = cursor.execute("""
            INSERT INTO telegram_updates (update_id, payload) VALUES (%s, %s)
            ON CONFLICT (update_id) DO NOTHING RETURNING update_id
        """, (update.update_id, Jsonb(update.model_dump()))).fetchone()
        if row is not None:
            enqueue_delivery(cursor, f"telegram_update:{update.update_id}", "telegram_update",
                             {"updateId": update.update_id})


@router.post("/telegram/webhook")
def telegram_webhook(
    update: Update,
    token: Annotated[str, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = "",
) -> dict[str, bool]:
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    database_url = os.getenv("DATABASE_URL", "")
    if not secret:
        raise HTTPException(503, "Webhook unavailable")
    if not compare_digest(token.encode(), secret.encode()):
        raise HTTPException(403, "Invalid webhook token")
    if not database_url:
        raise HTTPException(503, "Database unavailable")
    try:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=2) as connection:
            store_update(connection, update)
    except (psycopg.OperationalError, psycopg.errors.UndefinedTable):
        raise HTTPException(503, "Database unavailable") from None
    return {"ok": True}
