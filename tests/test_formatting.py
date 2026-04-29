from __future__ import annotations

from datetime import date

from app.formatting import compress_lessons, format_day_schedule
from app.schedule import ScheduleEntry


def test_compress_lessons_uses_pairs_wording() -> None:
    assert compress_lessons(["1"]) == "1-я пара"
    assert compress_lessons(["1", "2"]) == "1-2 пары"
    assert compress_lessons(["1", "3"]) == "1-я, 3-я пары"
    assert compress_lessons([]) == "пары не указаны"


def test_day_schedule_uses_pairs_not_lessons() -> None:
    text = format_day_schedule(
        date(2026, 5, 2),
        [
            ScheduleEntry(
                day=date(2026, 5, 2),
                teacher="Григорьев М.Ю.",
                lesson="1",
                room="321",
                sheet_name="МАЙ 2026",
                row_number=7,
            )
        ],
    )

    assert "1-я пара - каб. 321" in text
    assert "урок" not in text.lower()
