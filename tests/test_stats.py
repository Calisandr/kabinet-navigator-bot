from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from app.config import parse_admin_user_ids
from app.stats import (
    ACTIVE_24H_WINDOW_SECONDS,
    ACTIVE_NOW_WINDOW_SECONDS,
    READ_ACTIVITY_SCRIPT,
    REDIS_REQUEST_TIMEOUT,
    RECORD_ACTIVITY_SCRIPT,
    MemoryUserStatsStore,
    SQLiteUserStatsStore,
    UpstashRedisUserStatsStore,
    build_user_stats_store,
)


NOW = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "sqlite"])
def local_store(request, tmp_path):
    if request.param == "memory":
        return MemoryUserStatsStore(clock=lambda: NOW)
    return SQLiteUserStatsStore(tmp_path / "stats.sqlite3", clock=lambda: NOW)


def test_empty_store_reports_zero_users_and_start_of_tracking(local_store) -> None:
    stats = asyncio.run(local_store.get_stats())

    assert (stats.total_users, stats.active_now, stats.active_24h) == (0, 0, 0)
    assert stats.observed_at == NOW
    assert stats.tracking_since == NOW
    assert stats.is_persistent == (stats.storage_name == "sqlite")


def test_stats_count_people_once_across_repeated_requests(local_store) -> None:
    async def scenario():
        await local_store.record_user(1, NOW - timedelta(hours=30))
        await local_store.record_user(1, NOW)
        await local_store.record_user(1, NOW)
        await local_store.record_user(2, NOW - timedelta(minutes=4))
        await local_store.record_user(3, NOW - timedelta(minutes=20))
        await local_store.record_user(4, NOW - timedelta(hours=25))
        return await local_store.get_stats(NOW)

    stats = asyncio.run(scenario())

    assert (stats.total_users, stats.active_now, stats.active_24h) == (4, 2, 3)
    assert stats.tracking_since == NOW - timedelta(hours=30)


def test_activity_windows_have_exact_boundaries_and_exclude_future(local_store) -> None:
    async def scenario():
        times = [
            NOW,
            NOW - timedelta(seconds=ACTIVE_NOW_WINDOW_SECONDS),
            NOW - timedelta(seconds=ACTIVE_NOW_WINDOW_SECONDS, microseconds=1),
            NOW - timedelta(seconds=ACTIVE_24H_WINDOW_SECONDS),
            NOW - timedelta(seconds=ACTIVE_24H_WINDOW_SECONDS, microseconds=1),
            NOW + timedelta(microseconds=1),
        ]
        for user_id, seen_at in enumerate(times, start=1):
            await local_store.record_user(user_id, seen_at)
        return await local_store.get_stats(NOW)

    stats = asyncio.run(scenario())

    assert (stats.total_users, stats.active_now, stats.active_24h) == (6, 2, 4)


def test_recent_activity_survives_midnight_and_timezone_offsets(local_store) -> None:
    midnight = datetime(2026, 11, 16, 0, 1, tzinfo=timezone.utc)
    local_timezone = timezone(timedelta(hours=7))

    async def scenario():
        await local_store.record_user(1, midnight - timedelta(minutes=2))
        await local_store.record_user(2, (midnight - timedelta(hours=23)).astimezone(local_timezone))
        await local_store.record_user(3, midnight - timedelta(hours=24, minutes=1))
        return await local_store.get_stats(midnight.astimezone(local_timezone))

    stats = asyncio.run(scenario())

    assert (stats.total_users, stats.active_now, stats.active_24h) == (3, 1, 2)
    assert stats.observed_at == midnight
    assert stats.observed_at.tzinfo is timezone.utc


def test_out_of_order_requests_do_not_move_last_seen_backwards(local_store) -> None:
    async def scenario():
        await local_store.record_user(1, NOW)
        await local_store.record_user(1, NOW - timedelta(days=2))
        return await local_store.get_stats(NOW)

    stats = asyncio.run(scenario())

    assert (stats.total_users, stats.active_now, stats.active_24h) == (1, 1, 1)


def test_activity_expires_but_lifetime_total_remains(local_store) -> None:
    async def scenario():
        await local_store.record_user(1, NOW)
        after_five_minutes = await local_store.get_stats(NOW + timedelta(minutes=5, microseconds=1))
        after_one_day = await local_store.get_stats(NOW + timedelta(days=1, microseconds=1))
        return after_five_minutes, after_one_day

    recent, expired = asyncio.run(scenario())

    assert (recent.total_users, recent.active_now, recent.active_24h) == (1, 0, 1)
    assert (expired.total_users, expired.active_now, expired.active_24h) == (1, 0, 0)
    assert expired.tracking_since == NOW


def test_default_clock_is_used_for_record_and_snapshot(local_store) -> None:
    async def scenario():
        await local_store.record_user(1)
        return await local_store.get_stats()

    stats = asyncio.run(scenario())

    assert (stats.total_users, stats.active_now, stats.active_24h) == (1, 1, 1)
    assert stats.observed_at == NOW


def test_naive_timestamps_are_rejected(local_store) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(local_store.record_user(1, datetime(2026, 11, 16)))
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(local_store.get_stats(datetime(2026, 11, 16)))


def test_sqlite_users_and_tracking_start_survive_reopening(tmp_path) -> None:
    db_path = tmp_path / "stats.sqlite3"
    first_seen = NOW - timedelta(days=10)
    first = SQLiteUserStatsStore(db_path)
    asyncio.run(first.record_user(1, first_seen))

    reopened = SQLiteUserStatsStore(db_path)
    asyncio.run(reopened.record_user(2, NOW))
    stats = asyncio.run(reopened.get_stats(NOW))

    assert (stats.total_users, stats.active_now, stats.active_24h) == (2, 1, 1)
    assert stats.tracking_since == first_seen
    assert stats.is_persistent is True


def test_sqlite_preserves_legacy_users_and_exact_window_boundaries(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    earliest = NOW - timedelta(days=10)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE bot_users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_active_date TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO bot_users VALUES (?, ?, ?, ?)",
            [
                (1, earliest.isoformat(), (NOW - timedelta(minutes=5)).isoformat(), "2026-11-16"),
                (2, earliest.isoformat(), (NOW - timedelta(days=1)).isoformat(), "2026-11-15"),
                (3, earliest.isoformat(), (NOW - timedelta(days=2)).isoformat(), "2026-11-14"),
            ],
        )

    store = SQLiteUserStatsStore(db_path)
    stats = asyncio.run(store.get_stats(NOW))
    assert (stats.total_users, stats.active_now, stats.active_24h) == (3, 1, 2)
    assert stats.tracking_since == earliest

    asyncio.run(store.record_user(1, NOW))
    stats = asyncio.run(SQLiteUserStatsStore(db_path).get_stats(NOW))
    assert (stats.total_users, stats.active_now, stats.active_24h) == (3, 1, 2)


def test_sqlite_concurrent_requests_preserve_unique_users_and_latest_activity(tmp_path) -> None:
    store = SQLiteUserStatsStore(tmp_path / "concurrent.sqlite3")

    async def scenario():
        await asyncio.gather(
            store.record_user(1, NOW),
            store.record_user(1, NOW - timedelta(days=2)),
            store.record_user(1, NOW - timedelta(days=1)),
            *(store.record_user(user_id, NOW) for user_id in range(2, 7)),
        )
        return await store.get_stats(NOW)

    stats = asyncio.run(scenario())
    assert (stats.total_users, stats.active_now, stats.active_24h) == (6, 6, 6)


class FakeResponse:
    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield json.dumps(self.payload).encode()

    def close(self):
        pass


def test_redis_record_is_atomic_and_preserves_existing_lifetime_key(monkeypatch) -> None:
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"result": 1})

    monkeypatch.setattr("app.stats.requests.post", fake_post)
    store = UpstashRedisUserStatsStore("https://redis.example.invalid/", "fake-secret", "old_prefix")
    asyncio.run(store.record_user(123, NOW))

    url, kwargs = calls[0]
    assert url == "https://redis.example.invalid"
    assert kwargs["json"] == [
        "EVAL", RECORD_ACTIVITY_SCRIPT, 3,
        "old_prefix:users", "old_prefix:last_seen", "old_prefix:activity_tracking_since",
        "123", f"{NOW.timestamp():.6f}",
    ]
    assert kwargs["timeout"] == REDIS_REQUEST_TIMEOUT
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["headers"] == {"Authorization": "Bearer fake-secret"}
    assert "'GT'" in RECORD_ACTIVITY_SCRIPT
    assert "EXPIRE" not in RECORD_ACTIVITY_SCRIPT.upper()
    assert "DEL" not in RECORD_ACTIVITY_SCRIPT.upper()


def test_redis_reads_a_consistent_snapshot_and_keeps_legacy_lifetime_total(monkeypatch) -> None:
    calls = []
    started = NOW - timedelta(hours=2)

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        # Historical users are still included even though the new index is young.
        return FakeResponse({"result": [500, 2, 10, str(started.timestamp()), str(NOW.timestamp())]})

    monkeypatch.setattr("app.stats.requests.post", fake_post)
    store = UpstashRedisUserStatsStore("https://redis.example.invalid", "fake-secret", "prefix")
    stats = asyncio.run(store.get_stats(NOW))

    assert (stats.total_users, stats.active_now, stats.active_24h) == (500, 2, 10)
    assert stats.tracking_since == started
    assert stats.is_persistent is True
    assert stats.observed_at == NOW
    assert calls[0] == [
        "EVAL", READ_ACTIVITY_SCRIPT, 3,
        "prefix:users", "prefix:last_seen", "prefix:activity_tracking_since",
        f"{NOW.timestamp():.6f}",
        "300", "86400",
    ]
    assert "'SCARD'" in READ_ACTIVITY_SCRIPT
    assert "'ZCOUNT'" in READ_ACTIVITY_SCRIPT
    assert "ZREMRANGEBYSCORE" not in READ_ACTIVITY_SCRIPT
    assert "EXPIRE" not in READ_ACTIVITY_SCRIPT


def test_redis_uses_server_clock_for_current_activity_and_returns_actual_snapshot_time(monkeypatch) -> None:
    calls = []
    server_now = NOW + timedelta(seconds=2)

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        if kwargs["json"][1] == RECORD_ACTIVITY_SCRIPT:
            return FakeResponse({"result": 1})
        return FakeResponse({"result": [1, 1, 1, str(NOW.timestamp()), str(server_now.timestamp())]})

    monkeypatch.setattr("app.stats.requests.post", fake_post)
    store = UpstashRedisUserStatsStore("https://redis.example.invalid", "fake-secret", "prefix")

    async def scenario():
        await store.record_user(123)
        return await store.get_stats()

    stats = asyncio.run(scenario())
    assert calls[0][-2:] == ["123", ""]
    assert calls[1][-3:] == ["", "300", "86400"]
    assert "redis.call('TIME')" in RECORD_ACTIVITY_SCRIPT
    assert "redis.call('TIME')" in READ_ACTIVITY_SCRIPT
    assert stats.observed_at == server_now


@pytest.mark.parametrize("payload", [[], {}, {"error": "private server detail fake-secret"}])
def test_redis_rejects_invalid_or_failed_responses_without_exposing_server_details(monkeypatch, payload) -> None:
    monkeypatch.setattr("app.stats.requests.post", lambda *args, **kwargs: FakeResponse(payload))
    store = UpstashRedisUserStatsStore("https://redis.example.invalid", "fake-secret", "prefix")

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(store.get_stats(NOW))
    assert "fake-secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    "result",
    [
        None, [], [1, 0, 0],
        [1, 2, 1, 0, 0],
        [2, 1, -1, 0, 0],
        [2, 1, 2, "invalid", 0],
        [2, 1, 2, 0, "invalid"],
        [2, 1.5, 2, 0, 0],
        [2, True, 2, 0, 0],
    ],
)
def test_redis_rejects_malformed_or_inconsistent_counters(monkeypatch, result) -> None:
    monkeypatch.setattr("app.stats.requests.post", lambda *args, **kwargs: FakeResponse({"result": result}))
    store = UpstashRedisUserStatsStore("https://redis.example.invalid", "fake-secret", "prefix")

    with pytest.raises(RuntimeError):
        asyncio.run(store.get_stats(NOW))


def test_redis_rejects_a_missing_record_acknowledgement(monkeypatch) -> None:
    monkeypatch.setattr("app.stats.requests.post", lambda *args, **kwargs: FakeResponse({"result": None}))
    store = UpstashRedisUserStatsStore("https://redis.example.invalid", "fake-secret", "prefix")
    with pytest.raises(RuntimeError, match="record result"):
        asyncio.run(store.record_user(123, NOW))


def test_redis_http_errors_are_not_reported_as_zero_users(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.stats.requests.post",
        lambda *args, **kwargs: FakeResponse(None, requests.HTTPError("503 Service Unavailable")),
    )
    store = UpstashRedisUserStatsStore("https://redis.example.invalid", "fake-secret", "prefix")

    with pytest.raises(RuntimeError, match="Redis REST request failed"):
        asyncio.run(store.get_stats(NOW))


def test_store_selection_uses_persistent_redis_on_vercel_and_marks_fallback(monkeypatch, tmp_path) -> None:
    settings = SimpleNamespace(
        upstash_redis_rest_url="https://redis.example.invalid",
        upstash_redis_rest_token="fake-secret",
        user_stats_key_prefix="prefix",
        user_stats_db_path=str(tmp_path / "stats.sqlite3"),
    )
    monkeypatch.setenv("VERCEL", "1")
    assert isinstance(build_user_stats_store(settings), UpstashRedisUserStatsStore)

    settings.upstash_redis_rest_token = ""
    settings.upstash_redis_rest_url = ""
    temporary = build_user_stats_store(settings)
    assert isinstance(temporary, MemoryUserStatsStore)
    assert temporary.is_persistent is False

    monkeypatch.delenv("VERCEL")
    assert isinstance(build_user_stats_store(settings), SQLiteUserStatsStore)


def test_parse_admin_user_ids_accepts_commas_and_semicolons() -> None:
    assert parse_admin_user_ids("123, 456;789") == frozenset({123, 456, 789})
