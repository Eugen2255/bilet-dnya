from dotenv import load_dotenv
from fastapi import FastAPI

from app.telegram.webhook import router as telegram_router

load_dotenv()
app = FastAPI(title="Билет дня", version="0.1.0")
app.include_router(telegram_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
