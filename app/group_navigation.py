"""Small, stateless group navigation payloads and explicit calendar ranges."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta


CATEGORY_CODES = {"vo": "v", "spo": "s", "part_time": "p"}
CATEGORY_NAMES = {value: key for key, value in CATEGORY_CODES.items()}
_DATE = re.compile(r"(?<!\d)\d{1,2}[./]\d{1,2}[./]\d{4}(?!\d)")
_WEEKS = {
    "current": 0, "this": 0, "текущая": 0, "текущая неделя": 0, "эта неделя": 0,
    "previous": -1, "prev": -1, "last": -1, "прошлая": -1, "прошлая неделя": -1,
    "next": 1, "следующая": 1, "следующая неделя": 1,
}


def week_bounds(day: date, offset: int = 0) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday()) + timedelta(weeks=offset)
    return start, start + timedelta(days=6)


def validate_period(start: date, end: date) -> tuple[date, date]:
    if end < start or (end - start).days >= 31:
        raise ValueError("Выберите период от 1 до 31 дня: начальная дата не позже конечной.")
    return start, end


def parse_period(value: str, today: date) -> tuple[date, date]:
    """Accept a named week or two dates, never infer a year or swap dates."""
    raw = value.strip().casefold()
    if raw in _WEEKS:
        return week_bounds(today, _WEEKS[raw])
    matches = list(_DATE.finditer(raw))
    if len(matches) != 2:
        raise ValueError("Укажите неделю или две даты: 16.11.2026 22.11.2026.")
    remainder = _DATE.sub("", raw)
    if re.fullmatch(r"[\s—–\-]*(?:по|до)?[\s—–\-]*", remainder) is None:
        raise ValueError("Укажите только две даты: 16.11.2026 22.11.2026.")
    try:
        start, end = (datetime.strptime(item.group().replace("/", "."), "%d.%m.%Y").date() for item in matches)
    except ValueError:
        raise ValueError("Проверьте даты: нужен формат ДД.ММ.ГГГГ.") from None
    return validate_period(start, end)


def group_request(value: str, today: date) -> tuple[str, date | None, date | None]:
    """Existing group-only requests stay unchanged; a suffix selects a period."""
    text = value.strip()
    if len(text) > 160:
        raise ValueError("Слишком длинный запрос. Укажите код группы и, при необходимости, две даты.")
    first_date = _DATE.search(text)
    if first_date is not None:
        group = re.sub(r"\s+с$", "", text[:first_date.start()].strip(), flags=re.IGNORECASE)
        if not group:
            raise ValueError("Сначала укажите группу, например /group ИСт-241.2 next.")
        start, end = parse_period(text[first_date.start():], today)
        return group, start, end
    for suffix in sorted(_WEEKS, key=len, reverse=True):
        if text.casefold().endswith(" " + suffix):
            start, end = parse_period(suffix, today)
            return text[:-len(suffix)].strip(), start, end
    return text, None, None


def group_payload(action: str, category: str, group: str, *values: str) -> str:
    payload = ":".join((action, CATEGORY_CODES[category], *values, group))
    if len(payload.encode("utf-8")) > 64:
        raise ValueError("Group navigation payload is too long")
    return payload


def range_payload(category: str, group: str, start: date, end: date) -> str:
    return group_payload("gk", category, group, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))


def short_period(start: date, end: date) -> str:
    if start.year != end.year:
        return f"{start:%d.%m.%y}–{end:%d.%m.%y}"
    if start.month != end.month:
        return f"{start:%d.%m}–{end:%d.%m}"
    return f"{start:%d}–{end:%d.%m}"
