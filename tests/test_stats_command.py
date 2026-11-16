from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot
from app.stats import UserStats


OBSERVED_AT = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)
SECRET_ERROR = "FAKE_PRIVATE_TOKEN_123:storage authentication failed"


def make_update(kind="message", *, user_id=42, is_bot=False):
    user = SimpleNamespace(id=user_id, is_bot=is_bot) if user_id is not None else None
    message = SimpleNamespace(reply_text=AsyncMock(), from_user=user, text="/stats")
    update = SimpleNamespace(
        effective_user=user,
        effective_message=message,
        message=None,
        callback_query=None,
        edited_message=None,
        edited_channel_post=None,
        channel_post=None,
        my_chat_member=None,
    )
    if kind == "callback_query":
        update.callback_query = SimpleNamespace(from_user=user, message=message)
    else:
        setattr(update, kind, message)
    return update


def make_context(
    *,
    admin_ids=frozenset(),
    timezone_name="UTC",
    persistent=True,
    tracking_since=OBSERVED_AT - timedelta(days=30),
):
    snapshot = UserStats(
        total_users=137,
        active_now=9,
        active_24h=42,
        storage_name="sqlite" if persistent else "memory",
        is_persistent=persistent,
        observed_at=OBSERVED_AT,
        tracking_since=tracking_since,
    )
    store = SimpleNamespace(
        record_user=AsyncMock(),
        get_stats=AsyncMock(return_value=snapshot),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "settings": SimpleNamespace(admin_user_ids=admin_ids, timezone=timezone_name),
                "user_stats_store": store,
            }
        )
    )
    return context, store


def reply_text(update):
    update.effective_message.reply_text.assert_awaited_once()
    args = update.effective_message.reply_text.await_args
    return html.unescape(re.sub(r"<[^>]+>", "", args.args[0]))


def assert_optional_utc_timestamp(mock, *, positional_index, keyword):
    args = mock.await_args
    observed_at = args.kwargs.get(keyword)
    if len(args.args) > positional_index:
        observed_at = args.args[positional_index]
    if observed_at is not None:
        assert isinstance(observed_at, datetime)
        assert observed_at.tzinfo is not None
        assert observed_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("timezone_name", ["Asia/Krasnoyarsk", "Does/NotExist"])
def test_stats_command_shows_five_minutes_rolling_day_and_total(timezone_name):
    update = make_update()
    context, store = make_context(timezone_name=timezone_name)

    asyncio.run(bot.stats_command(update, context))

    text = reply_text(update)
    assert re.search(r"Сейчас.*5\s*минут.*:\s*9\b", text)
    assert re.search(r"За последние 24 часа:\s*42\b", text)
    assert re.search(r"Всего пользователей:\s*137\b", text)
    assert "15 минут" not in text
    assert "Данные на:" in text
    store.get_stats.assert_awaited_once()
    assert_optional_utc_timestamp(store.get_stats, positional_index=0, keyword="now")


def test_stats_command_warns_when_statistics_are_in_memory():
    update = make_update()
    context, _ = make_context(persistent=False)

    asyncio.run(bot.stats_command(update, context))

    text = reply_text(update).casefold()
    assert "временн" in text
    assert "памят" in text
    assert "перезапуск" in text
    assert "сбрас" in text or "сброс" in text


@pytest.mark.parametrize("tracked_hours,needs_warning", [(6, True), (24, False), (30, False)])
def test_stats_command_explains_incomplete_rolling_day(tracked_hours, needs_warning):
    update = make_update()
    context, _ = make_context(tracking_since=OBSERVED_AT - timedelta(hours=tracked_hours))

    asyncio.run(bot.stats_command(update, context))

    text = reply_text(update).casefold()
    assert ("полные 24 часа ещё накапливаются" in text) is needs_warning
    assert "учёт активности с:" in text


def test_stats_command_handles_unknown_history_start():
    update = make_update()
    context, _ = make_context(tracking_since=None)

    asyncio.run(bot.stats_command(update, context))

    text = reply_text(update).casefold()
    assert "137" in text
    assert "учёт активности с:" not in text


@pytest.mark.parametrize("user_id", [7, None])
def test_stats_command_denies_non_admin_without_reading_storage(user_id):
    update = make_update(user_id=user_id)
    context, store = make_context(admin_ids=frozenset({42}))

    asyncio.run(bot.stats_command(update, context))

    assert "только администратору" in reply_text(update)
    store.get_stats.assert_not_awaited()


@pytest.mark.parametrize("admin_ids", [frozenset(), frozenset({42})])
def test_stats_command_preserves_open_or_admin_access(admin_ids):
    update = make_update(user_id=42)
    context, store = make_context(admin_ids=admin_ids)

    asyncio.run(bot.stats_command(update, context))

    store.get_stats.assert_awaited_once()
    assert "137" in reply_text(update)


def test_stats_storage_failure_returns_explanation_without_fake_counts_or_secrets(caplog):
    update = make_update()
    context, store = make_context()
    store.get_stats.side_effect = RuntimeError(SECRET_ERROR)

    asyncio.run(bot.stats_command(update, context))

    text = reply_text(update)
    assert "временно недоступна" in text.casefold()
    assert "попробуйте чуть позже" in text.casefold()
    assert not re.search(r"\b0\b", text)
    assert SECRET_ERROR not in text
    assert SECRET_ERROR not in caplog.text


@pytest.mark.parametrize("kind", ["message", "callback_query"])
def test_track_usage_counts_only_human_messages_and_callbacks(kind):
    update = make_update(kind)
    context, store = make_context()

    asyncio.run(bot.track_usage(update, context))

    store.record_user.assert_awaited_once()
    assert store.record_user.await_args.args[0] == 42
    assert_optional_utc_timestamp(store.record_user, positional_index=1, keyword="observed_at")


@pytest.mark.parametrize(
    "kind,is_bot,user_id",
    [
        ("message", True, 42),
        ("callback_query", True, 42),
        ("message", False, None),
        ("edited_message", False, 42),
        ("edited_channel_post", False, 42),
        ("my_chat_member", False, 42),
        ("channel_post", False, 42),
    ],
)
def test_track_usage_ignores_bots_edits_and_status_updates(kind, is_bot, user_id):
    update = make_update(kind, is_bot=is_bot, user_id=user_id)
    context, store = make_context()

    asyncio.run(bot.track_usage(update, context))

    store.record_user.assert_not_awaited()


def test_tracking_failure_does_not_break_command_or_expose_secret(caplog):
    update = make_update()
    context, store = make_context()
    store.record_user.side_effect = RuntimeError(SECRET_ERROR)

    async def handle_update():
        await bot.track_usage(update, context)
        await bot.stats_command(update, context)

    asyncio.run(handle_update())

    store.record_user.assert_awaited_once()
    assert "137" in reply_text(update)
    assert SECRET_ERROR not in caplog.text
    assert SECRET_ERROR not in reply_text(update)


def test_tracking_timeout_does_not_block_the_command(monkeypatch):
    update = make_update()
    context, store = make_context()
    cancelled = []

    async def slow_record(*args, **kwargs):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)

    store.record_user.side_effect = slow_record
    monkeypatch.setattr(bot, "STATS_RECORD_TIMEOUT_SECONDS", 0.01)

    async def handle_update():
        await bot.track_usage(update, context)
        await bot.stats_command(update, context)

    async def bounded_scenario():
        await asyncio.wait_for(handle_update(), timeout=0.5)

    asyncio.run(bounded_scenario())

    assert cancelled == [True]
    store.get_stats.assert_awaited_once()
    assert "137" in reply_text(update)


def test_stats_read_timeout_returns_a_useful_response(monkeypatch):
    update = make_update()
    context, store = make_context()
    cancelled = []

    async def slow_read(*args, **kwargs):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)

    store.get_stats.side_effect = slow_read
    monkeypatch.setattr(bot, "STATS_READ_TIMEOUT_SECONDS", 0.01)

    async def bounded_scenario():
        await asyncio.wait_for(bot.stats_command(update, context), timeout=0.5)

    asyncio.run(bounded_scenario())

    assert cancelled == [True]
    text = reply_text(update).casefold()
    assert "временно недоступна" in text
    assert "попробуйте чуть позже" in text
    assert "137" not in text
