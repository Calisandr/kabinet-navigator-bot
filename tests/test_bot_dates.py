from __future__ import annotations

from datetime import date

from app.bot import parse_user_date


def test_parse_user_date_supports_relative_words() -> None:
    today = date(2026, 4, 29)

    assert parse_user_date("вчера", today) == date(2026, 4, 28)
    assert parse_user_date("сегодня", today) == date(2026, 4, 29)
    assert parse_user_date("завтра", today) == date(2026, 4, 30)


def test_parse_user_date_supports_numeric_dates() -> None:
    assert parse_user_date("02.05.2026", date(2026, 4, 29)) == date(2026, 5, 2)
    assert parse_user_date("02.05", date(2026, 4, 29)) == date(2026, 5, 2)
