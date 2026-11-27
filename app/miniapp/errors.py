from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

from app.imports.errors import ImportProblem


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **details):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def error_response(error: ApiError) -> JSONResponse:
    return JSONResponse({
        "error": {
            "code": error.code, "message": str(error), "requestId": str(uuid4()),
            "retryable": error.status >= 500, **error.details,
        },
    }, status_code=error.status)


async def api_error_handler(_request: Request, error: ApiError) -> JSONResponse:
    return error_response(error)


async def import_error_handler(_request: Request, error: ImportProblem) -> JSONResponse:
    status = {"NOT_FOUND": 404, "STATE_CONFLICT": 409, "FILE_TOO_LARGE": 413, "STORAGE_UNAVAILABLE": 503}
    detail = {key: value for key, value in error.detail().items() if key not in {"code", "message"}}
    return error_response(ApiError(error.code, str(error), status.get(error.code, 400), **detail))


async def validation_handler(request: Request, error: RequestValidationError) -> JSONResponse:
    if request.url.path.startswith("/v1/"):
        return error_response(ApiError("INVALID_INPUT", "Проверьте поля запроса."))
    return await request_validation_exception_handler(request, error)
