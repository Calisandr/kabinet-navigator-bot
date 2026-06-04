from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import requests

from .config import Settings


DEFAULT_STATS_DB_PATH = ".data/user_stats.sqlite3"
DEFAULT_STATS_KEY_PREFIX = "kabinet_navigator"
ACTIVE_DAY_TTL_SECONDS = 90 * 24 * 60 * 60
ACTIVE_NOW_WINDOW_SECONDS = 15 * 60


@dataclass(frozen=True)
class UserStats:
    total_users: int
    active_today: int
    active_now: int
    storage_name: str
    is_persistent: bool


class UserStatsStore(Protocol):
    storage_name: str
    is_persistent: bool

    async def record_user(self, user_id: int, active_date: date) -> None:
        ...

    async def get_stats(self, active_date: date) -> UserStats:
        ...


class MemoryUserStatsStore:
    storage_name = "memory"
    is_persistent = False

    def __init__(self) -> None:
        self._users: set[int] = set()
        self._active_by_day: dict[str, set[int]] = {}
        self._last_seen_by_user: dict[int, datetime] = {}
        self._lock = asyncio.Lock()

    async def record_user(self, user_id: int, active_date: date) -> None:
        day_key = active_date.isoformat()
        async with self._lock:
            self._users.add(user_id)
            self._active_by_day.setdefault(day_key, set()).add(user_id)
            self._last_seen_by_user[user_id] = datetime.now(timezone.utc)

    async def get_stats(self, active_date: date) -> UserStats:
        day_key = active_date.isoformat()
        active_since = datetime.now(timezone.utc) - timedelta(seconds=ACTIVE_NOW_WINDOW_SECONDS)
        async with self._lock:
            return UserStats(
                total_users=len(self._users),
                active_today=len(self._active_by_day.get(day_key, set())),
                active_now=sum(
                    1 for last_seen_at in self._last_seen_by_user.values()
                    if last_seen_at >= active_since
                ),
                storage_name=self.storage_name,
                is_persistent=self.is_persistent,
            )


class SQLiteUserStatsStore:
    storage_name = "sqlite"
    is_persistent = True

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def record_user(self, user_id: int, active_date: date) -> None:
        await asyncio.to_thread(self._record_user_sync, user_id, active_date)

    async def get_stats(self, active_date: date) -> UserStats:
        return await asyncio.to_thread(self._get_stats_sync, active_date)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_active_date TEXT NOT NULL
            )
            """
        )
        return connection

    def _record_user_sync(self, user_id: int, active_date: date) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        day_key = active_date.isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_users (user_id, first_seen_at, last_seen_at, last_active_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_active_date = excluded.last_active_date
                """,
                (user_id, now, now, day_key),
            )

    def _get_stats_sync(self, active_date: date) -> UserStats:
        day_key = active_date.isoformat()
        active_since = (
            datetime.now(timezone.utc) - timedelta(seconds=ACTIVE_NOW_WINDOW_SECONDS)
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            total_users = connection.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
            active_today = connection.execute(
                "SELECT COUNT(*) FROM bot_users WHERE last_active_date = ?",
                (day_key,),
            ).fetchone()[0]
            active_now = connection.execute(
                "SELECT COUNT(*) FROM bot_users WHERE last_seen_at >= ?",
                (active_since,),
            ).fetchone()[0]
        return UserStats(
            total_users=total_users,
            active_today=active_today,
            active_now=active_now,
            storage_name=self.storage_name,
            is_persistent=self.is_persistent,
        )


class UpstashRedisUserStatsStore:
    storage_name = "upstash_redis"
    is_persistent = True

    def __init__(self, rest_url: str, rest_token: str, key_prefix: str) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {rest_token}"}
        self.key_prefix = key_prefix.strip() or DEFAULT_STATS_KEY_PREFIX

    async def record_user(self, user_id: int, active_date: date) -> None:
        day_key = active_date.isoformat()
        active_key = self._active_key(day_key)
        active_now_key = self._active_now_key()
        now_timestamp = int(datetime.now(timezone.utc).timestamp())
        commands = [
            ["SADD", self._users_key(), str(user_id)],
            ["SADD", active_key, str(user_id)],
            ["EXPIRE", active_key, ACTIVE_DAY_TTL_SECONDS],
            ["ZADD", active_now_key, now_timestamp, str(user_id)],
            ["EXPIRE", active_now_key, ACTIVE_NOW_WINDOW_SECONDS * 2],
        ]
        await asyncio.to_thread(self._run_pipeline, commands)

    async def get_stats(self, active_date: date) -> UserStats:
        day_key = active_date.isoformat()
        active_now_key = self._active_now_key()
        active_before = int(
            (
                datetime.now(timezone.utc) - timedelta(seconds=ACTIVE_NOW_WINDOW_SECONDS)
            ).timestamp()
        )
        result = await asyncio.to_thread(
            self._run_pipeline,
            [
                ["SCARD", self._users_key()],
                ["SCARD", self._active_key(day_key)],
                ["ZREMRANGEBYSCORE", active_now_key, "-inf", active_before],
                ["ZCARD", active_now_key],
            ],
        )
        return UserStats(
            total_users=int(result[0]),
            active_today=int(result[1]),
            active_now=int(result[3]),
            storage_name=self.storage_name,
            is_persistent=self.is_persistent,
        )

    def _users_key(self) -> str:
        return f"{self.key_prefix}:users"

    def _active_key(self, day_key: str) -> str:
        return f"{self.key_prefix}:active:{day_key}"

    def _active_now_key(self) -> str:
        return f"{self.key_prefix}:active_now"

    def _run_pipeline(self, commands: list[list[object]]) -> list[object]:
        response = requests.post(
            f"{self.rest_url}/pipeline",
            headers=self.headers,
            json=commands,
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(payload["error"])
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected Upstash Redis response format")

        results: list[object] = []
        for item in payload:
            if not isinstance(item, dict):
                raise RuntimeError("Unexpected Upstash Redis pipeline item format")
            if item.get("error"):
                raise RuntimeError(item["error"])
            results.append(item.get("result"))
        return results


def build_user_stats_store(settings: Settings) -> UserStatsStore:
    if settings.upstash_redis_rest_url and settings.upstash_redis_rest_token:
        return UpstashRedisUserStatsStore(
            settings.upstash_redis_rest_url,
            settings.upstash_redis_rest_token,
            settings.user_stats_key_prefix,
        )

    if os.getenv("VERCEL"):
        return MemoryUserStatsStore()

    return SQLiteUserStatsStore(settings.user_stats_db_path or DEFAULT_STATS_DB_PATH)
