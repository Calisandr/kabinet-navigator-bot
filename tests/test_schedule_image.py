from __future__ import annotations

from datetime import date

from PIL import Image

from app.schedule import ScheduleEntry
from app.schedule_image import render_day_schedule_image


def test_render_day_schedule_image_returns_png() -> None:
    image_bytes = render_day_schedule_image(
        date(2026, 5, 2),
        [
            ScheduleEntry(
                day=date(2026, 5, 2),
                teacher="Короткова Е.В.",
                lesson="1",
                room="219",
                sheet_name="МАЙ 2026",
                row_number=9,
            )
        ],
    )

    assert image_bytes.getvalue().startswith(b"\x89PNG")
    image = Image.open(image_bytes)
    assert image.size[0] == 1200
    assert image.size[1] >= 700
