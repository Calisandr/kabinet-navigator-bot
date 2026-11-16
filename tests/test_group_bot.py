from __future__ import annotations

import asyncio
import threading
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import group_bot
from app.group_schedule import GroupNotFound, GroupScheduleError
from app.group_sources import GroupScheduleSource


TODAY = date(2026, 11, 16)
START = date(2026, 11, 16)
END = date(2026, 11, 21)


def option(group="ИСт-241.2", category="vo", start=START, end=END):
    return SimpleNamespace(
        group=group,
        category=category,
        category_label=group_bot.CATEGORIES[category],
        start=start,
        end=end,
    )


def timetable(*, group="ИСт-241.2", category="vo", start=START, end=END, images=2):
    source = GroupScheduleSource(
        title="Официальное расписание",
        category=category,
        url="https://www.prk.kuzstu.ru/upload/schedule.pdf?a=1&b=2",
        start=start,
        end=end,
    )
    return SimpleNamespace(
        group=group,
        source=source,
        images=tuple(BytesIO(b"offline-image") for _ in range(images)),
    )


def environment(monkeypatch, *, args=(), text="ИСт-241.2", options=None, result=None):
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), reply_photo=AsyncMock())
    update = SimpleNamespace(effective_message=message, callback_query=SimpleNamespace(answer=AsyncMock()))
    service = SimpleNamespace(
        list_groups=Mock(return_value=(option(),) if options is None else options),
        get_timetable=Mock(return_value=timetable() if result is None else result),
    )
    context = SimpleNamespace(
        args=list(args),
        application=SimpleNamespace(
            bot_data={"group_schedule_service": service, "settings": SimpleNamespace(timezone="UTC")}
        ),
    )
    monkeypatch.setattr(group_bot, "local_today", lambda _: TODAY)
    return update, context, service


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


@pytest.mark.parametrize("code", ["ИСт-241.2", "ист-241.2", "ИСт241.2", "СЭЗ-223", "ГДт-231.1", "  ИСт-241.2  ", "ТАт-231.2.1", "МАб-241.2.2", "ИСт-241.2.1.100"])
def test_complete_group_codes_are_recognized(code):
    assert group_bot.looks_like_group(code)


@pytest.mark.parametrize("hyphen", "‐‑‒–—―−")
def test_unicode_hyphens_are_normalized_like_the_pdf_index(hyphen):
    assert group_bot.looks_like_group(f"ТАт{hyphen}231.2.1")


@pytest.mark.parametrize("text", ["Сегодня", "16.11.2026", "Короткова", "/group ИСт-241.2", "<b>ИСт-241.2</b>", "123", "ИСт"])
def test_teacher_names_dates_and_commands_are_not_intercepted_as_group_codes(text):
    assert not group_bot.looks_like_group(text)


@pytest.mark.parametrize("handler", [group_bot.group_command, group_bot.groups_command, group_bot.send_group_prompt])
def test_group_prompt_shows_example_and_three_categories_without_loading_pdfs(monkeypatch, handler):
    update, context, service = environment(monkeypatch)

    asyncio.run(handler(update, context))

    update.effective_message.reply_text.assert_awaited_once()
    call = update.effective_message.reply_text.await_args
    assert "ИСт-241.2" in call.args[0]
    assert {button.callback_data for button in buttons(call.kwargs["reply_markup"])} == {
        "gl:vo:0", "gl:spo:0", "gl:part_time:0"
    }
    service.list_groups.assert_not_called()
    service.get_timetable.assert_not_called()


def test_group_command_gives_feedback_before_work_and_sends_images_with_official_links(monkeypatch):
    update, context, service = environment(monkeypatch, args=["ИСт-241.2"])
    events = []
    ui_thread = threading.get_ident()

    async def reply_text(text, **kwargs):
        events.append(("feedback", text))

    def get_timetable(*args, **kwargs):
        events.append(("service", threading.get_ident()))
        return timetable()

    update.effective_message.reply_text.side_effect = reply_text
    service.get_timetable.side_effect = get_timetable

    asyncio.run(group_bot.group_command(update, context))

    assert events[0] == ("feedback", "Подготавливаю расписание...")
    assert events[1][0] == "service" and events[1][1] != ui_thread
    service.get_timetable.assert_called_once_with("ИСт-241.2", TODAY, category=None, period_start=None)
    assert update.effective_message.reply_photo.await_count == 2
    for index, call in enumerate(update.effective_message.reply_photo.await_args_list, start=1):
        caption = call.kwargs["caption"]
        assert "ИСт-241.2" in caption
        assert "16.11.2026 — 21.11.2026" in caption
        assert "ВО и лицей" in caption
        assert "Оригинальное расписание (PDF)" in caption
        assert 'href="https://www.prk.kuzstu.ru/upload/schedule.pdf?a=1&amp;b=2"' in caption
        assert "Полный официальный документ филиала; из него взят фрагмент вашей группы." in caption
        assert f"Фрагмент {index} из 2" in caption
        assert call.kwargs["photo"].tell() == 0


def test_typed_group_uses_the_same_service_and_shows_actual_past_period(monkeypatch):
    past = timetable(start=date(2026, 11, 2), end=date(2026, 11, 7), images=1)
    update, context, service = environment(monkeypatch, text=" ИСт-241.2 ", result=past)

    asyncio.run(group_bot.on_group_text(update, context))

    service.get_timetable.assert_called_once_with("ИСт-241.2", TODAY, category=None, period_start=None)
    caption = update.effective_message.reply_photo.await_args.kwargs["caption"]
    assert "02.11.2026 — 07.11.2026" in caption
    assert "Последнее опубликованное расписание (период уже завершён)" in caption
    assert "текущее расписание" not in caption.casefold()


def test_future_period_is_named_explicitly(monkeypatch):
    future = timetable(start=date(2026, 11, 23), end=date(2026, 11, 28), images=1)
    update, context, _ = environment(monkeypatch, args=["ИСт-241.2"], result=future)

    asyncio.run(group_bot.group_command(update, context))

    caption = update.effective_message.reply_photo.await_args.kwargs["caption"]
    assert "23.11.2026 — 28.11.2026" in caption
    assert "Расписание на будущий период" in caption


def test_explicit_past_period_is_not_described_as_latest_publication(monkeypatch):
    past = timetable(start=date(2026, 11, 2), end=date(2026, 11, 7), images=1)
    update, context, service = environment(monkeypatch, result=past)

    asyncio.run(group_bot.handle_group_callback(update, context, "gg:vo:20261102:ИСт-241.2"))

    service.get_timetable.assert_called_once_with("ИСт-241.2", TODAY, category="vo", period_start=date(2026, 11, 2))
    caption = update.effective_message.reply_photo.await_args.kwargs["caption"]
    assert "Период уже завершён" in caption
    assert "Последнее опубликованное" not in caption


def test_explicit_unpublished_period_shows_error_without_returning_another_document(monkeypatch):
    update, context, service = environment(monkeypatch)
    service.get_timetable.side_effect = GroupScheduleError("На выбранный период расписание ещё не опубликовано.")

    asyncio.run(group_bot.handle_group_callback(update, context, "gg:vo:20261123:ИСт-241.2"))

    service.get_timetable.assert_called_once_with("ИСт-241.2", TODAY, category="vo", period_start=date(2026, 11, 23))
    assert "ещё не опубликовано" in update.effective_message.reply_text.await_args.args[0]
    update.effective_message.reply_photo.assert_not_awaited()


def test_category_lists_paginate_with_period_in_callbacks_and_work_in_a_fresh_context(monkeypatch):
    options = tuple(option(f"ИСт-241.{index}") for index in range(1, 26))
    update, context, service = environment(monkeypatch, options=options)

    assert asyncio.run(group_bot.handle_group_callback(update, context, "gl:vo:0")) is True

    service.list_groups.assert_called_once_with(TODAY, category="vo", period_start=None)
    first_call = update.effective_message.reply_text.await_args
    first_buttons = buttons(first_call.kwargs["reply_markup"])
    assert len([button for button in first_buttons if button.callback_data.startswith("gg:")]) == group_bot.GROUPS_PER_PAGE
    next_page = next(button.callback_data for button in first_buttons if button.text == "Далее →")
    assert next_page == "gw:vo:20261116:12"
    assert all(len(button.callback_data.encode("utf-8")) <= 64 for button in first_buttons)
    update.callback_query.answer.assert_not_awaited()

    fresh_update, fresh_context, fresh_service = environment(monkeypatch, options=options)
    assert asyncio.run(group_bot.handle_group_callback(fresh_update, fresh_context, next_page)) is True
    fresh_service.list_groups.assert_called_once_with(TODAY, category="vo", period_start=START)
    assert "Страница 2 из 3" in fresh_update.effective_message.reply_text.await_args.args[0]


def test_group_callback_contains_category_group_and_period_without_user_state(monkeypatch):
    update, context, service = environment(monkeypatch)

    assert asyncio.run(group_bot.handle_group_callback(update, context, "gg:vo:20261116:ИСт-241.2")) is True

    service.get_timetable.assert_called_once_with("ИСт-241.2", TODAY, category="vo", period_start=START)
    update.callback_query.answer.assert_not_awaited()


def test_multi_suffix_group_is_supported_in_plain_text_and_callback(monkeypatch):
    group = "ТАт-231.2.1"
    update, context, service = environment(monkeypatch, text=group, result=timetable(group=group))

    assert group_bot.looks_like_group(group)
    asyncio.run(group_bot.on_group_text(update, context))
    service.get_timetable.assert_called_once_with(group, TODAY, category=None, period_start=None)

    clicked, fresh_context, fresh_service = environment(monkeypatch, result=timetable(group=group))
    asyncio.run(group_bot.handle_group_callback(clicked, fresh_context, f"gg:vo:20261116:{group}"))
    fresh_service.get_timetable.assert_called_once_with(group, TODAY, category="vo", period_start=START)
    clicked.effective_message.reply_photo.assert_awaited()


def test_groups_command_accepts_category_alias(monkeypatch):
    update, context, service = environment(monkeypatch, args=["СПО"], options=(option("СЭЗ-223", "spo"),))

    asyncio.run(group_bot.groups_command(update, context))

    service.list_groups.assert_called_once_with(TODAY, category="spo", period_start=None)
    assert "СПО" in update.effective_message.reply_text.await_args.args[0]


def test_latest_part_time_group_list_marks_the_finished_saturday(monkeypatch):
    saturday = date(2026, 11, 14)
    update, context, _ = environment(
        monkeypatch,
        options=(option("ТАт-231.2.1", "part_time", saturday, saturday),),
    )

    asyncio.run(group_bot.handle_group_callback(update, context, "gl:part_time:0"))

    text = update.effective_message.reply_text.await_args.args[0]
    assert "14.11.2026 — 14.11.2026" in text
    assert "Последнее опубликованное расписание (период уже завершён)" in text
    assert "текущее расписание" not in text.casefold()


def test_not_found_shows_suggestions_and_escapes_user_input(monkeypatch):
    query = '<b>wrong</b> & "group"'
    update, context, service = environment(monkeypatch, args=[query])
    service.get_timetable.side_effect = GroupNotFound("Группа не найдена.", suggestions=(option(),))

    asyncio.run(group_bot.group_command(update, context))

    call = update.effective_message.reply_text.await_args
    assert "Группа не найдена" in call.args[0]
    assert "Похожие группы" in call.args[0]
    assert "&lt;b&gt;wrong&lt;/b&gt; &amp; &quot;group&quot;" in call.args[0]
    assert group_bot.GROUP_SCHEDULE_PAGE_URL in call.args[0]
    assert buttons(call.kwargs["reply_markup"])[0].callback_data == "gg:vo:20261116:ИСт-241.2"


@pytest.mark.parametrize("operation", ["list", "get"])
def test_unexpected_service_failure_returns_helpful_response_without_private_error(monkeypatch, caplog, operation):
    update, context, service = environment(monkeypatch, args=["ИСт-241.2"])
    secret = "FAKE_PRIVATE_TOKEN_backend_failure"
    if operation == "list":
        service.list_groups.side_effect = RuntimeError(secret)
        action = group_bot.handle_group_callback(update, context, "gl:vo:0")
    else:
        service.get_timetable.side_effect = RuntimeError(secret)
        action = group_bot.group_command(update, context)

    asyncio.run(action)

    text = update.effective_message.reply_text.await_args.args[0]
    assert "Попробуйте чуть позже" in text
    assert group_bot.GROUP_SCHEDULE_PAGE_URL in text
    assert secret not in text
    assert secret not in caplog.text


def test_expected_source_error_is_shown_safely(monkeypatch):
    update, context, service = environment(monkeypatch, args=["ИСт-241.2"])
    service.get_timetable.side_effect = GroupScheduleError(
        "На этот период <документ> ещё не опубликован.",
        source_url="https://www.prk.kuzstu.ru/upload/schedule.pdf?a=1&b=2",
    )

    asyncio.run(group_bot.group_command(update, context))

    text = update.effective_message.reply_text.await_args.args[0]
    assert "&lt;документ&gt;" in text
    assert 'href="https://www.prk.kuzstu.ru/upload/schedule.pdf?a=1&amp;b=2"' in text
    assert "Оригинальное расписание (PDF)" in text
    assert group_bot.GROUP_SCHEDULE_PAGE_URL in text
    update.effective_message.reply_photo.assert_not_awaited()


def test_timeout_returns_a_clear_response_instead_of_silence(monkeypatch):
    update, context, _ = environment(monkeypatch, args=["ИСт-241.2"])

    async def never_finished(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(group_bot.asyncio, "to_thread", never_finished)
    monkeypatch.setattr(group_bot, "GROUP_REQUEST_TIMEOUT_SECONDS", 0.01)

    asyncio.run(group_bot.group_command(update, context))

    assert "слишком много времени" in update.effective_message.reply_text.await_args.args[0]
    update.effective_message.reply_photo.assert_not_awaited()


def test_empty_images_and_empty_group_lists_have_explicit_responses(monkeypatch):
    update, context, _ = environment(monkeypatch, args=["ИСт-241.2"], result=timetable(images=0))
    asyncio.run(group_bot.group_command(update, context))
    assert "не удалось подготовить изображение" in update.effective_message.reply_text.await_args.args[0]

    empty_update, empty_context, _ = environment(monkeypatch, options=())
    asyncio.run(group_bot.handle_group_callback(empty_update, empty_context, "gl:vo:0"))
    assert "группы не найдены" in empty_update.effective_message.reply_text.await_args.args[0]


def test_photo_send_failure_still_returns_the_official_pdf(monkeypatch):
    update, context, _ = environment(monkeypatch, args=["ИСт-241.2"])
    update.effective_message.reply_photo.side_effect = RuntimeError("offline photo failure")

    asyncio.run(group_bot.group_command(update, context))

    text = update.effective_message.reply_text.await_args.args[0]
    assert "Не удалось отправить изображение" in text
    assert "Оригинальное расписание (PDF)" in text
    assert "offline photo failure" not in text


@pytest.mark.parametrize(
    "data",
    ["gl:unknown:0", "gl:vo:-1", "gl:vo:no", "gg:vo:20261301:ИСт-241.2", "gg:vo:20261116:<invalid>", "gw:vo:20261116:-1", "gg:vo:20261116:" + "Я" * 40],
)
def test_malformed_callbacks_do_not_load_documents_or_answer_twice(monkeypatch, data):
    update, context, service = environment(monkeypatch)

    assert asyncio.run(group_bot.handle_group_callback(update, context, data)) is True

    assert "кнопка недоступна" in update.effective_message.reply_text.await_args.args[0]
    service.list_groups.assert_not_called()
    service.get_timetable.assert_not_called()
    update.callback_query.answer.assert_not_awaited()


def test_unrelated_callback_is_left_for_other_handlers(monkeypatch):
    update, context, service = environment(monkeypatch)

    assert asyncio.run(group_bot.handle_group_callback(update, context, "date:2026-11-16")) is False

    update.effective_message.reply_text.assert_not_awaited()
    service.list_groups.assert_not_called()


def test_long_group_code_remains_visible_but_never_generates_oversized_callback(monkeypatch):
    long_group = "А" * 40 + "-241.2"
    update, context, _ = environment(monkeypatch, options=(option(long_group),))

    asyncio.run(group_bot.handle_group_callback(update, context, "gl:vo:0"))

    call = update.effective_message.reply_text.await_args
    assert long_group in call.args[0]
    assert "введите её полный код" in call.args[0]
    assert all(len(button.callback_data.encode("utf-8")) <= 64 for button in buttons(call.kwargs["reply_markup"]))


def test_local_today_falls_back_without_timezone_database():
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": SimpleNamespace(timezone="Does/NotExist")}))

    today = group_bot.local_today(context)

    assert isinstance(today, date)
