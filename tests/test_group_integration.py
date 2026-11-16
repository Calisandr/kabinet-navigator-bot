from __future__ import annotations

import asyncio
from datetime import date
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from app import bot, group_bot
from app.config import Settings
from app.group_schedule import GroupNotFound, GroupScheduleService


def context():
    return SimpleNamespace(
        args=[],
        user_data={"awaiting_teacher": True},
        application=SimpleNamespace(bot_data={"settings": SimpleNamespace(timezone="UTC")}),
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ИСт-241.2", "group"),
        ("ТАт-231.2.1", "group"),
        ("МАб−241.2.2", "group"),
        ("Расписание группы", "prompt"),
        ("02.05.2026", "date"),
        ("Сегодня", "date"),
        ("Григорьев", "teacher"),
    ],
)
def test_plain_text_routes_groups_dates_and_teacher_names_to_the_right_handler(monkeypatch, text, expected):
    update = SimpleNamespace(effective_message=SimpleNamespace(text=text))
    ctx = context()
    handlers = {
        "group": AsyncMock(),
        "prompt": AsyncMock(),
        "date": AsyncMock(),
        "teacher": AsyncMock(),
    }
    monkeypatch.setattr(bot, "on_group_text", handlers["group"])
    monkeypatch.setattr(bot, "send_group_prompt", handlers["prompt"])
    monkeypatch.setattr(bot, "send_date_schedule", handlers["date"])
    monkeypatch.setattr(bot, "send_teacher_schedule", handlers["teacher"])
    monkeypatch.setattr(bot, "today_for", lambda _: date(2026, 11, 16))

    asyncio.run(bot.on_text(update, ctx))

    for name, handler in handlers.items():
        if name == expected:
            handler.assert_awaited_once()
        else:
            handler.assert_not_awaited()
    if expected != "date" or text != "Сегодня":
        assert "awaiting_teacher" not in ctx.user_data


@pytest.mark.parametrize("data", ["gl:vo:0", "gw:spo:20261116:12", "gg:part_time:20261114:ГМсозт-241.2"])
def test_group_callback_is_acknowledged_once_and_delegated_without_date_fallthrough(monkeypatch, data):
    query = SimpleNamespace(data=data, answer=AsyncMock())
    update = SimpleNamespace(callback_query=query)
    ctx = context()
    group_callback = AsyncMock(return_value=True)
    date_sender = AsyncMock()
    monkeypatch.setattr(bot, "handle_group_callback", group_callback)
    monkeypatch.setattr(bot, "send_date_schedule", date_sender)

    asyncio.run(bot.on_callback(update, ctx))

    query.answer.assert_awaited_once_with()
    group_callback.assert_awaited_once_with(update, ctx, data)
    date_sender.assert_not_awaited()


def test_existing_date_callback_keeps_working_after_group_routing(monkeypatch):
    query = SimpleNamespace(data="date:2026-11-16", answer=AsyncMock())
    update = SimpleNamespace(callback_query=query)
    ctx = context()
    monkeypatch.setattr(bot, "handle_group_callback", AsyncMock(return_value=False))
    sender = AsyncMock()
    monkeypatch.setattr(bot, "send_date_schedule", sender)

    asyncio.run(bot.on_callback(update, ctx))

    query.answer.assert_awaited_once_with()
    sender.assert_awaited_once_with(update, ctx, date(2026, 11, 16), from_callback=True)


def test_application_registers_both_group_commands_and_keeps_existing_handlers(monkeypatch):
    settings = Settings(
        telegram_bot_token="123456:FAKE_OFFLINE_TOKEN",
        google_sheet_id="offline-sheet",
        timezone="UTC",
        cache_minutes=15,
        webhook_secret="offline-secret",
        webhook_url="",
        admin_user_ids=frozenset(),
        user_stats_db_path="unused",
        upstash_redis_rest_url="",
        upstash_redis_rest_token="",
        user_stats_key_prefix="offline",
    )
    monkeypatch.setattr(bot, "build_user_stats_store", lambda _: object())

    application = bot.build_application(settings)

    command_handlers = {
        command: handler.callback
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert command_handlers["group"] is group_bot.group_command
    assert command_handlers["groups"] is group_bot.groups_command
    assert command_handlers["today"] is bot.today_command
    assert command_handlers["teacher"] is bot.teacher_command
    assert command_handlers["stats"] is bot.stats_command
    assert isinstance(application.bot_data["group_schedule_service"], GroupScheduleService)
    assert any(isinstance(handler, CallbackQueryHandler) and handler.callback is bot.on_callback for handler in application.handlers[0])
    assert any(isinstance(handler, MessageHandler) and handler.callback is bot.on_text for handler in application.handlers[0])


class CaptionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""

    def handle_data(self, text):
        self.text += text


def test_group_caption_with_long_official_pdf_url_stays_below_telegram_visible_limit(monkeypatch):
    from io import BytesIO
    from app.group_sources import GroupScheduleSource

    source = GroupScheduleSource(
        title="Официальное расписание",
        category="part_time",
        url="https://www.prk.kuzstu.ru/upload/" + "%D0%90" * 250 + ".pdf?a=1&b=2",
        start=date(2026, 11, 14),
        end=date(2026, 11, 14),
    )
    result = SimpleNamespace(group="ГМсозт-241.2.1.100", source=source, images=(BytesIO(b"image"),))
    message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
    update = SimpleNamespace(effective_message=message)
    ctx = context()
    ctx.args = [result.group]
    ctx.application.bot_data["group_schedule_service"] = SimpleNamespace(get_timetable=lambda *args, **kwargs: result)
    monkeypatch.setattr(group_bot, "local_today", lambda _: date(2026, 11, 16))

    asyncio.run(group_bot.group_command(update, ctx))

    message.reply_photo.assert_awaited_once()
    caption = message.reply_photo.await_args.kwargs["caption"]
    visible = CaptionText()
    visible.feed(caption)
    assert len(visible.text.encode("utf-16-le")) // 2 <= 1024
    assert "Оригинальное расписание (PDF)" in visible.text
    assert source.url.replace("&", "&amp;") in caption
    assert "Фрагмент 1 из" not in visible.text


def test_long_unknown_group_input_returns_a_telegram_sized_error(monkeypatch):
    query = "<&" * 2000
    message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
    update = SimpleNamespace(effective_message=message)
    ctx = context()
    ctx.args = [query]

    def unknown_group(*args, **kwargs):
        raise GroupNotFound("Введите полный код группы, например ИСт-241.2.")

    ctx.application.bot_data["group_schedule_service"] = SimpleNamespace(get_timetable=unknown_group)
    monkeypatch.setattr(group_bot, "local_today", lambda _: date(2026, 11, 16))

    asyncio.run(group_bot.group_command(update, ctx))

    assert message.reply_text.await_count == 2
    text = message.reply_text.await_args.args[0]
    visible = CaptionText()
    visible.feed(text)
    assert len(visible.text.encode("utf-16-le")) // 2 <= 4096
    assert "Введите полный код группы" in text
    assert group_bot.GROUP_SCHEDULE_PAGE_URL in text
    assert "&lt;&amp;" in text
    message.reply_photo.assert_not_awaited()
