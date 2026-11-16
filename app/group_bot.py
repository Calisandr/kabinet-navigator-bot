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
from .group_schedule import GroupNotFound, GroupScheduleError
from .group_sources import GROUP_SCHEDULE_PAGE_URL


LOGGER = logging.getLogger(__name__)
GROUP_REQUEST_TIMEOUT_SECONDS = 35.0
GROUPS_PER_PAGE = 12
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
    return InlineKeyboardMarkup([[button] for button in buttons]) if buttons else None


async def send_group_prompt(update, context) -> None:
    await _message(update).reply_text(
        "<b>Расписание учебной группы</b>\n\n"
        "Введите полный код группы, например <code>ИСт-241.2</code>, "
        "или отправьте <code>/group ИСт-241.2</code>.\n\n"
        "Можно выбрать категорию и найти группу кнопкой:",
        parse_mode=ParseMode.HTML,
        reply_markup=_category_keyboard(),
    )


async def group_command(update, context) -> None:
    query = " ".join(getattr(context, "args", ()) or ()).strip()
    if not query:
        await send_group_prompt(update, context)
        return
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


async def _send_error(update, exc: Exception, *, query: str | None = None) -> None:
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
    await _message(update).reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


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
    rows = [[button] for option in page if (button := _group_button(option)) is not None]
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
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def _send_group_timetable(update, context, query: str, *, category: str | None = None, period_start: date | None = None) -> None:
    message = _message(update)
    await message.reply_text("Подготавливаю расписание...")
    today = local_today(context)
    try:
        service = context.application.bot_data["group_schedule_service"]
        timetable = await asyncio.wait_for(
            asyncio.to_thread(service.get_timetable, query, today, category=category, period_start=period_start),
            timeout=GROUP_REQUEST_TIMEOUT_SECONDS,
        )
        if len(timetable.images) != 1:
            raise GroupScheduleError(
                "Не удалось подготовить единое изображение группы на весь период. "
                "Откройте оригинальное расписание по ссылке ниже.",
                source_url=timetable.source.url,
            )
    except Exception as exc:
        await _send_error(update, exc, query=query)
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
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Группы этого периода", callback_data=f"gw:{source.category}:{source.start:%Y%m%d}:0")]]
    )
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


async def handle_group_callback(update, context, data: str) -> bool:
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
