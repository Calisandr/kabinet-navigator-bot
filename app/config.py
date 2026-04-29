from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


DEFAULT_SHEET_ID = "14inaoG-X5D6U3pLb1o0n3PwpZkSSlxGwoNlb6wVsU0Q"


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    google_sheet_id: str
    timezone: str
    cache_minutes: int
    webhook_secret: str
    webhook_url: str


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is empty. Put the bot token into .env or hosting secrets."
        )

    cache_minutes_raw = os.getenv("CACHE_MINUTES", "15").strip()
    try:
        cache_minutes = max(1, int(cache_minutes_raw))
    except ValueError:
        cache_minutes = 15

    return Settings(
        telegram_bot_token=token,
        google_sheet_id=os.getenv("GOOGLE_SHEET_ID", DEFAULT_SHEET_ID).strip()
        or DEFAULT_SHEET_ID,
        timezone=os.getenv("BOT_TIMEZONE", "Asia/Krasnoyarsk").strip()
        or "Asia/Krasnoyarsk",
        cache_minutes=cache_minutes,
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        webhook_url=os.getenv("WEBHOOK_URL", "").strip(),
    )
