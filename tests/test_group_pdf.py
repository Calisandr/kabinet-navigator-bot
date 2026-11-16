from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app import group_pdf
from app.group_pdf import UnsupportedTimetableError, extract_groups, normalize_group, render_group


def make_pdf(content: str, *, width=600, height=1000, pages=1) -> bytes:
    stream = content.encode("ascii")
    stream_id = pages + 4
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4+i} 0 R' for i in range(pages))}] /Count {pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    objects += [
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>".encode()
        for _ in range(pages)
    ]
    objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    result += f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(result)


def text(x, y, value, size=10):
    return f"BT /F1 {size} Tf {x} {y} Td ({value}) Tj ET"


def grid(verticals, horizontal, bottom, top):
    commands = ["0 0 0 RG 1 w"]
    commands += [f"{x} {bottom} m {x} {top} l S" for x in verticals]
    commands += [f"20 {y} m 580 {y} l S" for y in horizontal]
    return commands


DAY_COLORS = [(0, 80 + i * 20, 255 - i * 18) for i in range(7)]
HEADER_COLOR = (25, 60, 40)


@pytest.fixture
def weekly_pdf():
    edges = [930 - i * (880 / 7) for i in range(8)]
    content = []
    for index, (top, bottom) in enumerate(zip(edges, edges[1:])):
        red, green, blue = DAY_COLORS[index]
        content += [
            f"1 0 0 rg 110 {bottom} 215 {top-bottom} re f",
            f"{red/255:.5f} {green/255:.5f} {blue/255:.5f} rg 325 {bottom} 255 {top-bottom} re f",
        ]
    content += [f"{HEADER_COLOR[0]/255:.5f} {HEADER_COLOR[1]/255:.5f} {HEADER_COLOR[2]/255:.5f} rg 325 930 255 20 re f"]
    content += ["0 0 0 rg", text(25, 940, "date", 8), text(65, 940, "time", 8)]
    content += [text(125, 940, "AC-231.2 / AC-241.2"), text(350, 940, "MX-231.2.1"), text(307, 940, "12", 8)]
    for index, top in enumerate(edges[:-1]):
        content += [text(23, top - 30, str(index + 1), 8), text(64, top - 30, "08:30", 8)]
    content += grid([20, 40, 60, 110, 300, 325, 555, 580], [950, *edges], 50, 950)
    return make_pdf("\n".join(content))


@pytest.fixture
def part_time_pdf():
    content = ["0 0 0 rg"]
    for top, first, second in [(950, "AC-231.2", "MX-231.2"), (700, "AC-231.2.1", "HX-251.2")]:
        content += [text(125, top - 13, first), text(350, top - 13, second), text(65, top - 45, "08:30", 8)]
    content += grid([20, 40, 60, 110, 300, 325, 555, 580], [950, 930, 700, 680, 450], 450, 950)
    return make_pdf("\n".join(content))


def test_normalization_preserves_exact_suffixes_and_accepts_homoglyphs():
    assert normalize_group(" ГOc–231.2.1 ") == "ГОС-231.2.1"
    assert normalize_group("ГОс-231") != normalize_group("ГОс-231.2")
    assert normalize_group("ТАт-231.2.1") != normalize_group("ТАт-231.2.2")


def test_exact_group_index_keeps_joint_aliases_and_auditorium_column(weekly_pdf):
    locations = extract_groups(weekly_pdf)
    groups = {normalize_group(location.group): location for location in locations}

    assert set(groups) == {normalize_group(code) for code in ["AC-231.2", "AC-241.2", "MX-231.2.1"]}
    first, alias = groups[normalize_group("AC-231.2")], groups[normalize_group("AC-241.2")]
    assert first.column_left == alias.column_left == pytest.approx(110, abs=1)
    assert first.column_right == alias.column_right == pytest.approx(325, abs=1)
    assert first.service_right == pytest.approx(110, abs=1)
    assert "AC-231.2" in first.heading and "AC-241.2" in first.heading
    second = groups[normalize_group("MX-231.2.1")]
    assert second.column_left == pytest.approx(325, abs=1)
    assert second.column_right == pytest.approx(580, abs=1)


def test_crop_excludes_neighbour_and_keeps_service_strip(weekly_pdf):
    location = next(item for item in extract_groups(weekly_pdf) if item.group == "MX-231.2.1")
    images = [Image.open(part).convert("RGB") for part in render_group(weekly_pdf, location)]
    try:
        assert len(images) == 1
        assert all(900 <= image.width <= 1044 and image.height <= 8000 for image in images)
        all_colors = set()
        for image in images:
            all_colors.update(color for _, color in image.getcolors(image.width * image.height))
            # The original date/time strip occupies a visible area left of the
            # chosen column; cropping just the subject column would lose it.
            service_colors = image.crop((16, 80, 200, image.height - 16)).getcolors(image.width * image.height)
            assert sum(count for count, color in service_colors if max(color) < 50) > 100
        assert (255, 0, 0) not in all_colors
        for color in DAY_COLORS:
            assert color in all_colors
    finally:
        for image in images:
            image.close()


def test_complete_week_is_one_image_with_all_days_and_a_single_header(weekly_pdf):
    location = next(item for item in extract_groups(weekly_pdf) if item.group == "MX-231.2.1")
    parts = render_group(weekly_pdf, location)
    assert len(parts) == 1
    with Image.open(parts[0]) as image:
        image = image.convert("RGB")
        assert image.height > 1500  # A long week must not be divided at the old limit.
        assert image.width + image.height <= group_pdf.MAX_PHOTO_DIMENSION_SUM
        assert max(image.size) / min(image.size) <= group_pdf.MAX_PHOTO_ASPECT_RATIO
        # Each source day occupies one continuous vertical band, in order.
        # No repeated group header or missing day can separate its pixels.
        pixels = image.load()
        header_rows = [y for y in range(image.height) if pixels[image.width - 80, y] == HEADER_COLOR]
        assert header_rows == list(range(header_rows[0], header_rows[-1] + 1))
        assert header_rows[-1] < next(y for y in range(image.height) if pixels[image.width - 80, y] == DAY_COLORS[0])
        previous_bottom = 0
        for color in DAY_COLORS:
            rows = [y for y in range(image.height) if pixels[image.width - 80, y] == color]
            assert rows and rows[0] > previous_bottom
            assert rows == list(range(rows[0], rows[-1] + 1))
            previous_bottom = rows[-1]
        assert parts[0].getbuffer().nbytes < group_pdf.MAX_PHOTO_BYTES


def test_smaller_height_limit_scales_the_complete_week_instead_of_splitting_it(weekly_pdf):
    location = next(item for item in extract_groups(weekly_pdf) if item.group == "MX-231.2.1")
    parts = render_group(weekly_pdf, location, max_height=1000)
    assert len(parts) == 1
    with Image.open(parts[0]) as image:
        assert image.height <= 1000
        colors = {color for _, color in image.convert("RGB").getcolors(image.width * image.height)}
        assert set(DAY_COLORS) <= colors


def test_part_time_index_finds_later_blocks_and_limits_crop_to_one_group(part_time_pdf):
    locations = extract_groups(part_time_pdf)
    assert {item.group for item in locations} == {"AC-231.2", "MX-231.2", "AC-231.2.1", "HX-251.2"}
    assert sum(item.group == "AC-231.2" for item in locations) == 1
    top = next(item for item in locations if item.group == "MX-231.2")
    lower = next(item for item in locations if item.group == "HX-251.2")
    assert lower.table_top < top.table_bottom + 2
    assert lower.service_right == pytest.approx(top.service_right, abs=1)
    parts = render_group(part_time_pdf, lower)
    assert len(parts) == 1
    with Image.open(parts[0]) as image:
        assert image.height < 1100


def test_a_group_location_cannot_be_reused_for_a_changed_pdf(weekly_pdf):
    location = extract_groups(weekly_pdf)[0]
    with pytest.raises(UnsupportedTimetableError, match="different PDF"):
        render_group(weekly_pdf + b"\n%changed", location)


@pytest.mark.parametrize("data", [b"", b"<html>login</html>", b"%PDF-not-a-valid-document"])
def test_invalid_source_fails_instead_of_returning_a_wrong_timetable(data):
    with pytest.raises(UnsupportedTimetableError):
        extract_groups(data)


def test_unknown_layout_fails_with_no_group_crop():
    with pytest.raises(UnsupportedTimetableError, match="No supported group"):
        extract_groups(make_pdf(text(50, 800, "AC-231.2")))


def test_document_limits_are_checked_before_full_indexing(monkeypatch, weekly_pdf):
    monkeypatch.setattr(group_pdf, "MAX_PDF_BYTES", len(weekly_pdf) - 1)
    with pytest.raises(UnsupportedTimetableError, match="too large"):
        extract_groups(weekly_pdf)
    monkeypatch.setattr(group_pdf, "MAX_PDF_BYTES", 20 * 1024 * 1024)
    with pytest.raises(UnsupportedTimetableError, match="number of timetable pages"):
        extract_groups(make_pdf("", pages=41))


def test_output_dimensions_are_bounded(weekly_pdf):
    location = extract_groups(weekly_pdf)[0]
    with pytest.raises(ValueError, match="image dimensions"):
        render_group(weekly_pdf, location, target_width=20_000)
