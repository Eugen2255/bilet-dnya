import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from math import ceil
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ModelError(Exception):
    def __init__(self, code: str, *, retry_after: int | None = None, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.retryable = retryable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # An endpoint must be configured directly so the bearer token stays on that host.
        return None


def retry_delay(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0, ceil((date - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


@dataclass(frozen=True)
class ChatClient:
    url: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout: float = 12.0

    def __post_init__(self) -> None:
        url = urlsplit(self.url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not url.path.endswith("/chat/completions")
        ):
            raise ValueError("Set MODEL_PRIMARY_URL to the full chat completions endpoint")

    @classmethod
    def from_env(cls, prefix: str = "MODEL_PRIMARY") -> "ChatClient":
        url, model = os.getenv(f"{prefix}_URL", "").strip(), os.getenv(f"{prefix}_ID", "").strip()
        if not url or not model:
            raise ValueError(f"Set {prefix}_URL and {prefix}_ID")
        return cls(url, model, os.getenv(f"{prefix}_API_KEY", "").strip())

    def complete(self, messages: list[dict[str, str]], *, temperature: float = 0, max_tokens: int = 500) -> str:
        payload = self._request_json(self.url, {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        })
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") in {"length", "content_filter"} or not isinstance(content, str):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("INVALID_COMPLETION") from None
        return content

    def list_models(self) -> list[str]:
        payload = self._request_json(self.url.removesuffix("/chat/completions") + "/models")
        try:
            return [model["id"] for model in payload["data"] if isinstance(model["id"], str)]
        except (KeyError, TypeError):
            raise ModelError("INVALID_MODEL_LIST") from None

    def _request_json(self, url: str, body: dict | None = None):
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                payload = response.read(256 * 1024 + 1)
        except HTTPError as exc:
            with exc:
                raise ModelError(
                    f"MODEL_HTTP_{exc.code}",
                    retry_after=retry_delay(exc.headers.get("Retry-After")) if exc.code == 429 else None,
                    retryable=exc.code in {408, 429} or exc.code >= 500,
                ) from None
        except (OSError, HTTPException):
            raise ModelError("MODEL_UNAVAILABLE") from None

        try:
            if len(payload) > 256 * 1024:
                raise ValueError
            return json.loads(payload)
        except ValueError:
            raise ModelError("INVALID_MODEL_RESPONSE") from None
