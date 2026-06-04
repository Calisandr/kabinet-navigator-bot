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
    admin_user_ids: frozenset[int]
    user_stats_db_path: str
    upstash_redis_rest_url: str
    upstash_redis_rest_token: str
    user_stats_key_prefix: str


def parse_admin_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        value = item.strip()
        if not value:
            continue
        try:
            ids.add(int(value))
        except ValueError as exc:
            raise RuntimeError(
                "ADMIN_USER_IDS must contain Telegram numeric user IDs separated by commas."
            ) from exc
    return frozenset(ids)


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
        admin_user_ids=parse_admin_user_ids(os.getenv("ADMIN_USER_IDS", "")),
        user_stats_db_path=os.getenv("USER_STATS_DB_PATH", ".data/user_stats.sqlite3").strip(),
        upstash_redis_rest_url=os.getenv("UPSTASH_REDIS_REST_URL", "").strip(),
        upstash_redis_rest_token=os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip(),
        user_stats_key_prefix=os.getenv("USER_STATS_KEY_PREFIX", "kabinet_navigator").strip()
        or "kabinet_navigator",
    )
