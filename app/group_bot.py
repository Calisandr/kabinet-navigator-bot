from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from .group_pdf import normalize_group
from .group_navigation import (
    CATEGORY_NAMES, group_payload, group_request, parse_period, range_payload,
    short_period, validate_period, week_bounds,
)
from .group_schedule import GroupNotFound, GroupScheduleError
from .group_sources import GROUP_SCHEDULE_PAGE_URL
from .preferences import SavedGroup


LOGGER = logging.getLogger(__name__)
GROUP_REQUEST_TIMEOUT_SECONDS = 35.0
GROUPS_PER_PAGE = 12
PERIODS_PER_PAGE = 6
PREFERENCES_TIMEOUT_SECONDS = 2.0
CATEGORIES = {"vo": "ВО и лицей", "spo": "СПО", "part_time": "Заочная форма"}
_GROUP_CODE = re.compile(r"[A-ZА-ЯЁ]{1,8}-?\d{2,4}(?:\.\d{1,3}){0,3}")


def looks_like_group(text: str) -> bool:
    return bool(_GROUP_CODE.fullmatch(normalize_group(text)))


def local_today(context) -> date:
    settings = context.application.bot_data.get("settings")
    name = getattr(settings, "timezone", "UTC")
    try:
        local_timezone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        local_timezone = timezone.utc
    return datetime.now(local_timezone).date()


def _message(update):
    return update.effective_message


def _period(start: date, end: date) -> str:
    return f"{start:%d.%m.%Y} — {end:%d.%m.%Y}"


def _period_note(start: date, end: date, today: date, *, latest_available: bool = False) -> str:
    if end < today:
        if latest_available:
            return "\nПоследнее опубликованное расписание (период уже завершён)."
        return "\nПериод уже завершён."
    if start > today:
        return "\nРасписание на будущий период."
    return ""


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _publication_button(group: str, source, *, with_category: bool = False) -> InlineKeyboardButton:
    label = _period(source.start, source.end)
    if with_category:
        label = f"{CATEGORIES[source.category]} · {short_period(source.start, source.end)}"
    # Published documents can cover more than the custom-request allowance.
    # Keep those accessible without emitting an invalid date-range callback.
    if (source.end - source.start).days >= 31:
        return InlineKeyboardButton(label + " · PDF", url=source.url)
    return InlineKeyboardButton(label, callback_data=range_payload(source.category, group, source.start, source.end))


def _category_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"gl:{category}:0")] for category, label in CATEGORIES.items()]
    )


def _group_button(option) -> InlineKeyboardButton | None:
    data = f"gg:{option.category}:{option.start:%Y%m%d}:{option.group}"
    if len(data.encode("utf-8")) > 64:
        return None
    return InlineKeyboardButton(option.group, callback_data=data)


def _suggestion_keyboard(options) -> InlineKeyboardMarkup | None:
    buttons = [button for option in options[:GROUPS_PER_PAGE] if (button := _group_button(option)) is not None]
    return InlineKeyboardMarkup([buttons[index:index + 2] for index in range(0, len(buttons), 2)]) if buttons else None


def _saved_user_id(update) -> int | None:
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if user is None or user.is_bot or chat is None or chat.type != "private":
        return None
    return user.id


async def _read_saved(update, context) -> SavedGroup | None:
    user_id = _saved_user_id(update)
    store = context.application.bot_data.get("user_preferences_store")
    if user_id is None or store is None:
        return None
    return await asyncio.wait_for(store.get_group(user_id), timeout=PREFERENCES_TIMEOUT_SECONDS)


def _timetable_keyboard(group: str, source, *, anchor: date | None = None, saved: bool = False):
    base = anchor or source.start
    previous = week_bounds(base, -1)
    following = week_bounds(base, 1)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"← {short_period(*previous)}", callback_data=range_payload(source.category, group, *previous)),
            InlineKeyboardButton("Текущая", callback_data=group_payload("gc", source.category, group)),
            InlineKeyboardButton(f"{short_period(*following)} →", callback_data=range_payload(source.category, group, *following)),
        ],
        [
            InlineKeyboardButton("📅 Выбрать период", callback_data=group_payload("gp", source.category, group, "0")),
            InlineKeyboardButton("⭐ Закрепить группу", callback_data=group_payload("gf", source.category, group)),
        ],
        [InlineKeyboardButton("Группы этого периода", callback_data=f"gw:{source.category}:{source.start:%Y%m%d}:0")],
        *([[InlineKeyboardButton("Открепить мою группу", callback_data="gx")]] if saved else []),
    ])


async def _preferences_error(update, exc: Exception) -> None:
    LOGGER.warning("Saved group storage failed (%s)", type(exc).__name__)
    await _message(update).reply_text(
        "Не удалось обратиться к сохранённой группе. Попробуйте чуть позже. "
        "Обычный поиск через /group продолжает работать.",
    )


async def my_group_command(update, context) -> None:
    if _saved_user_id(update) is None:
        await _message(update).reply_text("Моя группа настраивается в личном чате с ботом. Откройте его и отправьте /mygroup.")
        return
    try:
        saved = await _read_saved(update, context)
    except Exception as exc:
        await _preferences_error(update, exc)
        return
    if saved is None:
        await _message(update).reply_text(
            "Группа пока не закреплена. Найдите своё расписание и нажмите под ним «⭐ Закрепить группу».",
        )
        await send_group_prompt(update, context)
        return
    await _send_group_timetable(update, context, saved.group, category=saved.category, saved=True)


async def week_command(update, context) -> None:
    """A single complete command works even after a serverless cold start."""
    if _saved_user_id(update) is None:
        await my_group_command(update, context)
        return
    try:
        saved = await _read_saved(update, context)
    except Exception as exc:
        await _preferences_error(update, exc)
        return
    if saved is None:
        await my_group_command(update, context)
        return
    value = " ".join(getattr(context, "args", ()) or ()) or "current"
    try:
        start, end = parse_period(value, local_today(context))
    except ValueError as exc:
        await _message(update).reply_text(str(exc) + "\nПримеры: /week next или /week 16.11.2026 22.11.2026.")
        return
    await _send_requested_range(update, context, saved.group, saved.category, start, end, saved=True)


async def _save_group(update, context, group: str, category: str) -> None:
    user_id = _saved_user_id(update)
    if user_id is None:
        await _message(update).reply_text("Закрепить свою группу можно в личном чате с ботом. Найдите там её через /group.")
        return
    store = context.application.bot_data.get("user_preferences_store")
    try:
        if store is None:
            raise RuntimeError("Saved group storage unavailable")
        await asyncio.wait_for(store.set_group(user_id, SavedGroup(group, category)), timeout=PREFERENCES_TIMEOUT_SECONDS)
    except Exception as exc:
        await _preferences_error(update, exc)
        return
    text = (
        f"⭐ <b>{html.escape(group)} — ваша группа</b>\n"
        f"{html.escape(CATEGORIES[category])}\n\n"
        "Теперь открывайте её кнопкой «⭐ Моя группа» или командой /mygroup.\n"
        "Чтобы заменить группу, найдите другую и закрепите её."
    )
    if not store.is_persistent:
        text += "\n\n⚠️ Сохранение временное: настройка сбросится после перезапуска бота."
    await _message(update).reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть мою группу", callback_data="gm")],
        [InlineKeyboardButton("Открепить группу", callback_data="gx")],
    ]))


async def _clear_saved_group(update, context) -> None:
    user_id = _saved_user_id(update)
    if user_id is None:
        await my_group_command(update, context)
        return
    try:
        store = context.application.bot_data["user_preferences_store"]
        await asyncio.wait_for(store.clear_group(user_id), timeout=PREFERENCES_TIMEOUT_SECONDS)
    except Exception as exc:
        await _preferences_error(update, exc)
        return
    await _message(update).reply_text("Группа откреплена. Можно выбрать и закрепить другую.", reply_markup=_category_keyboard())


async def send_group_prompt(update, context) -> None:
    rows = list(_category_keyboard().inline_keyboard)
    saved_text = ""
    try:
        saved = await _read_saved(update, context)
        if saved is not None:
            saved_text = f"Ваша группа: <b>{html.escape(saved.group)}</b>.\n\n"
            rows.insert(0, [InlineKeyboardButton(f"⭐ {saved.group}", callback_data="gm")])
    except Exception as exc:
        LOGGER.warning("Saved group prompt unavailable (%s)", type(exc).__name__)
        saved_text = "Закреплённая группа временно недоступна; выберите её ниже.\n\n"
    await _message(update).reply_text(
        "<b>Расписание учебной группы</b>\n\n" + saved_text +
        "Введите полный код группы, например <code>ИСт-241.2</code>, "
        "или отправьте <code>/group ИСт-241.2</code>.\n\n"
        "Можно выбрать категорию и найти группу кнопкой:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def group_command(update, context) -> None:
    query = " ".join(getattr(context, "args", ()) or ()).strip()
    if not query:
        await send_group_prompt(update, context)
        return
    try:
        query, start, end = group_request(query, local_today(context))
    except ValueError as exc:
        await _message(update).reply_text(str(exc) + "\nПример: /group ИСт-241.2 16.11.2026 22.11.2026.")
        return
    if start is not None and end is not None:
        await _send_requested_range(update, context, query, None, start, end)
    else:
        await _send_group_timetable(update, context, query)


async def groups_command(update, context) -> None:
    aliases = {"во": "vo", "лицей": "vo", "спо": "spo", "заочно": "part_time", **{key: key for key in CATEGORIES}}
    args = getattr(context, "args", ()) or ()
    category = aliases.get(" ".join(args).strip().casefold())
    if category:
        await _send_group_list(update, context, category)
    else:
        await send_group_prompt(update, context)


async def on_group_text(update, context) -> None:
    query = (_message(update).text or "").strip()
    await _send_group_timetable(update, context, query)


async def _send_error(update, exc: Exception, *, query: str | None = None, reply_markup=None) -> None:
    if isinstance(exc, GroupNotFound):
        message = getattr(exc, "message", "Группа не найдена в опубликованном расписании.")
        suggestions = tuple(getattr(exc, "suggestions", ()))
        text = html.escape(message)
        if query:
            shown_query = query if len(query) <= 100 else query[:100] + "…"
            text += f"\nЗапрос: <code>{html.escape(shown_query)}</code>"
        if suggestions:
            text += "\n\nПохожие группы:\n" + "\n".join(
                f"• <code>{html.escape(option.group)}</code> — {html.escape(option.category_label)}, "
                f"{_period(option.start, option.end)}"
                for option in suggestions[:GROUPS_PER_PAGE]
            )
            text += "\nВыберите кнопку или введите полный код группы."
        markup = _suggestion_keyboard(suggestions)
    elif isinstance(exc, GroupScheduleError):
        text = html.escape(getattr(exc, "message", "Не удалось подготовить расписание группы. Попробуйте чуть позже."))
        markup = _category_keyboard()
    elif isinstance(exc, TimeoutError):
        text = "Подготовка расписания заняла слишком много времени. Попробуйте ещё раз чуть позже."
        markup = _category_keyboard()
    else:
        LOGGER.warning("Group timetable request failed (%s)", type(exc).__name__)
        text = "Не удалось загрузить расписание группы. Попробуйте чуть позже."
        markup = _category_keyboard()
    source_url = getattr(exc, "source_url", None) if isinstance(exc, GroupScheduleError) else None
    if source_url and source_url != GROUP_SCHEDULE_PAGE_URL:
        text += "\n\n" + _link(source_url, "Оригинальное расписание (PDF)")
    text += "\n\n" + _link(GROUP_SCHEDULE_PAGE_URL, "Расписание на официальном сайте филиала")
    await _message(update).reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup or markup)


async def _send_group_list(update, context, category: str, offset: int = 0, period_start: date | None = None) -> None:
    message = _message(update)
    await message.reply_text("Подготавливаю список групп...")
    today = local_today(context)
    try:
        service = context.application.bot_data["group_schedule_service"]
        options = tuple(
            await asyncio.wait_for(
                asyncio.to_thread(service.list_groups, today, category=category, period_start=period_start),
                timeout=GROUP_REQUEST_TIMEOUT_SECONDS,
            )
        )
    except Exception as exc:
        await _send_error(update, exc)
        return
    if not options:
        await message.reply_text(
            "Для этой категории и периода группы не найдены.\n\n"
            + _link(GROUP_SCHEDULE_PAGE_URL, "Проверить публикации на официальном сайте"),
            parse_mode=ParseMode.HTML,
            reply_markup=_category_keyboard(),
        )
        return

    last_offset = ((len(options) - 1) // GROUPS_PER_PAGE) * GROUPS_PER_PAGE
    offset = min(max(offset, 0), last_offset)
    page = options[offset : offset + GROUPS_PER_PAGE]
    selected_start = period_start or options[0].start
    text = f"<b>Группы — {html.escape(CATEGORIES[category])}</b>\n"
    periods = tuple(dict.fromkeys((item.start, item.end) for item in page))
    for start, end in periods:
        text += f"\nРасписание: {_period(start, end)}{_period_note(start, end, today, latest_available=period_start is None)}\n"
    text += "\n" + "\n".join(f"• <code>{html.escape(option.group)}</code>" for option in page)
    text += f"\n\nСтраница {offset // GROUPS_PER_PAGE + 1} из {(len(options) - 1) // GROUPS_PER_PAGE + 1}."
    text += "\nВыберите группу кнопкой или введите её полный код."
    group_buttons = [button for option in page if (button := _group_button(option)) is not None]
    rows = [group_buttons[index:index + 2] for index in range(0, len(group_buttons), 2)]
    navigation = []
    if offset:
        navigation.append(
            InlineKeyboardButton("← Назад", callback_data=f"gw:{category}:{selected_start:%Y%m%d}:{max(0, offset - GROUPS_PER_PAGE)}")
        )
    if offset + GROUPS_PER_PAGE < len(options):
        navigation.append(
            InlineKeyboardButton("Далее →", callback_data=f"gw:{category}:{selected_start:%Y%m%d}:{offset + GROUPS_PER_PAGE}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("Разделы расписания", callback_data="gh")])
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def _send_group_timetable(
    update, context, query: str, *, category: str | None = None,
    period_start: date | None = None, period_end: date | None = None, saved: bool = False,
) -> None:
    message = _message(update)
    await message.reply_text("Подготавливаю расписание...")
    today = local_today(context)
    try:
        service = context.application.bot_data["group_schedule_service"]
        kwargs = {"category": category, "period_start": period_start}
        if period_end is not None:
            kwargs["period_end"] = period_end
        timetable = await asyncio.wait_for(
            asyncio.to_thread(service.get_timetable, query, today, **kwargs),
            timeout=GROUP_REQUEST_TIMEOUT_SECONDS,
        )
        if len(timetable.images) != 1:
            raise GroupScheduleError(
                "Не удалось подготовить единое изображение группы на весь период. "
                "Откройте оригинальное расписание по ссылке ниже.",
                source_url=timetable.source.url,
            )
    except Exception as exc:
        recovery = None
        if category is not None and looks_like_group(query):
            recovery = InlineKeyboardMarkup([
                [InlineKeyboardButton("Текущая неделя", callback_data=group_payload("gc", category, query))],
                [InlineKeyboardButton("Опубликованные периоды", callback_data=group_payload("gp", category, query, "0"))],
            ])
        await _send_error(update, exc, query=query, reply_markup=recovery)
        return

    source = timetable.source
    caption = (
        f"<b>Группа {html.escape(timetable.group)}</b>\n"
        f"{html.escape(source.category_label)}\n"
        f"Период: {_period(source.start, source.end)}"
        f"{_period_note(source.start, source.end, today, latest_available=period_start is None)}\n\n"
        + _link(source.url, "Оригинальное расписание (PDF)")
        + "\nПолный официальный документ филиала; из него взят фрагмент вашей группы.\n"
        + _link(source.page_url, "Раздел расписания на сайте филиала")
    )
    if period_start is not None and period_end is not None:
        caption = caption.replace("Период: ", "Документ: ", 1)
        caption = f"Запрошено: {_period(period_start, period_end)}\n" + caption
        if source.start > period_start or source.end < period_end:
            caption += "\nДокумент покрывает часть выбранного периода. Другие публикации — в «Выбрать период»."
    markup = _timetable_keyboard(timetable.group, source, anchor=period_start, saved=saved)
    try:
        image = timetable.images[0]
        image.seek(0)
        await message.reply_photo(
            photo=image,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except Exception as exc:
        LOGGER.warning("Group timetable image could not be sent (%s)", type(exc).__name__)
        await message.reply_text(
            "Не удалось отправить изображение. Откройте официальный PDF по ссылке ниже.\n\n" + caption,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def _send_requested_range(update, context, group: str, category: str | None, start: date, end: date, *, saved: bool = False):
    if not looks_like_group(group):
        await _message(update).reply_text("Введите полный код группы перед датами, например /group ИСт-241.2 16.11.2026 22.11.2026.")
        return
    # A longer request may span several official weeks. Offer those documents
    # explicitly instead of quietly returning only the one with most overlap.
    if (end - start).days >= 7:
        try:
            service = context.application.bot_data["group_schedule_service"]
            periods = await asyncio.wait_for(
                asyncio.to_thread(service.list_periods, local_today(context), category=category),
                timeout=GROUP_REQUEST_TIMEOUT_SECONDS,
            )
            matching = [source for source in periods if source.start <= end and source.end >= start]
            if len(matching) > 1:
                rows = [[_publication_button(group, source, with_category=True)] for source in matching[:24]]
                rows.append([InlineKeyboardButton("Разделы расписания", callback_data="gh")])
                text = (
                    f"<b>{html.escape(group)}</b> · {_period(start, end)}\n\n"
                    "На этот диапазон найдено несколько официальных документов. "
                    "Выберите публикацию: каждую неделю пришлю отдельным цельным изображением. "
                    "Кнопки PDF открывают полный документ на сайте.\n"
                    "Наличие группы проверяется при открытии документа."
                )
                if len(matching) > 24:
                    text += "\nПоказаны 24 последние публикации. Уточните даты для остальных."
                await _message(update).reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
                return
        except Exception as exc:
            await _send_error(update, exc, query=group)
            return
    await _send_group_timetable(update, context, group, category=category, period_start=start, period_end=end, saved=saved)


async def _send_period_list(update, context, group: str, category: str, offset: int = 0) -> None:
    try:
        service = context.application.bot_data["group_schedule_service"]
        periods = await asyncio.wait_for(
            asyncio.to_thread(service.list_periods, local_today(context), category=category),
            timeout=GROUP_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        await _send_error(update, exc)
        return
    last_offset = ((len(periods) - 1) // PERIODS_PER_PAGE) * PERIODS_PER_PAGE if periods else 0
    offset = min(max(0, offset), last_offset)
    current = week_bounds(local_today(context))
    previous, following = week_bounds(current[0], -1), week_bounds(current[0], 1)
    rows = [
        [InlineKeyboardButton("Прошлая", callback_data=range_payload(category, group, *previous)),
         InlineKeyboardButton("Текущая", callback_data=group_payload("gc", category, group)),
         InlineKeyboardButton("Следующая", callback_data=range_payload(category, group, *following))],
    ]
    for source in periods[offset:offset + PERIODS_PER_PAGE]:
        rows.append([_publication_button(group, source)])
    navigation = []
    if offset:
        navigation.append(InlineKeyboardButton("← Назад", callback_data=group_payload("gp", category, group, str(offset - PERIODS_PER_PAGE))))
    if offset + PERIODS_PER_PAGE < len(periods):
        navigation.append(InlineKeyboardButton("Далее →", callback_data=group_payload("gp", category, group, str(offset + PERIODS_PER_PAGE))))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("Ввести свой период", callback_data=group_payload("ge", category, group))])
    rows.append([InlineKeyboardButton("Разделы расписания", callback_data="gh")])
    text = (
        f"<b>Период для группы {html.escape(group)}</b>\n"
        f"{html.escape(CATEGORIES[category])}\n\n"
        "Выберите неделю или дату официальной публикации. Если нужной недели ещё нет на сайте, я сообщу об этом.\n\n"
    )
    if periods:
        text += f"Опубликованные документы · страница {offset // PERIODS_PER_PAGE + 1} из {last_offset // PERIODS_PER_PAGE + 1}.\n"
        text += "Наличие группы проверяется при открытии выбранного документа. Кнопки PDF открывают оригинал на сайте."
    else:
        text += "В этом разделе пока нет датированных публикаций."
    await _message(update).reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def _handle_navigation_callback(update, context, data: str) -> bool:
    if data not in {"gm", "gx", "gh"} and not data.startswith(("gf:", "gc:", "gk:", "gp:", "ge:")):
        return False
    try:
        if len(data.encode("utf-8")) > 64:
            raise ValueError("Oversized callback")
        if data == "gm":
            await my_group_command(update, context)
            return True
        if data == "gx":
            await _clear_saved_group(update, context)
            return True
        if data == "gh":
            await send_group_prompt(update, context)
            return True
        action, code, *parts = data.split(":")
        category = CATEGORY_NAMES[code]
        group = parts[-1]
        if not looks_like_group(group):
            raise ValueError("Invalid group")
        if action in {"gf", "gc", "ge"} and len(parts) == 1:
            if action == "gf":
                await _save_group(update, context, group, category)
            elif action == "gc":
                start, end = week_bounds(local_today(context))
                await _send_group_timetable(update, context, group, category=category, period_start=start, period_end=end)
            else:
                start, end = week_bounds(local_today(context))
                await _message(update).reply_text(
                    "<b>Свой период</b>\n\nОтправьте одной командой:\n"
                    f"<code>/group {html.escape(group)} {start:%d.%m.%Y} {end:%d.%m.%Y}</code>\n\n"
                    "Замените даты на свои (от 1 до 31 дня). Если группа закреплена, можно короче:\n"
                    f"<code>/week {start:%d.%m.%Y} {end:%d.%m.%Y}</code>\n\n"
                    "Бот покажет официальный документ, пересекающий выбранные даты, и его фактический период. "
                    "Полное изображение документа сохраняется; это не посуточная обрезка.",
                    parse_mode=ParseMode.HTML,
                )
        elif action == "gp" and len(parts) == 2:
            if not re.fullmatch(r"\d{1,5}", parts[0]):
                raise ValueError("Invalid offset")
            await _send_period_list(update, context, group, category, int(parts[0]))
        elif action == "gk" and len(parts) == 3:
            if not all(re.fullmatch(r"\d{8}", value) for value in parts[:2]):
                raise ValueError("Invalid period")
            start, end = (datetime.strptime(value, "%Y%m%d").date() for value in parts[:2])
            validate_period(start, end)
            await _send_group_timetable(update, context, group, category=category, period_start=start, period_end=end)
        else:
            raise ValueError("Invalid group action")
    except (ValueError, KeyError, IndexError, OverflowError):
        await _message(update).reply_text(
            "Эта кнопка недоступна. Откройте /mygroup или выберите группу заново.", reply_markup=_category_keyboard(),
        )
    return True


async def handle_group_callback(update, context, data: str) -> bool:
    if await _handle_navigation_callback(update, context, data):
        return True
    if not data.startswith(("gl:", "gg:", "gw:")):
        return False
    try:
        if len(data.encode("utf-8")) > 64:
            raise ValueError("Oversized callback")
        action, category, *parts = data.split(":")
        if category not in CATEGORIES:
            raise ValueError("Unknown category")
        if action == "gl" and len(parts) == 1:
            offset = int(parts[0])
            if offset < 0:
                raise ValueError("Negative offset")
            await _send_group_list(update, context, category, offset)
        elif action in {"gg", "gw"} and len(parts) == 2:
            if not re.fullmatch(r"\d{8}", parts[0]):
                raise ValueError("Invalid period")
            period_start = datetime.strptime(parts[0], "%Y%m%d").date()
            if action == "gg":
                if not looks_like_group(parts[1]):
                    raise ValueError("Invalid group code")
                await _send_group_timetable(update, context, parts[1], category=category, period_start=period_start)
            else:
                offset = int(parts[1])
                if offset < 0:
                    raise ValueError("Negative offset")
                await _send_group_list(update, context, category, offset, period_start)
        else:
            raise ValueError("Invalid group callback")
    except (ValueError, IndexError):
        await _message(update).reply_text(
            "Эта кнопка недоступна. Выберите категорию заново или введите полный код группы.",
            reply_markup=_category_keyboard(),
        )
    return True
