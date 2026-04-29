from __future__ import annotations

from collections import defaultdict
from datetime import date
from html import escape
from typing import Iterable

from .schedule import ScheduleEntry, group_entries_by_teacher, lesson_sort_key, sort_key_ru


WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}

SHORT_WEEKDAYS = {
    0: "пн",
    1: "вт",
    2: "ср",
    3: "чт",
    4: "пт",
    5: "сб",
    6: "вс",
}


def format_date(target: date, with_weekday: bool = True) -> str:
    base = target.strftime("%d.%m.%Y")
    if not with_weekday:
        return base
    return f"{base}, {WEEKDAYS[target.weekday()]}"


def format_short_date(target: date) -> str:
    return f"{target.strftime('%d.%m')} {SHORT_WEEKDAYS[target.weekday()]}"


def format_day_schedule(target: date, entries: Iterable[ScheduleEntry]) -> str:
    entries = list(entries)
    title = f"<b>{escape(format_date(target))}</b>"
    if not entries:
        return (
            f"{title}\n\n"
            "В таблице на эту дату кабинеты не указаны. Возможно, занятий нет "
            "или расписание еще не заполнено."
        )

    lines = [title, "", "Кабинеты:"]
    grouped = group_entries_by_teacher(entries)
    for teacher in sorted(grouped, key=sort_key_ru):
        lesson_text = format_lesson_rooms(grouped[teacher])
        lines.append(f"• <b>{escape(teacher)}</b> - {escape(lesson_text)}")

    lines.append("")
    lines.append("Предметы в таблице не указаны, поэтому показываю пары и кабинеты.")
    return "\n".join(lines)


def format_teacher_schedule(
    teacher_query: str,
    entries: Iterable[ScheduleEntry],
    today: date,
    limit: int = 30,
) -> str:
    entries = list(entries)
    if not entries:
        return (
            f"По запросу <b>{escape(teacher_query)}</b> ничего не нашлось.\n\n"
            "Попробуй ввести только фамилию, например: <code>Короткова</code>."
        )

    by_day: dict[date, list[ScheduleEntry]] = defaultdict(list)
    for entry in entries:
        by_day[entry.day].append(entry)

    upcoming_days = [day for day in sorted(by_day) if day >= today]
    past_days = [day for day in sorted(by_day) if day < today]
    ordered_days = (upcoming_days + past_days)[:limit]

    teacher_names = sorted({entry.teacher for entry in entries}, key=sort_key_ru)
    heading = ", ".join(teacher_names[:3])
    if len(teacher_names) > 3:
        heading += f" и еще {len(teacher_names) - 3}"

    lines = [f"<b>{escape(heading)}</b>", ""]
    for day in ordered_days:
        lesson_text = format_lesson_rooms(by_day[day])
        lines.append(f"• {escape(format_date(day))}: {escape(lesson_text)}")

    remaining = len(by_day) - len(ordered_days)
    if remaining > 0:
        lines.append("")
        lines.append(f"Показал ближайшие {len(ordered_days)} дат, еще {remaining} скрыто.")
    return "\n".join(lines)


def format_teachers_list(teachers: list[str], limit: int = 120) -> str:
    if not teachers:
        return "Список учителей пока пуст: таблица не загрузилась или не заполнена."

    shown = teachers[:limit]
    lines = ["<b>Учителя и события в таблице</b>", ""]
    lines.extend(f"• {escape(item)}" for item in shown)
    if len(teachers) > limit:
        lines.append("")
        lines.append(f"И еще {len(teachers) - limit}. Для поиска просто отправь фамилию.")
    return "\n".join(lines)


def format_next_dates(dates: list[date]) -> str:
    if not dates:
        return "Ближайших дат в таблице не нашлось."

    lines = ["<b>Ближайшие даты</b>", ""]
    lines.extend(f"• {escape(format_date(day))}" for day in dates)
    lines.append("")
    lines.append("Нажми кнопку с датой или отправь дату текстом: <code>02.05.2026</code>.")
    return "\n".join(lines)


def format_lesson_rooms(entries: Iterable[ScheduleEntry]) -> str:
    by_room: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        by_room[entry.room].append(entry.lesson)

    chunks: list[str] = []
    def group_key(room: str) -> tuple[tuple[int, str], tuple[int, str]]:
        lessons = sorted(set(by_room[room]), key=lesson_sort_key)
        first_lesson = lesson_sort_key(lessons[0]) if lessons else (999, "")
        return first_lesson, room_sort_key(room)

    for room in sorted(by_room, key=group_key):
        lessons = sorted(set(by_room[room]), key=lesson_sort_key)
        lesson_label = compress_lessons(lessons)
        chunks.append(f"{lesson_label} - каб. {room}")
    return "; ".join(chunks)


def compress_lessons(lessons: list[str]) -> str:
    if not lessons:
        return "пары не указаны"

    numeric = [int(item) for item in lessons if item.isdigit()]
    if len(numeric) != len(lessons):
        word = "пара" if len(lessons) == 1 else "пары"
        return f"{', '.join(lessons)} {word}"

    ranges: list[str] = []
    start = prev = numeric[0]
    for item in numeric[1:]:
        if item == prev + 1:
            prev = item
            continue
        ranges.append(format_range(start, prev))
        start = prev = item
    ranges.append(format_range(start, prev))
    label = ", ".join(ranges)
    word = "пара" if len(numeric) == 1 else "пары"
    return f"{label} {word}"


def format_range(start: int, end: int) -> str:
    if start == end:
        return f"{start}-я"
    return f"{start}-{end}"


def room_sort_key(room: str) -> tuple[int, str]:
    digits = "".join(char for char in room if char.isdigit())
    if digits:
        return int(digits), room
    return 9999, room


def split_long_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            parts.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        parts.append("\n".join(current))
    return parts
