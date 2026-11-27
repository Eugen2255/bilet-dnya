from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from app.imports.errors import ImportProblem
from app.miniapp.errors import ApiError, api_error_handler, import_error_handler, validation_handler
from app.miniapp.routes import router as miniapp_router
from app.telegram.webhook import router as telegram_router

load_dotenv()
app = FastAPI(title="Билет дня", version="0.1.0")
app.include_router(telegram_router)
app.include_router(miniapp_router)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(ImportProblem, import_error_handler)
app.add_exception_handler(RequestValidationError, validation_handler)
app.mount("/app", StaticFiles(directory=Path(__file__).parent / "miniapp" / "static", html=True), name="miniapp")


@app.middleware("http")
async def prevent_miniapp_api_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/v1/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
