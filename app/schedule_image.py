from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .formatting import WEEKDAYS, format_lesson_rooms
from .schedule import ScheduleEntry, group_entries_by_teacher, sort_key_ru


WIDTH = 1200
CARD_X = 46
CARD_W = WIDTH - CARD_X * 2
CONTENT_X = CARD_X + 48
CONTENT_W = CARD_W - 96
HERO_H = 236
ROW_GAP = 16

BG_TOP = (228, 235, 246)
BG_BOTTOM = (247, 249, 253)
CARD = (255, 255, 255)
INK = (18, 25, 43)
NAVY = (13, 20, 38)
NAVY_2 = (30, 41, 68)
MUTED = (91, 105, 129)
SOFT = (248, 250, 253)
LINE = (224, 231, 241)
BLUE = (49, 99, 235)
MINT = (20, 184, 166)
GOLD = (218, 166, 71)
CHIP_BLUE = (232, 240, 255)
CHIP_MINT = (222, 250, 243)


def render_day_schedule_image(target: date, entries: Iterable[ScheduleEntry]) -> BytesIO:
    entries = list(entries)
    fonts = FontSet.load()
    rows = build_rows(entries, fonts)
    height = calculate_height(rows, has_entries=bool(entries))

    image = Image.new("RGBA", (WIDTH, height), (255, 255, 255, 255))
    draw_background(image, height)
    draw_card(image, height)

    draw_header(image, target, len(entries), unique_rooms(entries), fonts)
    draw = ImageDraw.Draw(image)

    y = CARD_X + HERO_H + 64
    if entries:
        draw_section_title(draw, y, fonts)
        y += 74
        for index, row in enumerate(rows, start=1):
            y = draw_row(draw, index, row, fonts, y)
            y += ROW_GAP
    else:
        y += 22
        y = draw_empty_state(draw, fonts, y)

    draw_footer(draw, height, fonts)
    output = BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = f"schedule-{target.isoformat()}.png"
    return output


def build_rows(entries: list[ScheduleEntry], fonts: "FontSet") -> list[dict[str, object]]:
    grouped = group_entries_by_teacher(entries)
    rows: list[dict[str, object]] = []
    teacher_width = CONTENT_W - 154
    pill_width = CONTENT_W - 154

    for teacher in sorted(grouped, key=sort_key_ru):
        details = split_details(format_lesson_rooms(grouped[teacher]))
        teacher_lines = wrap_text(teacher, fonts.bold(31), teacher_width)
        pill_lines = layout_pills(details, fonts.semibold(23), pill_width)
        row_height = 88 + len(teacher_lines) * 38 + max(0, len(pill_lines) - 1) * 50
        rows.append(
            {
                "teacher": teacher_lines,
                "pill_lines": pill_lines,
                "height": max(126, row_height),
            }
        )
    return rows


def calculate_height(rows: list[dict[str, object]], has_entries: bool) -> int:
    if not has_entries:
        return 760
    rows_height = sum(int(row["height"]) + ROW_GAP for row in rows)
    return max(820, CARD_X * 2 + HERO_H + 64 + 74 + rows_height + 78)


def unique_rooms(entries: list[ScheduleEntry]) -> int:
    return len({entry.room for entry in entries if entry.room})


def format_compact_date(target: date) -> str:
    return target.strftime("%d.%m")


def split_details(details: str) -> list[str]:
    return [part.strip() for part in details.split(";") if part.strip()]


def draw_background(image: Image.Image, height: int) -> None:
    gradient = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(gradient)
    for y in range(height):
        t = y / max(1, height - 1)
        color = lerp_color(BG_TOP, BG_BOTTOM, t)
        draw.line((0, y, WIDTH, y), fill=color + (255,))
    image.alpha_composite(gradient)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((70, 84, 350, 184), radius=48, fill=(222, 247, 241, 170))
    draw.rounded_rectangle((820, 74, 1118, 176), radius=48, fill=(229, 235, 255, 180))
    draw.rounded_rectangle((892, height - 184, 1138, height - 76), radius=50, fill=(238, 244, 255, 180))


def draw_card(image: Image.Image, height: int) -> None:
    draw_soft_shadow(
        image,
        (CARD_X + 10, CARD_X + 14, CARD_X + CARD_W + 10, height - CARD_X + 14),
        radius=34,
        blur=22,
        color=(15, 23, 42, 34),
    )
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (CARD_X, CARD_X, CARD_X + CARD_W, height - CARD_X),
        radius=34,
        fill=CARD,
        outline=(217, 225, 238),
        width=2,
    )


def draw_header(
    image: Image.Image,
    target: date,
    entry_count: int,
    room_count: int,
    fonts: "FontSet",
) -> None:
    x = CONTENT_X
    y = CARD_X + 34
    w = CONTENT_W
    h = HERO_H
    rounded_gradient(image, (x, y, x + w, y + h), 34, NAVY, NAVY_2)
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((x + w - 338, y + 34, x + w - 34, y + 84), radius=25, fill=(255, 255, 255, 232), outline=(255, 255, 255), width=1)
    source = "Google Таблица"
    source_w = text_width(source, fonts.semibold(21))
    draw.text((x + w - 186 - source_w / 2, y + 46), source, font=fonts.semibold(21), fill=MUTED)
    draw.line((x + 36, y + h - 30, x + w - 36, y + h - 30), fill=(255, 255, 255, 34), width=1)

    date_box = (x + 34, y + 36, x + 230, y + 116)
    draw.rounded_rectangle(date_box, radius=26, fill=(255, 255, 255, 242))
    compact_date = format_compact_date(target)
    date_w = text_width(compact_date, fonts.bold(42))
    draw.text((date_box[0] + (196 - date_w) / 2, y + 49), compact_date, font=fonts.bold(42), fill=BLUE)

    weekday = WEEKDAYS[target.weekday()]
    weekday_box = (x + 34, y + 132, x + 230, y + 178)
    draw.rounded_rectangle(weekday_box, radius=23, fill=(255, 247, 230, 238), outline=(255, 255, 255, 70), width=1)
    weekday_w = text_width(weekday, fonts.semibold(22))
    draw.text((weekday_box[0] + (196 - weekday_w) / 2, y + 140), weekday, font=fonts.semibold(22), fill=(151, 97, 18))

    text_x = x + 266
    draw.text((text_x, y + 36), "Кабинетный Навигатор", font=fonts.semibold(30), fill=(203, 213, 225))
    draw.text((text_x, y + 76), "Расписание дня", font=fonts.bold(54), fill=(255, 255, 255))
    draw.text((text_x, y + 146), f"{target.strftime('%d.%m.%Y')} · пары и кабинеты", font=fonts.semibold(24), fill=(164, 178, 202))

    draw_metric(draw, x + w - 304, y + 122, f"{entry_count}", "записей", fonts, BLUE)
    draw_metric(draw, x + w - 158, y + 122, f"{room_count}", "кабинетов", fonts, MINT)


def draw_metric(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    value: str,
    label: str,
    fonts: "FontSet",
    color: tuple[int, int, int],
) -> None:
    draw.rounded_rectangle((x, y, x + 132, y + 72), radius=24, fill=(255, 255, 255, 234))
    value_w = text_width(value, fonts.bold(30))
    label_w = text_width(label, fonts.semibold(17))
    draw.text((x + (132 - value_w) / 2, y + 9), value, font=fonts.bold(30), fill=color)
    draw.text((x + (132 - label_w) / 2, y + 44), label, font=fonts.semibold(17), fill=MUTED)


def draw_section_title(draw: ImageDraw.ImageDraw, y: int, fonts: "FontSet") -> None:
    draw.text((CONTENT_X, y), "Кабинеты по парам", font=fonts.bold(38), fill=INK)
    subtitle = "преподаватель, пара и аудитория"
    subtitle_w = text_width(subtitle, fonts.semibold(22))
    chip_x = CONTENT_X + CONTENT_W - subtitle_w - 36
    draw.rounded_rectangle((chip_x, y + 4, CONTENT_X + CONTENT_W, y + 44), radius=20, fill=CHIP_MINT)
    draw.text((chip_x + 18, y + 10), subtitle, font=fonts.semibold(22), fill=(13, 116, 103))


def draw_row(
    draw: ImageDraw.ImageDraw,
    index: int,
    row: dict[str, object],
    fonts: "FontSet",
    y: int,
) -> int:
    height = int(row["height"])
    row_box = (CONTENT_X, y, CONTENT_X + CONTENT_W, y + height)
    draw.rounded_rectangle(row_box, radius=24, fill=SOFT, outline=LINE, width=1)
    draw.rounded_rectangle((CONTENT_X + 18, y + 18, CONTENT_X + 82, y + height - 18), radius=22, fill=(235, 242, 255))
    draw.rounded_rectangle((CONTENT_X + 18, y + 18, CONTENT_X + 28, y + height - 18), radius=5, fill=GOLD)
    draw.rounded_rectangle((CONTENT_X + 36, y + 28, CONTENT_X + 72, y + 64), radius=14, fill=(255, 255, 255, 218))
    number = f"{index:02d}"
    number_w = text_width(number, fonts.bold(20))
    draw.text((CONTENT_X + 54 - number_w / 2, y + 35), number, font=fonts.bold(20), fill=BLUE)

    text_x = CONTENT_X + 110
    line_y = y + 24
    for line in row["teacher"]:
        draw.text((text_x, line_y), str(line), font=fonts.bold(31), fill=INK)
        line_y += 38

    line_y += 8
    for pill_line in row["pill_lines"]:
        x = text_x
        for text, width in pill_line:
            draw_pill(draw, x, line_y, text, width, fonts)
            x += width + 10
        line_y += 50

    return y + height


def draw_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    width: int,
    fonts: "FontSet",
) -> None:
    draw.rounded_rectangle((x, y, x + width, y + 38), radius=19, fill=(255, 255, 255), outline=(219, 228, 242), width=1)
    draw.ellipse((x + 15, y + 14, x + 25, y + 24), fill=GOLD)
    if " - " in text:
        lesson_text, room_text = text.split(" - ", 1)
        draw.text((x + 36, y + 7), lesson_text, font=fonts.semibold(22), fill=INK)
        lesson_w = text_width(lesson_text, fonts.semibold(22))
        draw.text((x + 46 + lesson_w, y + 7), room_text, font=fonts.semibold(22), fill=BLUE)
    else:
        draw.text((x + 36, y + 7), text, font=fonts.semibold(22), fill=BLUE)


def draw_empty_state(draw: ImageDraw.ImageDraw, fonts: "FontSet", y: int) -> int:
    box_h = 270
    draw.rounded_rectangle((CONTENT_X, y, CONTENT_X + CONTENT_W, y + box_h), radius=28, fill=SOFT, outline=LINE, width=1)
    draw.rounded_rectangle((CONTENT_X + 40, y + 48, CONTENT_X + 116, y + 124), radius=26, fill=CHIP_BLUE)
    draw.text((CONTENT_X + 66, y + 56), "i", font=fonts.bold(44), fill=BLUE)
    draw.text((CONTENT_X + 148, y + 48), "На эту дату кабинеты не указаны", font=fonts.bold(34), fill=INK)
    message = "Возможно, занятий нет или расписание еще не заполнено в таблице."
    for index, line in enumerate(wrap_text(message, fonts.regular(28), CONTENT_W - 206)):
        draw.text((CONTENT_X + 148, y + 104 + index * 38), line, font=fonts.regular(28), fill=MUTED)
    return y + box_h


def draw_footer(draw: ImageDraw.ImageDraw, height: int, fonts: "FontSet") -> None:
    y = height - CARD_X - 48
    draw.line((CONTENT_X, y - 20, CONTENT_X + CONTENT_W, y - 20), fill=LINE, width=1)
    draw.text((CONTENT_X, y), "Данные из Google Таблицы", font=fonts.regular(22), fill=MUTED)
    footer = "kabinet-navigator-bot"
    footer_w = text_width(footer, fonts.semibold(22))
    draw.text((CONTENT_X + CONTENT_W - footer_w, y), footer, font=fonts.semibold(22), fill=BLUE)


def layout_pills(
    details: list[str],
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[list[tuple[str, int]]]:
    lines: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    current_width = 0
    for detail in details:
        width = min(max_width, text_width(detail, font) + 36)
        next_width = width if not current else current_width + 10 + width
        if current and next_width > max_width:
            lines.append(current)
            current = []
            current_width = 0
        current.append((detail, width))
        current_width = width if current_width == 0 else current_width + 10 + width
    if current:
        lines.append(current)
    return lines or [[("пары не указаны", text_width("пары не указаны", font) + 36)]]


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_width(candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def rounded_gradient(
    image: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    start: tuple[int, int, int],
    end: tuple[int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    w = x2 - x1
    h = y2 - y1
    gradient = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(gradient)
    for x in range(w):
        t = x / max(1, w - 1)
        color = lerp_color(start, end, t)
        draw.line((x, 0, x, h), fill=color + (255,))
    mask = Image.new("L", (w, h), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    image.paste(gradient, (x1, y1), mask)


def draw_soft_shadow(
    image: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    blur: int,
    color: tuple[int, int, int, int],
) -> None:
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    draw.rounded_rectangle(box, radius=radius, fill=color)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    image.alpha_composite(shadow)


def lerp_color(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(start[i] + (end[i] - start[i]) * t) for i in range(3))


def text_width(text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


class FontSet:
    def __init__(self, regular_path: Path, bold_path: Path) -> None:
        self.regular_path = regular_path
        self.bold_path = bold_path
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    @classmethod
    def load(cls) -> "FontSet":
        root = Path(__file__).resolve().parents[1]
        fonts_dir = root / "assets" / "fonts"
        regular = fonts_dir / "NotoSans-Regular.ttf"
        bold = fonts_dir / "NotoSans-Bold.ttf"
        if regular.exists() and bold.exists():
            return cls(regular, bold)

        fallback_regular = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        fallback_bold = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        if fallback_regular.exists() and fallback_bold.exists():
            return cls(fallback_regular, fallback_bold)

        windows_regular = Path("C:/Windows/Fonts/arial.ttf")
        windows_bold = Path("C:/Windows/Fonts/arialbd.ttf")
        if windows_regular.exists() and windows_bold.exists():
            return cls(windows_regular, windows_bold)

        raise RuntimeError("No TrueType font with Cyrillic support found.")

    def regular(self, size: int) -> ImageFont.FreeTypeFont:
        return self._font("regular", self.regular_path, size)

    def semibold(self, size: int) -> ImageFont.FreeTypeFont:
        return self._font("bold", self.bold_path, size)

    def bold(self, size: int) -> ImageFont.FreeTypeFont:
        return self._font("bold", self.bold_path, size)

    def _font(self, name: str, path: Path, size: int) -> ImageFont.FreeTypeFont:
        key = (name, size)
        if key not in self._cache:
            self._cache[key] = ImageFont.truetype(str(path), size=size)
        return self._cache[key]
