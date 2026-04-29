from __future__ import annotations

import sys

import requests

from app.config import load_settings


COMMANDS = [
    {"command": "start", "description": "открыть меню"},
    {"command": "today", "description": "расписание на сегодня"},
    {"command": "tomorrow", "description": "расписание на завтра"},
    {"command": "next", "description": "ближайшие даты"},
    {"command": "date", "description": "расписание на дату"},
    {"command": "teacher", "description": "поиск учителя"},
    {"command": "teachers", "description": "список учителей"},
    {"command": "refresh", "description": "обновить таблицу"},
    {"command": "help", "description": "помощь"},
]


def main() -> None:
    settings = load_settings()
    webhook_url = sys.argv[1].strip() if len(sys.argv) > 1 else settings.webhook_url
    if not webhook_url:
        raise SystemExit(
            "Pass Vercel URL as an argument or set WEBHOOK_URL in .env. "
            "Example: python -m scripts.set_webhook https://name.vercel.app"
        )

    webhook_url = normalize_webhook_url(webhook_url)
    api_base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

    webhook_payload = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }
    if settings.webhook_secret:
        webhook_payload["secret_token"] = settings.webhook_secret

    set_webhook = requests.post(
        f"{api_base}/setWebhook",
        json=webhook_payload,
        timeout=30,
    )
    set_webhook.raise_for_status()

    set_commands = requests.post(
        f"{api_base}/setMyCommands",
        json={"commands": COMMANDS},
        timeout=30,
    )
    set_commands.raise_for_status()

    info = requests.get(f"{api_base}/getWebhookInfo", timeout=30)
    info.raise_for_status()

    print("Webhook URL:", webhook_url)
    print("setWebhook:", set_webhook.json())
    print("setMyCommands:", set_commands.json())
    print("getWebhookInfo:", info.json())


def normalize_webhook_url(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/api/webhook"):
        return url
    return f"{url}/api/webhook"


if __name__ == "__main__":
    main()
