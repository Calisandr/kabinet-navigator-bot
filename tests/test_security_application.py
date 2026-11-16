from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationHandlerStop

from app import bot, schedule_image
from app.abuse import RequestBudget
from app.formatting import format_day_schedule, format_teacher_schedule, format_teachers_list
from app.schedule import Schedule, ScheduleEntry
from app.security_logging import install_secret_redaction


def test_request_budget_is_per_user_refills_and_bounds_memory():
    clock = [0.0]
    budget = RequestBudget(capacity=2, refill_per_second=1, max_users=2, clock=lambda: clock[0])
    assert budget.allow(1) and budget.allow(1)
    assert not budget.allow(1)
    assert budget.allow(2)
    clock[0] += 1
    assert budget.allow(1)
    assert not budget.allow(1)
    assert budget.allow(3)
    assert len(budget._users) == 2


def test_rate_limit_warning_cannot_amplify_every_flooding_message():
    clock = [0.0]
    budget = RequestBudget(capacity=1, clock=lambda: clock[0])
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42, is_bot=False), callback_query=None,
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"request_budget": budget}))

    async def scenario():
        await bot.guard_requests(update, context)
        for _ in range(10):
            with pytest.raises(ApplicationHandlerStop):
                await bot.guard_requests(update, context)
        update.effective_message.reply_text.assert_awaited_once()
        assert "Подождите" in update.effective_message.reply_text.await_args.args[0]
        clock[0] += 1
        await bot.guard_requests(update, context)

    asyncio.run(scenario())


@pytest.mark.parametrize("is_bot", [False, True])
def test_guard_allows_normal_humans_and_stops_bot_updates(is_bot):
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42, is_bot=is_bot))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"request_budget": RequestBudget()}))
    if is_bot:
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(bot.guard_requests(update, context))
    else:
        asyncio.run(bot.guard_requests(update, context))


def test_force_refresh_cannot_bypass_cache_repeatedly():
    calls = []
    schedule = Schedule(entries=(), loaded_at=datetime.now(timezone.utc))
    repository = SimpleNamespace(load=lambda: calls.append(True) or schedule)
    cache = bot.ScheduleCache(repository, ttl_seconds=900)

    async def scenario():
        assert await cache.get(force=True) is schedule
        assert await cache.get(force=True) is schedule
        assert len(calls) == 1
        cache._loaded_monotonic -= 21
        assert await cache.get(force=True) is schedule
        assert len(calls) == 2

    asyncio.run(scenario())


def test_anonymous_group_admin_commands_have_separate_group_budgets():
    budget = RequestBudget(capacity=1)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"request_budget": budget}))

    def anonymous_update(chat_id):
        group = Chat(chat_id, "supergroup", title="Students")
        return Update(1, message=Message(
            1, datetime.now(timezone.utc), group,
            from_user=User(1087968824, "Anonymous admin", True),
            sender_chat=group, text="/today",
        ))

    async def scenario():
        await bot.guard_requests(anonymous_update(-1001234567890), context)
        await bot.guard_requests(anonymous_update(-1001234567891), context)
        assert len(budget._users) == 2
        assert not budget.allow(-1001234567890)

    asyncio.run(scenario())


def test_overlong_teacher_query_is_rejected_before_network_or_rendering():
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
    asyncio.run(bot.send_teacher_schedule(update, SimpleNamespace(), "A" * 4096))
    update.effective_message.reply_text.assert_awaited_once()
    assert "до 120" in update.effective_message.reply_text.await_args.args[0]


def test_telegram_html_from_query_and_sheet_values_is_escaped():
    injection = '<a href="https://evil.invalid">click</a>&'
    entry = ScheduleEntry(date(2026, 11, 16), injection, "1", injection, "test", 1)
    outputs = (
        format_day_schedule(entry.day, [entry]),
        format_teacher_schedule(injection, [], entry.day),
        format_teacher_schedule(injection, [entry], entry.day),
        format_teachers_list([injection]),
    )
    assert all('<a href="https://evil.invalid">' not in value for value in outputs)
    assert all("&lt;a href=" in value for value in outputs)


def test_log_redaction_covers_arguments_exceptions_and_known_credentials(monkeypatch, caplog):
    token = "123456789:FAKE_PRIVATE_TELEGRAM_TOKEN_123456789"
    redis_token = "FAKE_REDIS_PRIVATE_TOKEN"
    secret = "FAKE_WEBHOOK_SECRET"
    monkeypatch.setenv("KV_REST_API_TOKEN", redis_token)
    monkeypatch.setenv("WEBHOOK_SECRET", secret)
    install_secret_redaction()
    caplog.set_level(logging.ERROR)
    try:
        raise RuntimeError(f"https://api.telegram.org/bot{token}/getMe Bearer {redis_token}")
    except RuntimeError:
        logging.getLogger("security-test").exception("Request failed: %s %s", token, secret)
    assert token not in caplog.text
    assert redis_token not in caplog.text
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "RuntimeError" in caplog.text


def test_generic_error_handler_answers_without_disclosing_internal_error(monkeypatch, caplog):
    reply = AsyncMock()
    monkeypatch.setattr(Message, "reply_text", reply)
    update = Update(1, message=Message(
        1, datetime.now(timezone.utc), Chat(42, "private"),
        from_user=User(42, "Student", False), text="/today",
    ))
    context = SimpleNamespace(error=RuntimeError("FAKE_PRIVATE_CONFIGURATION_SECRET"))
    asyncio.run(bot.on_error(update, context))
    reply.assert_awaited_once()
    assert "Попробуйте" in reply.await_args.args[0]
    assert "FAKE_PRIVATE_CONFIGURATION_SECRET" not in caplog.text


def test_day_canvas_limit_is_checked_before_image_allocation(monkeypatch):
    monkeypatch.setattr(schedule_image.FontSet, "load", lambda: object())
    monkeypatch.setattr(schedule_image, "build_rows", lambda *_: [])
    monkeypatch.setattr(schedule_image, "calculate_height", lambda *_args, **_kwargs: 1_000_000)
    monkeypatch.setattr(schedule_image.Image, "new", lambda *_args, **_kwargs: pytest.fail("Oversized canvas allocated"))
    with pytest.raises(ValueError, match="canvas limit"):
        schedule_image.render_day_schedule_image(date(2026, 11, 16), [])


def test_overlong_sheet_value_cannot_reach_font_layout(monkeypatch):
    monkeypatch.setattr(schedule_image.FontSet, "load", lambda: pytest.fail("Unsafe value reached layout"))
    entry = ScheduleEntry(date(2026, 11, 16), "A" * 32767, "1", "219", "test", 1)
    with pytest.raises(ValueError, match="too large"):
        schedule_image.render_day_schedule_image(entry.day, [entry])
