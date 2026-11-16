from __future__ import annotations

import asyncio
from datetime import date, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import bot, group_bot
from app.commands import COMMANDS
from app.group_navigation import (
    group_payload, group_request, parse_period, range_payload, short_period,
    validate_period, week_bounds,
)
from app.group_schedule import GroupScheduleError
from app.group_sources import GroupScheduleSource
from app.preferences import MemoryUserPreferencesStore, SavedGroup


TODAY = date(2026, 11, 16)
GROUP = "ИСт-241.2"


def source(start=TODAY, end=date(2026, 11, 22), category="vo"):
    return GroupScheduleSource("Published schedule", category,
        "https://www.prk.kuzstu.ru/upload/offline.pdf", start, end)


def environment(monkeypatch, *, store=None, user_id=42, chat_type="private", is_bot=False,
                args=(), result=None, periods=None):
    store = store if store is not None else MemoryUserPreferencesStore()
    message = SimpleNamespace(text=GROUP, reply_text=AsyncMock(), reply_photo=AsyncMock())
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        effective_chat=SimpleNamespace(id=user_id if chat_type == "private" else -10042, type=chat_type),
        callback_query=SimpleNamespace(answer=AsyncMock()),
    )
    service = SimpleNamespace(
        get_timetable=Mock(return_value=result or SimpleNamespace(
            group=GROUP, source=source(), images=(BytesIO(b"offline PNG"),))),
        list_groups=Mock(return_value=()),
        list_periods=Mock(return_value=(source(),) if periods is None else periods),
    )
    context = SimpleNamespace(
        args=list(args), user_data={}, application=SimpleNamespace(bot_data={
            "settings": SimpleNamespace(timezone="UTC"),
            "user_preferences_store": store, "group_schedule_service": service,
        }),
    )
    monkeypatch.setattr(group_bot, "local_today", lambda _: TODAY)
    return update, context, service, store


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def text_replies(update):
    return "\n".join(call.args[0] for call in update.effective_message.reply_text.await_args_list)


def test_saved_group_is_explicit_replaces_and_clears_in_fresh_contexts(monkeypatch):
    store = MemoryUserPreferencesStore()

    async def scenario():
        update, context, service, _ = environment(monkeypatch, store=store, args=[GROUP])
        await group_bot.group_command(update, context)
        assert await store.get_group(42) is None
        assert await group_bot.handle_group_callback(update, context, group_payload("gf", "vo", GROUP))
        assert await store.get_group(42) == SavedGroup(GROUP, "vo")
        assert "ваша группа" in text_replies(update)
        assert "Сохранение временное" in text_replies(update)

        fresh, fresh_context, fresh_service, _ = environment(monkeypatch, store=store)
        await group_bot.my_group_command(fresh, fresh_context)
        fresh_service.get_timetable.assert_called_once_with(GROUP, TODAY, category="vo", period_start=None)
        fresh.effective_message.reply_photo.assert_awaited_once()
        assert any(button.callback_data == "gx" for button in buttons(
            fresh.effective_message.reply_photo.await_args.kwargs["reply_markup"]))

        other, other_context, _, _ = environment(monkeypatch, store=store, user_id=43)
        await group_bot.my_group_command(other, other_context)
        other.effective_message.reply_photo.assert_not_awaited()
        assert "пока не закреплена" in text_replies(other)
        assert await store.get_group(43) is None

        assert await group_bot.handle_group_callback(fresh, fresh_context, group_payload("gf", "spo", "ГДт-231.1"))
        assert await store.get_group(42) == SavedGroup("ГДт-231.1", "spo")
        latest, latest_context, _, _ = environment(monkeypatch, store=store)
        assert await group_bot.handle_group_callback(latest, latest_context, "gx")
        assert await store.get_group(42) is None
        assert "откреплена" in text_replies(latest)

    asyncio.run(scenario())


@pytest.mark.parametrize("chat_type,is_bot", [("group", False), ("supergroup", False), ("private", True)])
@pytest.mark.parametrize("action", ["gf:v:ИСт-241.2", "gx", "gm"])
def test_personal_preferences_are_private_and_require_real_human(monkeypatch, chat_type, is_bot, action):
    store = SimpleNamespace(get_group=AsyncMock(), set_group=AsyncMock(), clear_group=AsyncMock())
    update, context, service, _ = environment(monkeypatch, store=store, chat_type=chat_type, is_bot=is_bot)
    assert asyncio.run(group_bot.handle_group_callback(update, context, action))
    store.get_group.assert_not_awaited()
    store.set_group.assert_not_awaited()
    store.clear_group.assert_not_awaited()
    service.get_timetable.assert_not_called()
    assert "личном чате" in text_replies(update)


@pytest.mark.parametrize("error", [RuntimeError("private internal storage failure"), TimeoutError()])
@pytest.mark.parametrize("action", ["gf:v:ИСт-241.2", "gm", "gx"])
def test_storage_failure_has_no_success_message_or_internal_details(monkeypatch, error, action):
    store = SimpleNamespace(is_persistent=True,
        get_group=AsyncMock(side_effect=error), set_group=AsyncMock(side_effect=error),
        clear_group=AsyncMock(side_effect=error))
    update, context, service, _ = environment(monkeypatch, store=store)
    assert asyncio.run(group_bot.handle_group_callback(update, context, action))
    text = text_replies(update)
    assert "Не удалось обратиться" in text
    assert "private internal" not in text
    assert "ваша группа" not in text and "Группа откреплена" not in text
    service.get_timetable.assert_not_called()


def test_slow_store_is_bounded_and_does_not_acknowledge_a_save(monkeypatch):
    async def slow(*_args):
        await asyncio.sleep(1)
    store = SimpleNamespace(is_persistent=True, set_group=slow)
    update, context, _, _ = environment(monkeypatch, store=store)
    monkeypatch.setattr(group_bot, "PREFERENCES_TIMEOUT_SECONDS", 0.01)
    asyncio.run(group_bot.handle_group_callback(update, context, "gf:v:ИСт-241.2"))
    assert "Не удалось обратиться" in text_replies(update)
    assert "ваша группа" not in text_replies(update)


def test_broken_preferences_do_not_break_normal_schedule_or_prompt(monkeypatch):
    store = SimpleNamespace(get_group=AsyncMock(side_effect=RuntimeError("private storage error")))
    update, context, service, _ = environment(monkeypatch, store=store, args=[GROUP])

    async def scenario():
        await group_bot.my_group_command(update, context)
        await group_bot.group_command(update, context)
        await group_bot.send_group_prompt(update, context)
    asyncio.run(scenario())
    service.get_timetable.assert_called_once_with(GROUP, TODAY, category=None, period_start=None)
    update.effective_message.reply_photo.assert_awaited_once()
    assert {button.callback_data for button in buttons(update.effective_message.reply_text.await_args.kwargs["reply_markup"])} == {
        "gl:vo:0", "gl:spo:0", "gl:part_time:0"}


@pytest.mark.parametrize("day,offset,start,end", [
    (date(2026, 11, 22), 0, date(2026, 11, 16), date(2026, 11, 22)),
    (date(2026, 1, 1), 0, date(2025, 12, 29), date(2026, 1, 4)),
    (date(2026, 1, 1), -1, date(2025, 12, 22), date(2025, 12, 28)),
    (date(2025, 12, 31), 1, date(2026, 1, 5), date(2026, 1, 11)),
])
def test_week_bounds_handle_sunday_and_calendar_boundaries(day, offset, start, end):
    assert week_bounds(day, offset) == (start, end)


@pytest.mark.parametrize("value,expected", [
    ("current", (date(2026, 11, 16), date(2026, 11, 22))),
    ("прошлая неделя", (date(2026, 11, 9), date(2026, 11, 15))),
    ("следующая", (date(2026, 11, 23), date(2026, 11, 29))),
    ("29.12.2025 — 04.01.2026", (date(2025, 12, 29), date(2026, 1, 4))),
    ("01/11/2026 по 30/11/2026", (date(2026, 11, 1), date(2026, 11, 30))),
    ("01.11.2026 01.11.2026", (date(2026, 11, 1), date(2026, 11, 1))),
])
def test_named_and_explicit_periods_parse(value, expected):
    assert parse_period(value, TODAY) == expected


@pytest.mark.parametrize("value", [
    "31.02.2026 05.03.2026", "22.11.2026 16.11.2026", "01.11.2026 02.12.2026",
    "16.11.2026", "16.11 22.11", "tomorrow", "16.11.2026 22.11.2026 arbitrary text",
    "01.11.2026 10.11.2026 12.11.2026", "2026-11-16 2026-11-22",
])
def test_ambiguous_invalid_reversed_or_overlong_periods_are_rejected(value):
    with pytest.raises(ValueError):
        parse_period(value, TODAY)


def test_period_is_inclusive_maximum_31_days_and_group_request_preserves_legacy():
    assert validate_period(date(2026, 11, 1), date(2026, 12, 1)) == (date(2026, 11, 1), date(2026, 12, 1))
    with pytest.raises(ValueError):
        validate_period(date(2026, 11, 1), date(2026, 12, 2))
    assert group_request(GROUP, TODAY) == (GROUP, None, None)
    assert group_request(GROUP + " next", TODAY) == (GROUP, date(2026, 11, 23), date(2026, 11, 29))
    assert group_request(GROUP + " 29.12.2025 04.01.2026", TODAY) == (GROUP, date(2025, 12, 29), date(2026, 1, 4))
    with pytest.raises(ValueError):
        group_request("A" * 161, TODAY)


@pytest.mark.parametrize("category", ["vo", "spo", "part_time"])
def test_navigation_callback_payloads_fit_telegram_for_maximum_cyrillic_group(category):
    group = "АБВГДЕЖЗ-1234.123.123.123"
    assert group_bot.looks_like_group(group)
    values = [
        group_payload("gf", category, group), group_payload("gc", category, group),
        group_payload("ge", category, group), group_payload("gp", category, group, "99999"),
        range_payload(category, group, date(2025, 12, 29), date(2026, 1, 4)),
    ]
    markup = group_bot._timetable_keyboard(group, source(category=category), saved=True)
    values.extend(button.callback_data for button in buttons(markup))
    assert all(len(value.encode("utf-8")) <= 64 for value in values)
    with pytest.raises(ValueError):
        group_payload("gf", category, "Я" * 100)


@pytest.mark.parametrize("data", [
    "gf::", "gf:x:ИСт-241.2", "gf:v:", "gf:v:<script>", "gf:v:ИСт-241.2:extra",
    "gp:v:-1:ИСт-241.2", "gp:v:100000:ИСт-241.2", "gp:v:no:ИСт-241.2",
    "gk:v:20260231:20260301:ИСт-241.2", "gk:v:20261122:20261116:ИСт-241.2",
    "gk:v:20261101:20261202:ИСт-241.2", "gk:v:2026111:20261122:ИСт-241.2",
    "gf:v:" + "Я" * 100,
])
def test_malformed_navigation_never_reaches_storage_or_schedule(monkeypatch, data):
    store = SimpleNamespace(get_group=AsyncMock(), set_group=AsyncMock(), clear_group=AsyncMock())
    update, context, service, _ = environment(monkeypatch, store=store)
    assert asyncio.run(group_bot.handle_group_callback(update, context, data))
    store.get_group.assert_not_awaited()
    store.set_group.assert_not_awaited()
    store.clear_group.assert_not_awaited()
    service.get_timetable.assert_not_called()
    service.list_periods.assert_not_called()
    service.list_groups.assert_not_called()
    assert "кнопка недоступна" in text_replies(update)


def test_period_list_is_paginated_and_does_not_render_or_download_a_schedule(monkeypatch):
    periods = tuple(source(TODAY - timedelta(weeks=i), TODAY - timedelta(weeks=i) + timedelta(days=6)) for i in range(8))
    update, context, service, _ = environment(monkeypatch, periods=periods)
    assert asyncio.run(group_bot.handle_group_callback(update, context, "gp:v:0:ИСт-241.2"))
    service.list_periods.assert_called_once_with(TODAY, category="vo")
    service.get_timetable.assert_not_called()
    service.list_groups.assert_not_called()
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert sum(button.callback_data.startswith("gk:") for button in buttons(markup)) == group_bot.PERIODS_PER_PAGE + 2
    next_callback = next(button.callback_data for button in buttons(markup) if button.text == "Далее →")
    assert next_callback == "gp:v:6:ИСт-241.2"
    fresh, fresh_context, _, _ = environment(monkeypatch, periods=periods)
    assert asyncio.run(group_bot.handle_group_callback(fresh, fresh_context, next_callback))
    assert "страница 2 из 2" in text_replies(fresh)
    assert any(button.text == "← Назад" for button in buttons(fresh.effective_message.reply_text.await_args.kwargs["reply_markup"]))


def test_selected_week_caption_keeps_actual_document_dates_and_one_photo(monkeypatch):
    result = SimpleNamespace(group=GROUP, source=source(end=date(2026, 11, 21)), images=(BytesIO(b"offline PNG"),))
    update, context, service, _ = environment(monkeypatch, result=result)
    assert asyncio.run(group_bot.handle_group_callback(update, context, range_payload("vo", GROUP, TODAY, date(2026, 11, 22))))
    service.get_timetable.assert_called_once_with(GROUP, TODAY, category="vo", period_start=TODAY, period_end=date(2026, 11, 22))
    update.effective_message.reply_photo.assert_awaited_once()
    caption = update.effective_message.reply_photo.await_args.kwargs["caption"]
    assert "Запрошено: 16.11.2026 — 22.11.2026" in caption
    assert "Документ: 16.11.2026 — 21.11.2026" in caption
    assert "покрывает часть" in caption
    assert "Оригинальное расписание (PDF)" in caption


def test_mygroup_week_command_reads_saved_group_and_uses_explicit_range(monkeypatch):
    update, context, service, store = environment(monkeypatch, args=["next"])
    async def scenario():
        await store.set_group(42, SavedGroup(GROUP, "vo"))
        await group_bot.week_command(update, context)
    asyncio.run(scenario())
    service.get_timetable.assert_called_once_with(GROUP, TODAY, category="vo", period_start=date(2026, 11, 23), period_end=date(2026, 11, 29))


def test_old_group_callback_remains_stateless_and_unchanged(monkeypatch):
    update, context, service, _ = environment(monkeypatch)
    assert asyncio.run(group_bot.handle_group_callback(update, context, "gg:vo:20261116:ИСт-241.2"))
    service.get_timetable.assert_called_once_with(GROUP, TODAY, category="vo", period_start=TODAY)
    update.effective_message.reply_photo.assert_awaited_once()
    update.callback_query.answer.assert_not_awaited()


@pytest.mark.parametrize("text", ["⭐ Моя группа", "Моя группа"])
def test_mygroup_menu_route_does_not_reach_teacher_or_category_prompt(monkeypatch, text):
    update, context, _, _ = environment(monkeypatch)
    update.effective_message.text = text
    context.user_data["awaiting_teacher"] = True
    saved = AsyncMock()
    teacher, prompt = AsyncMock(), AsyncMock()
    monkeypatch.setattr(bot, "my_group_command", saved)
    monkeypatch.setattr(bot, "send_teacher_schedule", teacher)
    monkeypatch.setattr(bot, "send_group_prompt", prompt)
    asyncio.run(bot.on_text(update, context))
    saved.assert_awaited_once_with(update, context)
    teacher.assert_not_awaited()
    prompt.assert_not_awaited()
    assert "awaiting_teacher" not in context.user_data


def test_all_previous_keyboard_labels_and_commands_are_retained():
    labels = {button.text for row in bot.MAIN_KEYBOARD.keyboard for button in row}
    assert {
        "Расписание группы", "Вчера", "Сегодня", "Завтра", "Ближайшие даты",
        "Найти преподавателя", "Все преподаватели", "Обновить", "Статистика",
        "⭐ Моя группа",
    } <= labels
    names = {command["command"] for command in COMMANDS}
    assert {"mygroup", "week", "group", "groups", "today", "stats"} <= names
    assert len(bot.MAIN_KEYBOARD.keyboard) == 5


def test_short_period_keeps_year_visible_when_navigation_crosses_year():
    assert short_period(date(2025, 12, 29), date(2026, 1, 4)) == "29.12.25–04.01.26"


def test_custom_multiweek_command_offers_every_matching_publication_without_rendering(monkeypatch):
    periods = (source(), source(date(2026, 11, 9), date(2026, 11, 15)))
    update, context, service, _ = environment(monkeypatch,
        args=[GROUP, "09.11.2026", "22.11.2026"], periods=periods)
    asyncio.run(group_bot.group_command(update, context))
    service.list_periods.assert_called_once_with(TODAY, category=None)
    service.get_timetable.assert_not_called()
    update.effective_message.reply_photo.assert_not_awaited()
    text = text_replies(update)
    assert "09.11.2026 — 22.11.2026" in text
    assert "несколько официальных документов" in text
    assert "каждую неделю пришлю отдельным цельным изображением" in text
    callbacks = {button.callback_data for button in buttons(
        update.effective_message.reply_text.await_args.kwargs["reply_markup"])}
    assert callbacks == {
        range_payload("vo", GROUP, date(2026, 11, 9), date(2026, 11, 15)),
        range_payload("vo", GROUP, TODAY, date(2026, 11, 22)), "gh",
    }


def test_saved_group_multiweek_command_uses_saved_category(monkeypatch):
    periods = (source(), source(date(2026, 11, 9), date(2026, 11, 15)))
    update, context, service, store = environment(monkeypatch,
        args=["09.11.2026", "22.11.2026"], periods=periods)
    async def scenario():
        await store.set_group(42, SavedGroup(GROUP, "vo"))
        await group_bot.week_command(update, context)
    asyncio.run(scenario())
    service.list_periods.assert_called_once_with(TODAY, category="vo")
    service.get_timetable.assert_not_called()
    assert "несколько официальных документов" in text_replies(update)


@pytest.mark.parametrize("args", [
    [GROUP, "22.11.2026", "09.11.2026"],
    [GROUP, "31.02.2026", "08.03.2026"],
    [GROUP, "01.11.2026", "02.12.2026"],
])
def test_invalid_custom_command_rejects_before_catalogue_or_render(monkeypatch, args):
    update, context, service, _ = environment(monkeypatch, args=args)
    asyncio.run(group_bot.group_command(update, context))
    service.list_periods.assert_not_called()
    service.get_timetable.assert_not_called()
    update.effective_message.reply_photo.assert_not_awaited()
    assert "Пример:" in text_replies(update)


def test_unpublished_custom_range_returns_error_without_showing_latest_source(monkeypatch):
    update, context, service, _ = environment(monkeypatch,
        args=[GROUP, "23.11.2026", "06.12.2026"], periods=(source(),))
    service.get_timetable.side_effect = GroupScheduleError("На этот период расписание ещё не опубликовано.")
    asyncio.run(group_bot.group_command(update, context))
    service.list_periods.assert_called_once_with(TODAY, category=None)
    service.get_timetable.assert_called_once_with(GROUP, TODAY, category=None,
        period_start=date(2026, 11, 23), period_end=date(2026, 12, 6))
    update.effective_message.reply_photo.assert_not_awaited()
    assert "ещё не опубликовано" in text_replies(update)
    assert "Последнее опубликованное" not in text_replies(update)


def test_multiweek_catalogue_failure_does_not_fallback_to_unrelated_schedule(monkeypatch):
    update, context, service, _ = environment(monkeypatch,
        args=[GROUP, "09.11.2026", "22.11.2026"])
    service.list_periods.side_effect = RuntimeError("FAKE_PRIVATE_UPSTREAM_DETAILS")
    asyncio.run(group_bot.group_command(update, context))
    service.get_timetable.assert_not_called()
    update.effective_message.reply_photo.assert_not_awaited()
    assert "Не удалось загрузить" in text_replies(update)
    assert "FAKE_PRIVATE" not in text_replies(update)


def test_empty_period_catalogue_keeps_week_and_custom_date_navigation(monkeypatch):
    update, context, service, _ = environment(monkeypatch, periods=())
    asyncio.run(group_bot.handle_group_callback(update, context, "gp:v:0:ИСт-241.2"))
    service.get_timetable.assert_not_called()
    assert "пока нет датированных публикаций" in text_replies(update)
    labels = {button.text for button in buttons(update.effective_message.reply_text.await_args.kwargs["reply_markup"])}
    assert {"Прошлая", "Текущая", "Следующая", "Ввести свой период", "Разделы расписания"} <= labels


def test_invalid_group_in_custom_range_does_not_make_unusable_buttons(monkeypatch):
    update, context, service, _ = environment(monkeypatch, args=["Короткова", "09.11.2026", "22.11.2026"])
    asyncio.run(group_bot.group_command(update, context))
    service.list_periods.assert_not_called()
    service.get_timetable.assert_not_called()
    assert "Введите полный код группы" in text_replies(update)


def test_natural_russian_from_to_range_is_accepted():
    assert group_request("ИСт-241.2 с 09.11.2026 по 22.11.2026", TODAY) == (
        GROUP, date(2026, 11, 9), date(2026, 11, 22),
    )


def test_long_publication_button_opens_official_pdf_instead_of_invalid_callback(monkeypatch):
    long_source = source(date(2026, 11, 1), date(2026, 12, 2))
    update, context, service, _ = environment(monkeypatch, periods=(long_source,))
    asyncio.run(group_bot.handle_group_callback(update, context, "gp:v:0:ИСт-241.2"))
    links = [item for item in buttons(update.effective_message.reply_text.await_args.kwargs["reply_markup"]) if item.url]
    assert len(links) == 1 and links[0].url == long_source.url
    assert "PDF" in links[0].text and links[0].callback_data is None
    service.get_timetable.assert_not_called()
