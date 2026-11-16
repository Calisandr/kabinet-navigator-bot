from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from .config import Settings


DEFAULT_STATS_DB_PATH = ".data/user_stats.sqlite3"
DEFAULT_STATS_KEY_PREFIX = "kabinet_navigator"
ACTIVE_NOW_WINDOW_SECONDS = 5 * 60
ACTIVE_24H_WINDOW_SECONDS = 24 * 60 * 60
MAX_TELEGRAM_USER_ID = (1 << 52) - 1
MAX_REDIS_RESPONSE_BYTES = 64 * 1024
REDIS_REQUEST_TIMEOUT = (0.75, 1.0)


def validate_user_id(user_id: int) -> None:
    if type(user_id) is not int or not 1 <= user_id <= MAX_TELEGRAM_USER_ID:
        raise ValueError("Telegram user ID must be a positive integer within the supported range")


def validate_redis_rest_url(value: str) -> str:
    """Validate operator configuration without accepting arbitrary redirect targets."""
    try:
        if not isinstance(value, str) or any(ord(character) <= 32 for character in value):
            raise ValueError
        parsed = urlsplit(value)
        hostname = parsed.hostname
        canonical_hostname = hostname.rstrip(".").lower() if hostname else ""
        if (
            parsed.scheme != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or "\\" in value or canonical_hostname == "localhost"
            or canonical_hostname.endswith((".localhost", ".local"))
            or "." not in canonical_hostname
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(canonical_hostname)
        except ValueError:
            # getaddrinfo also recognizes abbreviated/octal IPv4 forms such as
            # 127.1 and 0177.0.0.1. Parse those without performing a DNS lookup.
            try:
                address = ipaddress.IPv4Address(socket.inet_aton(canonical_hostname))
            except OSError:
                address = None
        if address is not None and not address.is_global:
            raise ValueError
        return urlunsplit(("https", parsed.netloc, "", "", ""))
    except (TypeError, ValueError):
        raise ValueError("Redis REST endpoint must be an HTTPS root URL without credentials or parameters") from None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Activity timestamps must be timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def timestamp_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


@dataclass(frozen=True)
class UserStats:
    total_users: int
    active_now: int
    active_24h: int
    storage_name: str
    is_persistent: bool
    observed_at: datetime
    tracking_since: datetime | None


class UserStatsStore(Protocol):
    storage_name: str
    is_persistent: bool

    async def record_user(self, user_id: int, observed_at: datetime | None = None) -> None:
        ...

    async def get_stats(self, now: datetime | None = None) -> UserStats:
        ...


class MemoryUserStatsStore:
    storage_name = "memory"
    is_persistent = False

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._last_seen_by_user: dict[int, datetime] = {}
        self._tracking_since: datetime | None = None
        self._lock = asyncio.Lock()

    def _touch_tracking_since(self, observed_at: datetime) -> None:
        if self._tracking_since is None or observed_at < self._tracking_since:
            self._tracking_since = observed_at

    async def record_user(self, user_id: int, observed_at: datetime | None = None) -> None:
        validate_user_id(user_id)
        async with self._lock:
            observed_at = as_utc(observed_at if observed_at is not None else self._clock())
            previous = self._last_seen_by_user.get(user_id)
            if previous is None or observed_at > previous:
                self._last_seen_by_user[user_id] = observed_at
            self._touch_tracking_since(observed_at)

    async def get_stats(self, now: datetime | None = None) -> UserStats:
        async with self._lock:
            now = as_utc(now if now is not None else self._clock())
            active_since = now - timedelta(seconds=ACTIVE_NOW_WINDOW_SECONDS)
            day_since = now - timedelta(seconds=ACTIVE_24H_WINDOW_SECONDS)
            self._touch_tracking_since(now)
            return UserStats(
                total_users=len(self._last_seen_by_user),
                active_now=sum(active_since <= last_seen <= now for last_seen in self._last_seen_by_user.values()),
                active_24h=sum(day_since <= last_seen <= now for last_seen in self._last_seen_by_user.values()),
                storage_name=self.storage_name,
                is_persistent=self.is_persistent,
                observed_at=now,
                tracking_since=self._tracking_since,
            )


class SQLiteUserStatsStore:
    storage_name = "sqlite"
    is_persistent = True

    def __init__(self, db_path: str | Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.db_path = Path(db_path)
        self._clock = clock

    async def record_user(self, user_id: int, observed_at: datetime | None = None) -> None:
        validate_user_id(user_id)
        if observed_at is not None:
            observed_at = as_utc(observed_at)
        await asyncio.to_thread(self._record_user_sync, user_id, observed_at)

    async def get_stats(self, now: datetime | None = None) -> UserStats:
        if now is not None:
            now = as_utc(now)
        return await asyncio.to_thread(self._get_stats_sync, now)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=5)
        try:
            # The schema migration, metadata, and counters share one transaction.
            connection.execute("BEGIN IMMEDIATE")
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
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bot_stats_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            version = connection.execute(
                "SELECT value FROM bot_stats_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version is None:
                # Legacy rows used second precision. Normalize once so exact
                # window boundaries compare correctly with new microsecond rows.
                legacy_rows = connection.execute(
                    "SELECT user_id, first_seen_at, last_seen_at FROM bot_users"
                ).fetchall()
                for user_id, first_seen, last_seen in legacy_rows:
                    first_seen = as_utc(datetime.fromisoformat(first_seen))
                    last_seen = as_utc(datetime.fromisoformat(last_seen))
                    connection.execute(
                        "UPDATE bot_users SET first_seen_at = ?, last_seen_at = ? WHERE user_id = ?",
                        (timestamp_text(first_seen), timestamp_text(last_seen), user_id),
                    )
                earliest = connection.execute("SELECT MIN(first_seen_at) FROM bot_users").fetchone()[0]
                if earliest is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO bot_stats_metadata (key, value) VALUES ('tracking_since', ?)",
                        (earliest,),
                    )
                connection.execute(
                    "INSERT INTO bot_stats_metadata (key, value) VALUES ('schema_version', '2')"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS bot_users_last_seen_idx ON bot_users (last_seen_at)"
            )
            return connection
        except BaseException:
            connection.close()
            raise

    def _touch_tracking_since(self, connection: sqlite3.Connection, observed_at: datetime) -> None:
        connection.execute(
            """
            INSERT INTO bot_stats_metadata (key, value) VALUES ('tracking_since', ?)
            ON CONFLICT(key) DO UPDATE SET value = MIN(value, excluded.value)
            """,
            (timestamp_text(observed_at),),
        )

    def _record_user_sync(self, user_id: int, observed_at: datetime | None) -> None:
        with closing(self._connect()) as connection, connection:
            observed_at = as_utc(observed_at if observed_at is not None else self._clock())
            observed_text = timestamp_text(observed_at)
            self._touch_tracking_since(connection, observed_at)
            connection.execute(
                """
                INSERT INTO bot_users (user_id, first_seen_at, last_seen_at, last_active_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_seen_at = MIN(first_seen_at, excluded.first_seen_at),
                    last_seen_at = MAX(last_seen_at, excluded.last_seen_at),
                    last_active_date = CASE
                        WHEN excluded.last_seen_at > last_seen_at THEN excluded.last_active_date
                        ELSE last_active_date
                    END
                """,
                (user_id, observed_text, observed_text, observed_at.date().isoformat()),
            )

    def _get_stats_sync(self, now: datetime | None) -> UserStats:
        with closing(self._connect()) as connection, connection:
            now = as_utc(now if now is not None else self._clock())
            active_since = now - timedelta(seconds=ACTIVE_NOW_WINDOW_SECONDS)
            day_since = now - timedelta(seconds=ACTIVE_24H_WINDOW_SECONDS)
            now_text = timestamp_text(now)
            self._touch_tracking_since(connection, now)
            total_users, active_now, active_24h = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM bot_users),
                    (SELECT COUNT(*) FROM bot_users WHERE last_seen_at BETWEEN ? AND ?),
                    (SELECT COUNT(*) FROM bot_users WHERE last_seen_at BETWEEN ? AND ?)
                """,
                (timestamp_text(active_since), now_text, timestamp_text(day_since), now_text),
            ).fetchone()
            tracking_since = connection.execute(
                "SELECT value FROM bot_stats_metadata WHERE key = 'tracking_since'"
            ).fetchone()[0]
        return UserStats(
            total_users=total_users,
            active_now=active_now,
            active_24h=active_24h,
            storage_name=self.storage_name,
            is_persistent=self.is_persistent,
            observed_at=now,
            tracking_since=as_utc(datetime.fromisoformat(tracking_since)),
        )


RECORD_ACTIVITY_SCRIPT = """
local timestamp = ARGV[2]
if timestamp == '' then
    local clock = redis.call('TIME')
    timestamp = string.format('%.6f', tonumber(clock[1]) + tonumber(clock[2]) / 1000000)
end
local since = redis.call('GET', KEYS[3])
if not since or tonumber(timestamp) < tonumber(since) then
    redis.call('SET', KEYS[3], timestamp)
end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], 'GT', timestamp, ARGV[1])
return 1
"""

READ_ACTIVITY_SCRIPT = """
local timestamp = ARGV[1]
if timestamp == '' then
    local clock = redis.call('TIME')
    timestamp = string.format('%.6f', tonumber(clock[1]) + tonumber(clock[2]) / 1000000)
end
local since = redis.call('GET', KEYS[3])
if not since or tonumber(timestamp) < tonumber(since) then
    redis.call('SET', KEYS[3], timestamp)
    since = timestamp
end
local active_since = string.format('%.6f', tonumber(timestamp) - tonumber(ARGV[2]))
local day_since = string.format('%.6f', tonumber(timestamp) - tonumber(ARGV[3]))
return {
    redis.call('SCARD', KEYS[1]),
    redis.call('ZCOUNT', KEYS[2], active_since, timestamp),
    redis.call('ZCOUNT', KEYS[2], day_since, timestamp),
    since,
    timestamp
}
"""


class UpstashRedisUserStatsStore:
    storage_name = "upstash_redis"
    is_persistent = True

    def __init__(
        self, rest_url: str, rest_token: str, key_prefix: str, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.rest_url = validate_redis_rest_url(rest_url)
        if not isinstance(rest_token, str) or not rest_token or any(character.isspace() for character in rest_token):
            raise ValueError("Redis REST authentication token must be a non-empty value without whitespace")
        self.headers = {"Authorization": f"Bearer {rest_token}"}
        self.key_prefix = key_prefix.strip() or DEFAULT_STATS_KEY_PREFIX
        self._clock = clock

    def _keys(self) -> list[str]:
        # Keep the existing all-time users set. The new last_seen index has no
        # TTL; the legacy active_now index expired and cannot recover 24 hours.
        return [
            f"{self.key_prefix}:users",
            f"{self.key_prefix}:last_seen",
            f"{self.key_prefix}:activity_tracking_since",
        ]

    async def record_user(self, user_id: int, observed_at: datetime | None = None) -> None:
        validate_user_id(user_id)
        if observed_at is None and self._clock is not None:
            observed_at = self._clock()
        timestamp = f"{as_utc(observed_at).timestamp():.6f}" if observed_at is not None else ""
        result = await asyncio.to_thread(
            self._run_script, RECORD_ACTIVITY_SCRIPT, [str(user_id), timestamp]
        )
        if type(result) is not int or result != 1:
            raise RuntimeError("Unexpected Upstash Redis record result")

    async def get_stats(self, now: datetime | None = None) -> UserStats:
        if now is None and self._clock is not None:
            now = self._clock()
        timestamp = f"{as_utc(now).timestamp():.6f}" if now is not None else ""
        result = await asyncio.to_thread(
            self._run_script,
            READ_ACTIVITY_SCRIPT,
            [
                timestamp,
                str(ACTIVE_NOW_WINDOW_SECONDS),
                str(ACTIVE_24H_WINDOW_SECONDS),
            ],
        )
        if not isinstance(result, list) or len(result) != 5:
            raise RuntimeError("Unexpected Upstash Redis activity response format")
        try:
            if any(type(item) is not int for item in result[:3]):
                raise ValueError("Activity counters must be integers")
            counts = result[:3]
            if any(item < 0 for item in counts) or not counts[0] >= counts[2] >= counts[1]:
                raise ValueError("Inconsistent activity counters")
            tracking_since = datetime.fromtimestamp(float(result[3]), timezone.utc)
            observed_at = datetime.fromtimestamp(float(result[4]), timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise RuntimeError("Invalid Upstash Redis activity response") from exc
        return UserStats(
            total_users=counts[0],
            active_now=counts[1],
            active_24h=counts[2],
            storage_name=self.storage_name,
            is_persistent=self.is_persistent,
            observed_at=observed_at,
            tracking_since=tracking_since,
        )

    def _run_script(self, script: str, args: list[str]) -> object:
        keys = self._keys()
        response = None
        try:
            response = requests.post(
                self.rest_url,
                headers=self.headers,
                json=["EVAL", script, len(keys), *keys, *args],
                timeout=REDIS_REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
            if 300 <= response.status_code < 400:
                raise RuntimeError("Redis REST redirects are not permitted")
            response.raise_for_status()
            content_length = response.headers.get("Content-Length", "")
            if content_length.isdigit() and int(content_length) > MAX_REDIS_RESPONSE_BYTES:
                raise RuntimeError("Redis REST response exceeds the allowed size")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                content.extend(chunk)
                if len(content) > MAX_REDIS_RESPONSE_BYTES:
                    raise RuntimeError("Redis REST response exceeds the allowed size")
            payload = json.loads(content)
        except requests.RequestException:
            raise RuntimeError("Redis REST request failed") from None
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise RuntimeError("Redis REST response was not valid JSON") from None
        finally:
            if response is not None:
                response.close()
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Upstash Redis response format")
        if payload.get("error"):
            raise RuntimeError("Upstash Redis rejected the activity request")
        if "result" not in payload:
            raise RuntimeError("Missing Upstash Redis activity result")
        return payload["result"]


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
