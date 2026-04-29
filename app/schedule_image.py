from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .formatting import format_date, format_lesson_rooms
from .schedule import ScheduleEntry, group_entries_by_teacher, sort_key_ru


WIDTH = 1200
MARGIN = 56
CARD_RADIUS = 32
CARD_X = 44
CARD_W = WIDTH - CARD_X * 2
CONTENT_X = CARD_X + 48
CONTENT_W = CARD_W - 96

BG = (239, 244, 250)
CARD = (255, 255, 255)
TEXT = (25, 35, 55)
MUTED = (91, 107, 128)
LINE = (225, 232, 242)
BLUE = (37, 99, 235)
TEAL = (20, 184, 166)
SOFT_BLUE = (226, 236, 255)
SOFT_TEAL = (220, 252, 243)


def render_day_schedule_image(target: date, entries: Iterable[ScheduleEntry]) -> BytesIO:
    entries = list(entries)
    fonts = FontSet.load()
    rows = build_rows(entries, fonts)
    height = calculate_height(rows, has_entries=bool(entries), fonts=fonts)

    image = Image.new("RGBA", (WIDTH, height), BG + (255,))
    draw = ImageDraw.Draw(image)
    draw_background(draw, height)
    draw_card(image, height)
    draw = ImageDraw.Draw(image)

    y = CARD_X + 44
    draw_header(draw, target, fonts, y)
    y += 154

    if entries:
        draw.text((CONTENT_X, y), "Кабинеты по парам", font=fonts.semibold(34), fill=TEXT)
        y += 58
        for row in rows:
            y = draw_row(draw, row, fonts, y)
            y += 18
    else:
        y += 18
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
    for teacher in sorted(grouped, key=sort_key_ru):
        details = format_lesson_rooms(grouped[teacher])
        teacher_lines = wrap_text(teacher, fonts.bold(30), CONTENT_W - 48)
        detail_lines = wrap_text(details, fonts.regular(28), CONTENT_W - 48)
        row_height = 34 + len(teacher_lines) * 38 + len(detail_lines) * 36 + 26
        rows.append(
            {
                "teacher": teacher_lines,
                "details": detail_lines,
                "height": max(122, row_height),
            }
        )
    return rows


def calculate_height(rows: list[dict[str, object]], has_entries: bool, fonts: "FontSet") -> int:
    if not has_entries:
        return 700
    rows_height = sum(int(row["height"]) + 18 for row in rows)
    return max(760, CARD_X * 2 + 154 + 58 + rows_height + 96)


def draw_background(draw: ImageDraw.ImageDraw, height: int) -> None:
    draw.rectangle((0, 0, WIDTH, height), fill=BG)
    draw.rounded_rectangle((64, 56, 330, 190), radius=44, fill=(230, 249, 247))
    draw.rounded_rectangle((852, 64, 1118, 178), radius=44, fill=(229, 238, 255))
    draw.rounded_rectangle((916, height - 210, 1144, height - 78), radius=46, fill=(237, 245, 255))


def draw_card(image: Image.Image, height: int) -> None:
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (CARD_X + 8, CARD_X + 10, CARD_X + CARD_W + 8, height - CARD_X + 10),
        radius=CARD_RADIUS,
        fill=(25, 35, 55, 26),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    image.alpha_composite(shadow)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (CARD_X, CARD_X, CARD_X + CARD_W, height - CARD_X),
        radius=CARD_RADIUS,
        fill=CARD,
        outline=(222, 230, 242),
        width=2,
    )


def draw_header(draw: ImageDraw.ImageDraw, target: date, fonts: "FontSet", y: int) -> None:
    draw.rounded_rectangle((CONTENT_X, y, CONTENT_X + 112, y + 112), radius=28, fill=SOFT_BLUE)
    draw.text((CONTENT_X + 28, y + 18), target.strftime("%d"), font=fonts.bold(44), fill=BLUE)
    draw.text((CONTENT_X + 33, y + 67), target.strftime("%m"), font=fonts.semibold(24), fill=TEAL)

    text_x = CONTENT_X + 142
    draw.text((text_x, y + 4), "Кабинетный Навигатор", font=fonts.semibold(30), fill=MUTED)
    draw.text((text_x, y + 44), format_date(target), font=fonts.bold(46), fill=TEXT)
    draw.rounded_rectangle((text_x, y + 102, text_x + 214, y + 138), radius=18, fill=SOFT_TEAL)
    draw.text((text_x + 18, y + 107), "расписание дня", font=fonts.semibold(20), fill=(17, 126, 111))


def draw_row(draw: ImageDraw.ImageDraw, row: dict[str, object], fonts: "FontSet", y: int) -> int:
    height = int(row["height"])
    draw.rounded_rectangle((CONTENT_X, y, CONTENT_X + CONTENT_W, y + height), radius=22, fill=(248, 251, 255))
    draw.rectangle((CONTENT_X, y + 22, CONTENT_X + 7, y + height - 22), fill=BLUE)

    text_x = CONTENT_X + 28
    line_y = y + 24
    for line in row["teacher"]:
        draw.text((text_x, line_y), str(line), font=fonts.bold(30), fill=TEXT)
        line_y += 38

    line_y += 8
    for line in row["details"]:
        draw.text((text_x, line_y), str(line), font=fonts.regular(28), fill=MUTED)
        line_y += 36

    draw.line((CONTENT_X + 24, y + height, CONTENT_X + CONTENT_W - 24, y + height), fill=LINE, width=1)
    return y + height


def draw_empty_state(draw: ImageDraw.ImageDraw, fonts: "FontSet", y: int) -> int:
    box_h = 260
    draw.rounded_rectangle((CONTENT_X, y, CONTENT_X + CONTENT_W, y + box_h), radius=26, fill=(248, 251, 255))
    draw.rounded_rectangle((CONTENT_X + 40, y + 48, CONTENT_X + 112, y + 120), radius=22, fill=SOFT_BLUE)
    draw.text((CONTENT_X + 62, y + 56), "i", font=fonts.bold(42), fill=BLUE)
    draw.text((CONTENT_X + 142, y + 48), "На эту дату кабинеты не указаны", font=fonts.bold(34), fill=TEXT)
    message = "Возможно, занятий нет или расписание еще не заполнено в таблице."
    for index, line in enumerate(wrap_text(message, fonts.regular(28), CONTENT_W - 190)):
        draw.text((CONTENT_X + 142, y + 102 + index * 36), line, font=fonts.regular(28), fill=MUTED)
    return y + box_h


def draw_footer(draw: ImageDraw.ImageDraw, height: int, fonts: "FontSet") -> None:
    y = height - CARD_X - 48
    draw.line((CONTENT_X, y - 20, CONTENT_X + CONTENT_W, y - 20), fill=LINE, width=1)
    draw.text((CONTENT_X, y), "Данные из Google Таблицы", font=fonts.regular(22), fill=MUTED)
    footer = "kabinet-navigator-bot"
    footer_w = text_width(footer, fonts.semibold(22))
    draw.text((CONTENT_X + CONTENT_W - footer_w, y), footer, font=fonts.semibold(22), fill=BLUE)


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
