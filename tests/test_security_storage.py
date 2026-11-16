from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest
import requests

from app import stats


NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
SQL_PAYLOAD = "1); DROP TABLE bot_users; --"
LUA_PAYLOAD = "x'); redis.call('FLUSHALL'); --"
FAKE_SECRET = "FAKE_PRIVATE_STORAGE_TOKEN"


class Response:
    def __init__(self, payload=None, *, status=200, chunks=None, headers=None, http_error=None):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks if chunks is not None else [json.dumps(payload).encode()]
        self.http_error = http_error
        self.closed = False

    def raise_for_status(self):
        if self.http_error:
            raise self.http_error

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


def redis_store(**kwargs):
    return stats.UpstashRedisUserStatsStore(
        "https://redis.example.invalid", FAKE_SECRET, kwargs.pop("key_prefix", "prefix"), **kwargs
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite", "redis"])
@pytest.mark.parametrize(
    "user_id", [0, -1, True, False, 1.0, "1", None, 1 << 52, SQL_PAYLOAD, LUA_PAYLOAD, b"123"]
)
def test_invalid_user_ids_are_rejected_before_storage_or_network(monkeypatch, tmp_path, backend, user_id):
    monkeypatch.setattr(stats.requests, "post", lambda *a, **k: pytest.fail("Invalid input must not reach Redis"))
    path = tmp_path / "stats.sqlite3"
    store = {
        "memory": lambda: stats.MemoryUserStatsStore(),
        "sqlite": lambda: stats.SQLiteUserStatsStore(path),
        "redis": redis_store,
    }[backend]()

    with pytest.raises(ValueError, match="Telegram user ID"):
        asyncio.run(store.record_user(user_id, NOW))

    assert not path.exists()


@pytest.mark.parametrize("user_id", [1, (1 << 52) - 1])
def test_supported_telegram_user_id_boundaries_are_preserved(tmp_path, user_id):
    store = stats.SQLiteUserStatsStore(tmp_path / "stats.sqlite3")
    asyncio.run(store.record_user(user_id, NOW))
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT user_id FROM bot_users").fetchone() == (user_id,)


def test_sql_injection_payload_cannot_change_existing_data(tmp_path):
    store = stats.SQLiteUserStatsStore(tmp_path / "stats.sqlite3")
    asyncio.run(store.record_user(42, NOW))

    with pytest.raises(ValueError):
        asyncio.run(store.record_user(SQL_PAYLOAD, NOW))
    # The sync layer still uses bound parameters even if it is called directly;
    # SQLite rejects the type instead of parsing the value as another statement.
    with pytest.raises(sqlite3.IntegrityError):
        store._record_user_sync(SQL_PAYLOAD, NOW)

    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT user_id FROM bot_users").fetchall() == [(42,)]
        assert connection.execute("SELECT COUNT(*) FROM bot_stats_metadata").fetchone()[0] == 2


def test_redis_keys_are_arguments_and_cannot_modify_the_lua_program(monkeypatch):
    calls = []
    response = Response({"result": 1})
    monkeypatch.setattr(stats.requests, "post", lambda url, **kwargs: calls.append((url, kwargs)) or response)
    store = redis_store(key_prefix=LUA_PAYLOAD)

    asyncio.run(store.record_user(42, NOW))

    command = calls[0][1]["json"]
    assert command[:3] == ["EVAL", stats.RECORD_ACTIVITY_SCRIPT, 3]
    assert command[3:6] == [f"{LUA_PAYLOAD}:users", f"{LUA_PAYLOAD}:last_seen", f"{LUA_PAYLOAD}:activity_tracking_since"]
    assert command[6] == "42"
    assert LUA_PAYLOAD not in command[1]
    assert response.closed


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://redis.example.invalid", "https://user:password@redis.example.invalid",
        "https://redis.example.invalid/?token=secret", "https://redis.example.invalid/#token",
        "https://redis.example.invalid/extra/path", "https://redis.example.invalid:8443",
        "https://127.0.0.1", "https://127.1", "https://0177.0.0.1", "https://127.0.0.1.", "https://10.1.2.3", "https://169.254.169.254", "https://[::1]",
        "https://localhost", "https://host.local", "https://host.local.", "https://localhost.", "https://x.localhost",
        " https://redis.example.invalid", "https://redis.example.invalid\n", "https://redis.example.invalid\\@attacker.invalid",
    ],
)
def test_unsafe_or_ambiguous_redis_endpoint_configuration_is_rejected(endpoint):
    with pytest.raises(ValueError, match="Redis REST endpoint") as error:
        stats.UpstashRedisUserStatsStore(endpoint, FAKE_SECRET, "prefix")
    assert endpoint not in str(error.value)
    assert "password" not in str(error.value)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_storage_credentials_are_never_followed_to_a_redirect(monkeypatch, status):
    response = Response(status=status, headers={"Location": "https://attacker.invalid/steal"})
    calls = []
    monkeypatch.setattr(stats.requests, "post", lambda url, **kwargs: calls.append((url, kwargs)) or response)

    with pytest.raises(RuntimeError, match="redirects are not permitted"):
        asyncio.run(redis_store().get_stats(NOW))

    assert len(calls) == 1
    assert calls[0][0] == "https://redis.example.invalid"
    assert calls[0][1]["allow_redirects"] is False
    assert response.closed


@pytest.mark.parametrize(
    "response",
    [
        Response({"result": 1}, headers={"Content-Length": str(stats.MAX_REDIS_RESPONSE_BYTES + 1)}),
        Response(chunks=[b"a" * 4096] * (stats.MAX_REDIS_RESPONSE_BYTES // 4096 + 1)),
        Response(chunks=[b"{" + FAKE_SECRET.encode()]),
        Response(chunks=[b"\xff\xfe"]),
        Response(chunks=[b"[" * 2000 + b"0" + b"]" * 2000]),
        Response({"error": FAKE_SECRET}),
    ],
)
def test_oversized_or_malformed_redis_responses_fail_safely_and_close(monkeypatch, response):
    monkeypatch.setattr(stats.requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError) as error:
        asyncio.run(redis_store().get_stats(NOW))

    assert FAKE_SECRET not in str(error.value)
    assert response.closed


@pytest.mark.parametrize("kind", [requests.ConnectionError, requests.Timeout, requests.HTTPError])
def test_redis_transport_errors_do_not_expose_endpoint_or_credentials(monkeypatch, kind):
    def fail(*args, **kwargs):
        raise kind("https://redis.example.invalid " + FAKE_SECRET)

    monkeypatch.setattr(stats.requests, "post", fail)

    with pytest.raises(RuntimeError, match="Redis REST request failed") as error:
        asyncio.run(redis_store().get_stats(NOW))

    assert FAKE_SECRET not in str(error.value)
    assert "redis.example.invalid" not in str(error.value)
    assert error.value.__suppress_context__


def test_http_status_errors_are_sanitized_and_response_closed(monkeypatch):
    response = Response(http_error=requests.HTTPError("https://redis.example.invalid/ " + FAKE_SECRET))
    monkeypatch.setattr(stats.requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(redis_store().get_stats(NOW))
    assert response.closed
    assert FAKE_SECRET not in str(error.value)
