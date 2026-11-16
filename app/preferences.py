"""One explicitly saved group per Telegram user, separate from activity counts."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Settings
from .group_pdf import normalize_group
from .stats import (
    DEFAULT_STATS_DB_PATH,
    DEFAULT_STATS_KEY_PREFIX,
    redis_rest_command,
    validate_redis_rest_url,
    validate_user_id,
)


_GROUP_CODE = re.compile(r"[A-ZА-ЯЁ]{1,8}-?\d{2,4}(?:\.\d{1,3}){0,3}\Z")
_CATEGORIES = frozenset({"vo", "spo", "part_time"})
MAX_SAVED_GROUP_BYTES = 512


@dataclass(frozen=True)
class SavedGroup:
    group: str
    category: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.group, str) or not 1 <= len(self.group) <= 80
            or any(ord(character) <= 32 for character in self.group)
            or not _GROUP_CODE.fullmatch(normalize_group(self.group))
            or not isinstance(self.category, str) or self.category not in _CATEGORIES
        ):
            raise ValueError("Saved group must contain a supported group code and category")


class UserPreferencesStore(Protocol):
    storage_name: str
    is_persistent: bool

    async def get_group(self, user_id: int) -> SavedGroup | None:
        ...

    async def set_group(self, user_id: int, group: SavedGroup) -> None:
        ...

    async def clear_group(self, user_id: int) -> None:
        ...


def _validate_group(group: SavedGroup) -> None:
    if not isinstance(group, SavedGroup):
        raise ValueError("Saved group must be a SavedGroup value")
    # Validate again at the storage boundary, including an object constructed
    # through a deserializer that bypassed the frozen dataclass constructor.
    group.__post_init__()


def _decode_group(value: object) -> SavedGroup:
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_SAVED_GROUP_BYTES:
            raise ValueError
        payload = json.loads(value)
        if (
            not isinstance(payload, dict) or set(payload) != {"version", "group", "category"}
            or type(payload["version"]) is not int or payload["version"] != 1
        ):
            raise ValueError
        return SavedGroup(payload["group"], payload["category"])
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise RuntimeError("Saved group data is invalid") from None


class MemoryUserPreferencesStore:
    storage_name = "memory"
    is_persistent = False

    def __init__(self) -> None:
        self._groups: dict[int, SavedGroup] = {}

    async def get_group(self, user_id: int) -> SavedGroup | None:
        validate_user_id(user_id)
        return self._groups.get(user_id)

    async def set_group(self, user_id: int, group: SavedGroup) -> None:
        validate_user_id(user_id)
        _validate_group(group)
        self._groups[user_id] = group

    async def clear_group(self, user_id: int) -> None:
        validate_user_id(user_id)
        self._groups.pop(user_id, None)


class SQLiteUserPreferencesStore:
    storage_name = "sqlite"
    is_persistent = True

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def get_group(self, user_id: int) -> SavedGroup | None:
        validate_user_id(user_id)
        return await asyncio.to_thread(self._get_group_sync, user_id)

    async def set_group(self, user_id: int, group: SavedGroup) -> None:
        validate_user_id(user_id)
        _validate_group(group)
        await asyncio.to_thread(self._set_group_sync, user_id, group)

    async def clear_group(self, user_id: int) -> None:
        validate_user_id(user_id)
        await asyncio.to_thread(self._clear_group_sync, user_id)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_group_preferences (
                    user_id INTEGER PRIMARY KEY,
                    group_code TEXT NOT NULL,
                    category TEXT NOT NULL
                )
                """
            )
            return connection
        except BaseException:
            connection.close()
            raise

    def _get_group_sync(self, user_id: int) -> SavedGroup | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT group_code, category FROM user_group_preferences WHERE user_id = ?", (user_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return SavedGroup(*row)
        except (TypeError, ValueError):
            raise RuntimeError("Saved group data is invalid") from None

    def _set_group_sync(self, user_id: int, group: SavedGroup) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO user_group_preferences (user_id, group_code, category) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET group_code = excluded.group_code, category = excluded.category
                """,
                (user_id, group.group, group.category),
            )

    def _clear_group_sync(self, user_id: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM user_group_preferences WHERE user_id = ?", (user_id,))


class UpstashRedisUserPreferencesStore:
    storage_name = "upstash_redis"
    is_persistent = True

    def __init__(self, rest_url: str, rest_token: str, key_prefix: str) -> None:
        self.rest_url = validate_redis_rest_url(rest_url)
        if not isinstance(rest_token, str) or not rest_token or any(character.isspace() for character in rest_token):
            raise ValueError("Redis REST authentication token must be a non-empty value without whitespace")
        self.headers = {"Authorization": f"Bearer {rest_token}"}
        self.key_prefix = key_prefix.strip() or DEFAULT_STATS_KEY_PREFIX

    def _key(self, user_id: int) -> str:
        return f"{self.key_prefix}:preferences:{user_id}"

    async def get_group(self, user_id: int) -> SavedGroup | None:
        validate_user_id(user_id)
        result = await asyncio.to_thread(redis_rest_command, self.rest_url, self.headers, ["GET", self._key(user_id)])
        return None if result is None else _decode_group(result)

    async def set_group(self, user_id: int, group: SavedGroup) -> None:
        validate_user_id(user_id)
        _validate_group(group)
        value = json.dumps({"version": 1, "group": group.group, "category": group.category}, ensure_ascii=False)
        result = await asyncio.to_thread(
            redis_rest_command, self.rest_url, self.headers, ["SET", self._key(user_id), value],
        )
        if result != "OK":
            raise RuntimeError("Unexpected Redis saved group write result")

    async def clear_group(self, user_id: int) -> None:
        validate_user_id(user_id)
        result = await asyncio.to_thread(redis_rest_command, self.rest_url, self.headers, ["DEL", self._key(user_id)])
        if type(result) is not int or result not in (0, 1):
            raise RuntimeError("Unexpected Redis saved group removal result")


def build_user_preferences_store(settings: Settings) -> UserPreferencesStore:
    if settings.upstash_redis_rest_url and settings.upstash_redis_rest_token:
        return UpstashRedisUserPreferencesStore(
            settings.upstash_redis_rest_url, settings.upstash_redis_rest_token, settings.user_stats_key_prefix,
        )
    if os.getenv("VERCEL"):
        return MemoryUserPreferencesStore()
    return SQLiteUserPreferencesStore(settings.user_stats_db_path or DEFAULT_STATS_DB_PATH)
