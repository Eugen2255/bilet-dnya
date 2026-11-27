import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import psycopg
from fastapi import Cookie, Depends, Request
from psycopg import Connection
from psycopg.rows import dict_row

from app.miniapp.errors import ApiError


SESSION_COOKIE = "bilet_session"
SESSION_AGE = 8 * 60 * 60
INIT_MAX_AGE = 5 * 60


@dataclass(frozen=True)
class Session:
    user_id: UUID
    csrf_hash: str


def verify_init_data(raw: str, bot_token: str, now: datetime) -> int:
    try:
        fields = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=40, errors="strict")
        data = dict(fields)
        if len(data) != len(fields) or any(
            not re.fullmatch(r"[A-Za-z0-9_]+", key) or "\n" in value or "\r" in value
            for key, value in fields
        ):
            raise ValueError
        signature = data.pop("hash")
        message = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        key = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        expected = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise ValueError
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**63:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ApiError("AUTH_INVALID", "Не удалось проверить вход через Telegram.", 401) from None
    if not 0 <= now.timestamp() - auth_date <= INIT_MAX_AGE:
        raise ApiError("AUTH_EXPIRED", "Откройте приложение заново из Telegram.", 401)
    return user["id"]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def db_connection():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ApiError("SERVICE_UNAVAILABLE", "База данных недоступна.", 503)
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=2, row_factory=dict_row) as connection:
            yield connection
    except (psycopg.OperationalError, psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        raise ApiError("SERVICE_UNAVAILABLE", "База данных недоступна.", 503) from None


Database = Annotated[Connection, Depends(db_connection)]


def session_from_cookie(db: Database, token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None) -> Session:
    if not token or len(token) > 100:
        raise ApiError("AUTH_EXPIRED", "Откройте приложение заново из Telegram.", 401)
    row = db.execute("""
        SELECT user_id, csrf_hash FROM web_sessions WHERE token_hash = %s AND expires_at > clock_timestamp()
    """, (digest(token),)).fetchone()
    if row is None:
        raise ApiError("AUTH_EXPIRED", "Откройте приложение заново из Telegram.", 401)
    return Session(**row)


Reader = Annotated[Session, Depends(session_from_cookie)]


def check_origin(request: Request) -> None:
    configured = os.getenv("MINI_APP_URL", "")
    origin = urlsplit(configured or str(request.base_url))
    if (origin.scheme not in {"http", "https"} or not origin.netloc
            or (not configured and origin.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ApiError("SERVICE_UNAVAILABLE", "Адрес Mini App не настроен.", 503)
    if request.headers.get("origin") != f"{origin.scheme}://{origin.netloc}":
        raise ApiError("FORBIDDEN", "Запрос пришёл с другого сайта.", 403)


def write_session(request: Request, reader: Reader) -> Session:
    check_origin(request)
    csrf = request.headers.get("X-CSRF-Token", "")
    if not csrf or not hmac.compare_digest(digest(csrf), reader.csrf_hash):
        raise ApiError("FORBIDDEN", "Запрос не прошёл проверку сессии. Откройте приложение заново.", 403)
    return reader


Writer = Annotated[Session, Depends(write_session)]


def create_session(db: Connection, telegram_id: int) -> tuple[UUID, str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db.transaction():
        user_id = db.execute("""
            INSERT INTO users (telegram_user_id) VALUES (%s)
            ON CONFLICT (telegram_user_id) DO UPDATE SET telegram_user_id = EXCLUDED.telegram_user_id
            RETURNING id
        """, (telegram_id,)).fetchone()["id"]
        db.execute("DELETE FROM web_sessions WHERE expires_at <= clock_timestamp()")
        db.execute("""
            INSERT INTO web_sessions (token_hash, user_id, csrf_hash, expires_at)
            VALUES (%s, %s, %s, %s)
        """, (digest(token), user_id, digest(csrf), datetime.now(timezone.utc) + timedelta(seconds=SESSION_AGE)))
    return user_id, token, csrf
