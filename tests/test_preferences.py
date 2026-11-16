from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from app import preferences, stats
from app.preferences import (
    MemoryUserPreferencesStore,
    SavedGroup,
    SQLiteUserPreferencesStore,
    UpstashRedisUserPreferencesStore,
    build_user_preferences_store,
)


GROUP = SavedGroup("ИСт-241.2", "spo")
OTHER = SavedGroup("МАб-241.2.2", "vo")
FAKE_SECRET = "FAKE_PRIVATE_PREFERENCES_TOKEN"


class Response:
    def __init__(self, result=None, *, raw=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = raw if raw is not None else json.dumps({"result": result}).encode()
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.raw), chunk_size):
            yield self.raw[offset:offset + chunk_size]

    def close(self):
        self.closed = True


def redis_store(prefix="bot"):
    return UpstashRedisUserPreferencesStore("https://redis.example.invalid", FAKE_SECRET, prefix)


@pytest.fixture(params=["memory", "sqlite"])
def local_store(request, tmp_path):
    if request.param == "memory":
        return MemoryUserPreferencesStore()
    return SQLiteUserPreferencesStore(tmp_path / "stats.sqlite3")


def test_saved_group_replacement_and_removal_are_private_per_user(local_store):
    async def scenario():
        assert await local_store.get_group(1) is None
        await local_store.set_group(1, GROUP)
        await local_store.set_group(2, OTHER)
        assert await local_store.get_group(1) == GROUP
        assert await local_store.get_group(2) == OTHER
        await local_store.set_group(1, OTHER)
        assert await local_store.get_group(1) == OTHER
        await local_store.clear_group(1)
        await local_store.clear_group(1)
        assert await local_store.get_group(1) is None
        assert await local_store.get_group(2) == OTHER
    asyncio.run(scenario())
    assert local_store.is_persistent == (local_store.storage_name == "sqlite")


def test_sqlite_saved_group_survives_reopen_without_changing_activity(tmp_path):
    path = tmp_path / "stats.sqlite3"
    activity = stats.SQLiteUserStatsStore(path)
    store = SQLiteUserPreferencesStore(path)
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)

    async def scenario():
        await activity.record_user(42, now)
        before = await activity.get_stats(now)
        await store.set_group(42, GROUP)
        reopened = SQLiteUserPreferencesStore(path)
        assert await reopened.get_group(42) == GROUP
        await reopened.set_group(43, OTHER)
        await reopened.clear_group(43)
        assert await activity.get_stats(now) == before
        assert await reopened.get_group(42) == GROUP
    asyncio.run(scenario())


@pytest.mark.parametrize("user_id", [0, -1, True, False, "42", 1.0, None, 1 << 52, "42); DROP TABLE bot_users; --"])
@pytest.mark.parametrize("backend", ["memory", "sqlite", "redis"])
def test_invalid_identity_is_rejected_before_any_storage(monkeypatch, tmp_path, user_id, backend):
    path = tmp_path / "private.sqlite3"
    monkeypatch.setattr(stats.requests, "post", lambda *_a, **_k: pytest.fail("Invalid ID reached Redis"))
    store = {
        "memory": MemoryUserPreferencesStore,
        "sqlite": lambda: SQLiteUserPreferencesStore(path),
        "redis": redis_store,
    }[backend]()

    async def scenario():
        with pytest.raises(ValueError, match="Telegram user ID"):
            await store.get_group(user_id)
        with pytest.raises(ValueError, match="Telegram user ID"):
            await store.set_group(user_id, GROUP)
        with pytest.raises(ValueError, match="Telegram user ID"):
            await store.clear_group(user_id)
    asyncio.run(scenario())
    assert not path.exists()


@pytest.mark.parametrize(
    "group,category",
    [("", "spo"), (None, "spo"), ("A" * 81, "vo"), ("ИСт-241.2\n", "spo"), ("И Ст-241.2", "spo"),
     ("ИСт-241.2", "private"), ("ИСт-241.2", None), ("1); DROP TABLE bot_users; --", "vo"),
     ("<a href='x'>ИСт-241.2</a>", "vo"), ("x'); redis.call('FLUSHALL'); --", "vo")],
)
def test_saved_group_schema_rejects_malformed_or_injection_values(group, category):
    with pytest.raises(ValueError, match="supported group code"):
        SavedGroup(group, category)


def test_storage_validates_group_type_before_opening_database(tmp_path):
    path = tmp_path / "stats.sqlite3"
    store = SQLiteUserPreferencesStore(path)
    with pytest.raises(ValueError, match="SavedGroup"):
        asyncio.run(store.set_group(42, {"group": GROUP.group, "category": GROUP.category}))
    assert not path.exists()


def test_sqlite_corrupted_preference_fails_without_returning_untrusted_data(tmp_path):
    store = SQLiteUserPreferencesStore(tmp_path / "stats.sqlite3")
    asyncio.run(store.set_group(42, GROUP))
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE user_group_preferences SET group_code = ? WHERE user_id = ?", ("FAKE_PRIVATE_INVALID_DATA", 42))
    with pytest.raises(RuntimeError, match="Saved group data is invalid") as error:
        asyncio.run(store.get_group(42))
    assert "FAKE_PRIVATE_INVALID_DATA" not in str(error.value)


def test_redis_roundtrip_survives_new_store_and_keeps_commands_parameterized(monkeypatch):
    records = {}
    calls = []
    responses = []

    def post(url, **kwargs):
        command = kwargs["json"]
        calls.append((url, kwargs))
        if command[0] == "GET":
            result = records.get(command[1])
        elif command[0] == "SET":
            assert len(command) == 3  # No expiry or concatenated Redis program.
            records[command[1]] = command[2]
            result = "OK"
        elif command[0] == "DEL":
            result = int(records.pop(command[1], None) is not None)
        else:
            pytest.fail("Unsupported Redis command")
        response = Response(result)
        responses.append(response)
        return response

    monkeypatch.setattr(stats.requests, "post", post)
    prefix = "x'); redis.call('FLUSHALL'); --"
    first = redis_store(prefix)
    second = redis_store(prefix)

    async def scenario():
        assert await first.get_group(42) is None
        await first.set_group(42, GROUP)
        await first.set_group(43, OTHER)
        assert await second.get_group(42) == GROUP
        await second.clear_group(42)
        assert await first.get_group(42) is None
        assert await first.get_group(43) == OTHER
    asyncio.run(scenario())

    assert set(records) == {prefix + ":preferences:43"}
    assert all(kwargs["allow_redirects"] is False and kwargs["stream"] is True for _, kwargs in calls)
    assert all(kwargs["timeout"] == stats.REDIS_REQUEST_TIMEOUT for _, kwargs in calls)
    assert all(kwargs["headers"] == {"Authorization": f"Bearer {FAKE_SECRET}"} for _, kwargs in calls)
    assert all(response.closed for response in responses)
    assert all(kwargs["json"][0] in {"GET", "SET", "DEL"} for _, kwargs in calls)


@pytest.mark.parametrize(
    "value",
    [False, 1, [], "not json", "[" * 2000, "x" * (preferences.MAX_SAVED_GROUP_BYTES + 1),
     json.dumps({"version": True, "group": GROUP.group, "category": GROUP.category}),
     json.dumps({"version": 2, "group": GROUP.group, "category": GROUP.category}),
     json.dumps({"version": 1, "group": GROUP.group}),
     json.dumps({"version": 1, "group": GROUP.group, "category": "unknown"}),
     json.dumps({"version": 1, "group": "FAKE_PRIVATE_INVALID_DATA", "category": "vo"})],
)
def test_redis_malformed_saved_record_is_rejected_safely(monkeypatch, value):
    response = Response(value)
    monkeypatch.setattr(stats.requests, "post", lambda *_a, **_k: response)
    with pytest.raises(RuntimeError, match="Saved group data is invalid") as error:
        asyncio.run(redis_store().get_group(42))
    assert "FAKE_PRIVATE_INVALID_DATA" not in str(error.value)
    assert response.closed


@pytest.mark.parametrize("result", [None, True, 1, "ERROR", {"private": FAKE_SECRET}])
def test_redis_failed_write_does_not_claim_saved(monkeypatch, result):
    monkeypatch.setattr(stats.requests, "post", lambda *_a, **_k: Response(result))
    with pytest.raises(RuntimeError, match="saved group write result") as error:
        asyncio.run(redis_store().set_group(42, GROUP))
    assert FAKE_SECRET not in str(error.value)


@pytest.mark.parametrize("result", [None, True, "1", -1, 2, {"private": FAKE_SECRET}])
def test_redis_invalid_delete_result_is_rejected(monkeypatch, result):
    monkeypatch.setattr(stats.requests, "post", lambda *_a, **_k: Response(result))
    with pytest.raises(RuntimeError, match="saved group removal result"):
        asyncio.run(redis_store().clear_group(42))


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_preferences_share_no_redirect_credential_protection(monkeypatch, status):
    response = Response(status=status, headers={"Location": "https://attacker.invalid"})
    monkeypatch.setattr(stats.requests, "post", lambda *_a, **_k: response)
    with pytest.raises(RuntimeError, match="redirects are not permitted"):
        asyncio.run(redis_store().get_group(42))
    assert response.closed


def test_preferences_share_transport_bounds_and_safe_errors(monkeypatch):
    response = Response(raw=b"x" * (stats.MAX_REDIS_RESPONSE_BYTES + 1))
    monkeypatch.setattr(stats.requests, "post", lambda *_a, **_k: response)
    with pytest.raises(RuntimeError, match="exceeds the allowed size"):
        asyncio.run(redis_store().get_group(42))
    assert response.closed

    def fail(*_a, **_k):
        raise requests.ConnectionError("https://redis.example.invalid " + FAKE_SECRET)
    monkeypatch.setattr(stats.requests, "post", fail)
    with pytest.raises(RuntimeError, match="Redis REST request failed") as error:
        asyncio.run(redis_store().set_group(42, GROUP))
    assert FAKE_SECRET not in str(error.value)
    assert error.value.__suppress_context__


def test_backend_selection_uses_existing_settings_without_new_credentials(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        upstash_redis_rest_url="https://redis.example.invalid", upstash_redis_rest_token=FAKE_SECRET,
        user_stats_key_prefix="existing", user_stats_db_path=str(tmp_path / "stats.sqlite3"),
    )
    monkeypatch.setenv("VERCEL", "1")
    assert isinstance(build_user_preferences_store(settings), UpstashRedisUserPreferencesStore)
    settings.upstash_redis_rest_url = settings.upstash_redis_rest_token = ""
    assert isinstance(build_user_preferences_store(settings), MemoryUserPreferencesStore)
    monkeypatch.delenv("VERCEL")
    local = build_user_preferences_store(settings)
    assert isinstance(local, SQLiteUserPreferencesStore)
    assert local.db_path == tmp_path / "stats.sqlite3"
