from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .commands import COMMANDS, COMMAND_LANGUAGE_CODES, COMMAND_SCOPE_TYPES
from .abuse import RequestBudget
from .config import Settings, load_settings
from .formatting import (
    format_date,
    format_day_schedule,
    format_next_dates,
    format_short_date,
    format_teacher_schedule,
    format_teachers_list,
    split_long_message,
)
from .group_bot import (
    group_command,
    groups_command,
    handle_group_callback,
    looks_like_group,
    on_group_text,
    send_group_prompt,
)
from .group_schedule import GroupScheduleService
from .schedule import GoogleSheetScheduleRepository, Schedule
from .schedule_image import render_day_schedule_image
from .stats import UserStatsStore, build_user_stats_store
from .security_logging import install_secret_redaction


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
install_secret_redaction()
# HTTPX request URLs include the Telegram bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)
STATS_RECORD_TIMEOUT_SECONDS = 2.0
STATS_READ_TIMEOUT_SECONDS = 3.0
MAX_TEACHER_QUERY_CHARACTERS = 120

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Расписание группы"],
        ["Вчера", "Сегодня", "Завтра"],
        ["Ближайшие даты", "Найти преподавателя"],
        ["Все преподаватели", "Обновить"],
        ["Статистика"],
    ],
    resize_keyboard=True,
    input_field_placeholder="Группа, фамилия или дата",
)


class ScheduleCache:
    def __init__(self, repository: GoogleSheetScheduleRepository, ttl_seconds: int) -> None:
        self.repository = repository
        self.ttl_seconds = ttl_seconds
        self._schedule: Schedule | None = None
        self._loaded_monotonic = 0.0
        self._lock = asyncio.Lock()

    async def get(self, force: bool = False) -> Schedule:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            is_fresh = self._schedule is not None and now - self._loaded_monotonic < self.ttl_seconds
            if is_fresh and not force:
                return self._schedule
            if force and self._schedule is not None and now - self._loaded_monotonic < 20:
                return self._schedule

            schedule = await asyncio.to_thread(self.repository.load)
            self._schedule = schedule
            self._loaded_monotonic = now
            LOGGER.info("Loaded %s schedule entries", len(schedule.entries))
            return schedule


def get_cache(context: ContextTypes.DEFAULT_TYPE) -> ScheduleCache:
    return context.application.bot_data["schedule_cache"]


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def get_user_stats_store(context: ContextTypes.DEFAULT_TYPE) -> UserStatsStore:
    return context.application.bot_data["user_stats_store"]


def timezone_for(timezone_name: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def today_for(context: ContextTypes.DEFAULT_TYPE) -> date:
    return datetime.now(timezone_for(get_settings(context).timezone)).date()


async def track_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.is_bot:
        return
    if update.message is None and update.callback_query is None:
        return

    try:
        await asyncio.wait_for(
            get_user_stats_store(context).record_user(user.id),
            timeout=STATS_RECORD_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        LOGGER.warning("User activity could not be recorded (%s)", type(exc).__name__)


async def guard_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    message = update.effective_message if hasattr(update, "effective_message") else None
    sender_chat = getattr(message, "sender_chat", None)
    if user.is_bot and sender_chat is None:
        raise ApplicationHandlerStop
    budget = context.application.bot_data["request_budget"]
    # Anonymous administrators share Telegram's synthetic bot identity. Use
    # their sending chat so unrelated groups do not share the same budget.
    budget_key = sender_chat.id if user.is_bot else user.id
    if not budget.allow(budget_key):
        # Stop before statistics, document fetching or rendering. Repeated
        # limit responses would themselves amplify a flooding account.
        if budget.should_notify(budget_key):
            notice = "Слишком много запросов. Подождите несколько секунд."
            if update.callback_query is not None:
                await update.callback_query.answer(notice)
            elif update.effective_message is not None:
                await update.effective_message.reply_text(notice)
        raise ApplicationHandlerStop


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Telegram update handler failed (%s)", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Не удалось обработать запрос. Попробуйте чуть позже.", reply_markup=MAIN_KEYBOARD,
            )
        except Exception as exc:
            LOGGER.warning("Could not send an error response (%s)", type(exc).__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>Кабинетный Навигатор</b>\n\n"
        "Показываю расписание учебных групп Прокопьевского филиала КузГТУ "
        "из официальных документов сайта. Нажми <b>«Расписание группы»</b> "
        "или отправь код: <code>ИСт-241.2</code>.\n"
        "Пришлю читаемый фрагмент с предметами и ссылкой на оригинальный PDF.\n\n"
        "Также можно найти преподавателя по фамилии: <code>Григорьев</code>. "
        "Кнопки «Сегодня», «Завтра» и даты показывают занятость компьютерных кабинетов "
        "по данным Google Таблицы.\n"
        "Для даты подходит формат: <code>02.05.2026</code>, а также слова "
        "<code>вчера</code>, <code>сегодня</code>, <code>завтра</code>."
    )
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>Команды</b>\n\n"
        "<b>Расписание учебных групп</b>\n"
        "/group ИСт-241.2 - предметы и пары выбранной группы\n"
        "/groups - список групп по разделам\n\n"
        "<b>Компьютерные кабинеты</b>\n"
        "/today - расписание на сегодня\n"
        "/tomorrow - расписание на завтра\n"
        "/next - ближайшие даты из таблицы\n"
        "/date 02.05.2026 - расписание на дату\n"
        "/date завтра - расписание на относительную дату\n"
        "/teacher Короткова - поиск преподавателя\n"
        "/teachers - список преподавателей и событий\n"
        "/refresh - обновить данные из таблицы\n"
        "/stats - статистика пользователей"
    )
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_date_schedule(update, context, today_for(context))


async def yesterday_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_date_schedule(update, context, today_for(context) - timedelta(days=1))


async def tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_date_schedule(update, context, today_for(context) + timedelta(days=1))


async def date_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args).strip()
    target = parse_user_date(raw, today_for(context))
    if target is None:
        await update.effective_message.reply_text(
            "Напиши дату после команды, например: <code>/date 02.05.2026</code> "
            "или <code>/date завтра</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
        return
    await send_date_schedule(update, context, target)


async def teacher_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        context.user_data["awaiting_teacher"] = True
        await update.effective_message.reply_text(
            "Введите фамилию или часть ФИО преподавателя.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    await send_teacher_schedule(update, context, query)


async def teachers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    schedule = await get_cache(context).get()
    await reply_split(update, format_teachers_list(schedule.teachers))


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_next_dates(update, context)


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    await message.reply_text("Обновляю расписание из Google Таблицы...")
    schedule = await get_cache(context).get(force=True)
    text = (
        "Готово. "
        f"Загружено записей: <b>{len(schedule.entries)}</b>, "
        f"дат: <b>{len(schedule.dates)}</b>."
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings(context)
    user = update.effective_user
    if settings.admin_user_ids and (user is None or user.id not in settings.admin_user_ids):
        await update.effective_message.reply_text(
            "Команда доступна только администратору.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    try:
        stats = await asyncio.wait_for(
            get_user_stats_store(context).get_stats(),
            timeout=STATS_READ_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        LOGGER.warning("User statistics are unavailable (%s)", type(exc).__name__)
        await update.effective_message.reply_text(
            "Статистика временно недоступна. Попробуйте чуть позже.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    def count(value: int) -> str:
        return f"{value:,}".replace(",", " ")

    local_timezone = timezone_for(settings.timezone)
    observed_at = stats.observed_at.astimezone(local_timezone)
    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"Сейчас (за 5 минут): <b>{count(stats.active_now)}</b>\n"
        f"За последние 24 часа: <b>{count(stats.active_24h)}</b>\n"
        f"Всего пользователей: <b>{count(stats.total_users)}</b>\n\n"
        "Каждый человек учитывается один раз в каждом показателе.\n"
        "«Сейчас» означает недавнее обращение к боту.\n\n"
        f"Данные на: <i>{observed_at:%d.%m.%Y %H:%M %Z}</i>"
    )
    if stats.tracking_since is not None:
        tracking_since = stats.tracking_since.astimezone(local_timezone)
        text += f"\nУчёт активности с: <i>{tracking_since:%d.%m.%Y %H:%M %Z}</i>"
        if stats.observed_at - stats.tracking_since < timedelta(hours=24):
            text += "\n<i>Данные за полные 24 часа ещё накапливаются.</i>"
    if not stats.is_persistent:
        text += (
            "\n\n⚠️ <b>Временная статистика</b>\n"
            "Данные хранятся в памяти одного процесса и сбрасываются при перезапуске. "
            "Общее число пользователей может быть неполным."
        )
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    lowered = text.casefold()

    if lowered in {"вчера", "сегодня", "завтра"}:
        target = parse_user_date(text, today_for(context))
        if target:
            await send_date_schedule(update, context, target)
            return
    if lowered == "ближайшие даты":
        await send_next_dates(update, context)
        return
    if lowered in {"найти учителя", "найти преподавателя"}:
        context.user_data["awaiting_teacher"] = True
        await update.effective_message.reply_text(
            "Введите фамилию или часть ФИО преподавателя.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if lowered in {"все учителя", "все преподаватели"}:
        await teachers_command(update, context)
        return
    if lowered == "обновить":
        await refresh_command(update, context)
        return
    if lowered == "статистика":
        await stats_command(update, context)
        return
    if lowered in {"расписание группы", "расписание групп", "моя группа"}:
        context.user_data.pop("awaiting_teacher", None)
        await send_group_prompt(update, context)
        return

    parsed_date = parse_user_date(text, today_for(context))
    if parsed_date:
        context.user_data.pop("awaiting_teacher", None)
        await send_date_schedule(update, context, parsed_date)
        return

    if looks_like_group(text):
        context.user_data.pop("awaiting_teacher", None)
        await on_group_text(update, context)
        return

    context.user_data.pop("awaiting_teacher", None)
    await send_teacher_schedule(update, context, text)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    if await handle_group_callback(update, context, data):
        return
    if data.startswith("date:"):
        try:
            target = date.fromisoformat(data.removeprefix("date:"))
        except ValueError:
            await query.edit_message_text("Не смог разобрать дату.")
            return
        await send_date_schedule(update, context, target, from_callback=True)
        return


async def send_date_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target: date,
    from_callback: bool = False,
) -> None:
    schedule = await get_cache(context).get()
    entries = schedule.entries_for_date(target)
    text = format_day_schedule(target, entries)
    if from_callback and update.callback_query:
        await send_day_schedule_image(update, target, entries, text, reply_markup=None)
        return
    await send_day_schedule_image(update, target, entries, text, reply_markup=MAIN_KEYBOARD)


async def send_day_schedule_image(
    update: Update,
    target: date,
    entries,
    fallback_text: str,
    reply_markup=None,
) -> None:
    try:
        image = await asyncio.to_thread(render_day_schedule_image, target, entries)
        caption = f"Расписание на {format_date(target)}"
        await update.effective_message.reply_photo(
            photo=image,
            caption=caption,
            reply_markup=reply_markup,
        )
        return
    except Exception:
        LOGGER.exception("Failed to render or send schedule image")
    await reply_split(update, fallback_text)


async def send_teacher_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
) -> None:
    if len(query) > MAX_TEACHER_QUERY_CHARACTERS:
        await update.effective_message.reply_text(
            "Слишком длинный запрос. Введите фамилию или часть ФИО (до 120 символов).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    schedule = await get_cache(context).get()
    text = format_teacher_schedule(query, schedule.search_teacher(query), today_for(context))
    await reply_split(update, text)


async def send_next_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    schedule = await get_cache(context).get()
    dates = schedule.next_dates(today_for(context))
    buttons = [
        [InlineKeyboardButton(format_short_date(item), callback_data=f"date:{item.isoformat()}")]
        for item in dates[:10]
    ]
    await update.effective_message.reply_text(
        format_next_dates(dates),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else MAIN_KEYBOARD,
    )


async def reply_split(update: Update, text: str) -> None:
    for index, part in enumerate(split_long_message(text)):
        await update.effective_message.reply_text(
            part,
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD if index == 0 else None,
        )


def parse_user_date(raw: str, today: date) -> date | None:
    text = raw.strip()
    lowered = text.casefold()
    if lowered == "вчера":
        return today - timedelta(days=1)
    if lowered == "сегодня":
        return today
    if lowered == "завтра":
        return today + timedelta(days=1)

    match = re.fullmatch(r"([0-3]?\d)[./-]([01]?\d)(?:[./-](\d{2,4}))?", text)
    if not match:
        return None

    day = int(match.group(1))
    month = int(match.group(2))
    year_raw = match.group(3)
    if year_raw is None:
        year = today.year
    elif len(year_raw) == 2:
        year = 2000 + int(year_raw)
    else:
        year = int(year_raw)

    try:
        return date(year, month, day)
    except ValueError:
        return None


async def set_bot_commands(application: Application) -> None:
    commands = [BotCommand(item["command"], item["description"]) for item in COMMANDS]
    scopes = {
        "default": BotCommandScopeDefault(),
        "all_private_chats": BotCommandScopeAllPrivateChats(),
    }
    for scope_type in COMMAND_SCOPE_TYPES:
        for language_code in COMMAND_LANGUAGE_CODES:
            kwargs = {"scope": scopes[scope_type]}
            if language_code is not None:
                kwargs["language_code"] = language_code
            await application.bot.set_my_commands(commands, **kwargs)


async def post_init(application: Application) -> None:
    await set_bot_commands(application)


def build_application(settings: Settings) -> Application:
    repository = GoogleSheetScheduleRepository(settings.google_sheet_id)
    cache = ScheduleCache(repository, ttl_seconds=settings.cache_minutes * 60)

    application = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
    application.bot_data["settings"] = settings
    application.bot_data["schedule_cache"] = cache
    application.bot_data["user_stats_store"] = build_user_stats_store(settings)
    application.bot_data["group_schedule_service"] = GroupScheduleService()
    application.bot_data["request_budget"] = RequestBudget()

    application.add_handler(TypeHandler(Update, guard_requests), group=-2)
    application.add_handler(TypeHandler(Update, track_usage), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("group", group_command))
    application.add_handler(CommandHandler("groups", groups_command))
    application.add_handler(CommandHandler("yesterday", yesterday_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("tomorrow", tomorrow_command))
    application.add_handler(CommandHandler("date", date_command))
    application.add_handler(CommandHandler("teacher", teacher_command))
    application.add_handler(CommandHandler("teachers", teachers_command))
    application.add_handler(CommandHandler("next", next_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    settings = load_settings()
    application = build_application(settings)
    LOGGER.info("Kabinet Navigator bot is starting")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
