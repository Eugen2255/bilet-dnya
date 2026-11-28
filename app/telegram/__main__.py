import argparse
import os
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv

from app.telegram.api import BotAPI, TelegramError


def main() -> None:
    parser = argparse.ArgumentParser(description="Установить webhook для тестового Telegram-бота")
    parser.add_argument("url", help="Публичный HTTPS-адрес /telegram/webhook")
    args = parser.parse_args()
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    url = urlsplit(args.url)
    if url.scheme != "https" or not url.hostname or not url.path.endswith("/telegram/webhook"):
        parser.error("Укажите полный HTTPS-адрес /telegram/webhook")
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        parser.error("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_WEBHOOK_SECRET в .env")
    try:
        result = BotAPI(token).call("setWebhook", {
            "url": args.url,
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query", "my_chat_member"],
        })
    except TelegramError as exc:
        parser.exit(1, f"Webhook не установлен. {exc}\n")
    if result is not True:
        parser.exit(1, "Telegram не подтвердил установку webhook.\n")
    print("Webhook установлен.")


if __name__ == "__main__":
    main()
