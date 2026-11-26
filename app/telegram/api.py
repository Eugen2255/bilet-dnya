import json
from functools import cached_property
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code: int | None = None, retry_after: int | None = None):
        super().__init__(f"Telegram API error {code}" if code is not None else "Telegram API unavailable")
        self.code = code
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.code is None or self.code in {408, 429} or self.code >= 500


class BotAPI:
    def __init__(self, token: str):
        self._token = token

    def call(self, method: str, data: dict) -> object:
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except HTTPError as exc:
            with exc:
                try:
                    payload = json.load(exc)
                except (ValueError, OSError, HTTPException):
                    payload = {}
            raise response_error(payload, exc.code) from None
        except (OSError, HTTPException, ValueError):
            raise TelegramError() from None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise response_error(payload)
        return payload.get("result")

    @cached_property
    def username(self) -> str:
        result = self.call("getMe", {})
        if not isinstance(result, dict) or not isinstance(result.get("username"), str):
            raise TelegramError()
        return result["username"]

    def get_member(self, chat_id: int, user_id: int) -> dict:
        result = self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise TelegramError()
        return result

    def send(self, payload: dict) -> int:
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise TelegramError()
        return result["message_id"]

    def answer_callback(self, payload: dict) -> None:
        self.call("answerCallbackQuery", payload)


def response_error(payload: object, status: int | None = None) -> TelegramError:
    if not isinstance(payload, dict):
        return TelegramError(status)
    code = payload.get("error_code")
    parameters = payload.get("parameters")
    delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
    return TelegramError(code if type(code) is int else status, delay if type(delay) is int and delay > 0 else None)
