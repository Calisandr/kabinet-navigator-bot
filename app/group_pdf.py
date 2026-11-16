"""Find group columns and render legible excerpts of official timetable PDFs."""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from io import BytesIO
from statistics import median

import pypdfium2 as pdfium
from PIL import Image


MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 40
MAX_PAGE_CHARACTERS = 100_000
TARGET_IMAGE_WIDTH = 1040
MAX_IMAGE_HEIGHT = 1500
_PDFIUM_LOCK = threading.RLock()
_GROUP_PATTERN = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9])([A-Za-zА-Яа-яЁё]{1,8}[-\u2010-\u2015\u2212]\d{2,4}(?:\.\d{1,3}){0,3})(?![\d.])"
)
_HOMOGLYPHS = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")


class UnsupportedTimetableError(ValueError):
    """The document cannot be cropped safely into a single group's timetable."""


@dataclass(frozen=True)
class GroupLocation:
    group: str
    page_index: int
    column_left: float
    column_right: float
    table_left: float
    table_right: float
    table_top: float
    table_bottom: float
    service_right: float
    heading: str
    day_edges: tuple[float, ...]
    pdf_sha256: str


def normalize_group(value: str) -> str:
    """Normalize visually interchangeable letters while preserving group suffixes."""
    return re.sub(r"\s+", "", value).upper().translate(_HOMOGLYPHS).translate(
        str.maketrans({char: "-" for char in "‐‑‒–—―−"})
    )


def _validate_bytes(pdf_bytes: bytes) -> None:
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise UnsupportedTimetableError("The timetable source is not a PDF document")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise UnsupportedTimetableError("The timetable PDF is too large")


def _cluster(values: list[float], tolerance: float = 1.3) -> tuple[float, ...]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return tuple(sum(group) / len(group) for group in groups)


def _search_box(textpage, words: tuple[str, ...]) -> tuple[float, float, float, float] | None:
    for word in words:
        search = textpage.search(word)
        try:
            hit = search.get_next()
        finally:
            search.close()
        if hit:
            index, count = hit
            boxes = [textpage.get_charbox(char) for char in range(index, index + count)]
            return min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)
    return None


def _header_text(textpage, date_box) -> tuple[str, list[tuple[float, float, float, float] | None]]:
    band = max(4.0, (date_box[3] - date_box[1]) * 1.4)
    low, high = date_box[1] - band, date_box[3] + band
    characters = []
    for index in range(textpage.count_chars()):
        codepoint = pdfium.raw.FPDFText_GetUnicode(textpage.raw, index)
        if not 0 < codepoint <= 0x10FFFF:
            continue
        character = chr(codepoint)
        if character.isspace():
            continue
        box = textpage.get_charbox(index)
        if low <= (box[1] + box[3]) / 2 <= high and box[2] > box[0]:
            characters.append((box[0], character, box))
    characters.sort(key=lambda item: item[0])
    spacing_threshold = max(2.5, median(item[2][3] - item[2][1] for item in characters) * 0.60) if characters else 2.5
    text: list[str] = []
    boxes: list[tuple[float, float, float, float] | None] = []
    previous_right: float | None = None
    for _, character, box in characters:
        if previous_right is not None and box[0] - previous_right > spacing_threshold:
            text.append(" ")
            boxes.append(None)
        text.append(character)
        boxes.append(box)
        previous_right = max(previous_right or box[2], box[2])
    return "".join(text), boxes


def _table_geometry(page):
    width, height = page.get_size()
    verticals = []
    paths = []
    for obj in page.get_objects():
        if obj.type != pdfium.raw.FPDF_PAGEOBJ_PATH:
            continue
        left, bottom, right, top = obj.get_bounds()
        paths.append((left, bottom, right, top))
        if top - bottom >= height * 0.45 and right - left <= 4:
            verticals.append(((left + right) / 2, bottom, top))
    x_edges = _cluster([item[0] for item in verticals])
    if len(x_edges) < 6:
        return None
    table_left, table_right = x_edges[0], x_edges[-1]
    table_bottom = min(item[1] for item in verticals)
    table_top = max(item[2] for item in verticals)
    horizontal = _cluster([
        (bottom + top) / 2
        for left, bottom, right, top in paths
        if right - left >= (table_right - table_left) * 0.98 and top - bottom <= 4
    ])
    return x_edges, table_bottom, table_top, horizontal


def _group_cells(candidates, x_edges):
    cells = {}
    table_width = x_edges[-1] - x_edges[0]
    for group, left, right in candidates:
        left_edges = [x for x in x_edges if x < left]
        right_edges = [x for x in x_edges if x > right]
        if not left_edges or not right_edges:
            continue
        cell_left, subject_right = max(left_edges), min(right_edges)
        subject_width = subject_right - cell_left
        if subject_width < 45 or subject_width > table_width * 0.65:
            continue
        cell_right = subject_right
        following = [x for x in x_edges if x > subject_right + 0.1]
        # Include the narrow auditorium column, not the neighbouring group.
        if following and following[0] - subject_right <= subject_width * 0.30:
            cell_right = following[0]
        cells.setdefault((cell_left, cell_right), []).append(group)
    return cells


def _locations_from_cells(cells, page_index, digest, x_edges, table_top, table_bottom, day_edges, service_right=None):
    if not cells:
        return ()
    if service_right is None:
        service_right = min(left for left, _ in cells)
    if service_right - x_edges[0] > (x_edges[-1] - x_edges[0]) * 0.25:
        return ()
    result = []
    for (cell_left, cell_right), groups in cells.items():
        heading = " / ".join(dict.fromkeys(groups))
        seen = set()
        for group in groups:
            normalized = normalize_group(group)
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(GroupLocation(
                group=group, page_index=page_index, column_left=cell_left,
                column_right=cell_right, table_left=x_edges[0], table_right=x_edges[-1],
                table_top=table_top, table_bottom=table_bottom,
                service_right=service_right, heading=heading, day_edges=day_edges,
                pdf_sha256=digest,
            ))
    return tuple(result)


def _block_locations(page, page_index: int, digest: str) -> tuple[GroupLocation, ...]:
    """Handle the repeated one-day group cards used by the part-time faculty."""
    width, height = page.get_size()
    if page.get_rotation() != 0 or width <= 0 or height <= 0 or width > 4000 or height > 4000:
        return ()
    geometry = _table_geometry(page)
    if geometry is None:
        return ()
    x_edges, table_bottom, _, horizontal = geometry
    textpage = page.get_textpage()
    try:
        if textpage.count_chars() > MAX_PAGE_CHARACTERS:
            return ()
        codes = dict.fromkeys(match.group(1) for match in _GROUP_PATTERN.finditer(textpage.get_text_range()))
        rows = {}
        for group in codes:
            search = textpage.search(group)
            try:
                while hit := search.get_next():
                    index, count = hit
                    before = textpage.get_text_range(index - 1, 1) if index else ""
                    after = textpage.get_text_range(index + count, 1) if index + count < textpage.count_chars() else ""
                    if (before and before[-1].isalnum()) or (after and (after[0].isdigit() or after[0] == ".")):
                        continue
                    boxes = [textpage.get_charbox(i) for i in range(index, index + count)]
                    left, right = min(b[0] for b in boxes), max(b[2] for b in boxes)
                    bottom, top = min(b[1] for b in boxes), max(b[3] for b in boxes)
                    upper = [y for y in horizontal if y > top]
                    lower = [y for y in horizontal if y < bottom]
                    if not upper or not lower:
                        continue
                    header_top, header_bottom = min(upper), max(lower)
                    if not 4 <= header_top - header_bottom <= 30:
                        continue
                    body_bottoms = [y for y in horizontal if y < header_bottom - 1]
                    if not body_bottoms:
                        continue
                    body_bottom = max(body_bottoms)
                    if not 30 <= header_bottom - body_bottom <= page.get_height() * 0.35:
                        continue
                    rows.setdefault((header_top, header_bottom, body_bottom), []).append((group, left, right))
            finally:
                search.close()
        # The service strip belongs to the table, even if a row has only a
        # single filled group in the second or third column.
        all_subject_lefts = [
            max(x for x in x_edges if x < left)
            for values in rows.values() for _, left, _ in values
            if any(x < left for x in x_edges)
        ]
        result = []
        for (top, header_bottom, bottom), candidates in rows.items():
            cells = _group_cells(candidates, x_edges)
            row_locations = _locations_from_cells(
                cells, page_index, digest, x_edges, top, bottom,
                (header_bottom, max(table_bottom, bottom)), min(all_subject_lefts),
            )
            result.extend(row_locations)
        return tuple(result)
    finally:
        textpage.close()


def _page_locations(page, page_index: int, digest: str) -> tuple[GroupLocation, ...]:
    width, height = page.get_size()
    if page.get_rotation() != 0 or width <= 0 or height <= 0 or width > 4000 or height > 4000:
        return ()
    textpage = page.get_textpage()
    try:
        if textpage.count_chars() > MAX_PAGE_CHARACTERS:
            return ()
        date_box = _search_box(textpage, ("дата", "date"))
        time_box = _search_box(textpage, ("время", "time"))
        if date_box is None or time_box is None:
            return ()
        header_text, header_boxes = _header_text(textpage, date_box)
        matches = list(_GROUP_PATTERN.finditer(header_text))
        if not matches:
            return ()

        geometry = _table_geometry(page)
        if geometry is None:
            return ()
        x_edges, table_bottom, table_top, horizontal = geometry
        table_left, table_right = x_edges[0], x_edges[-1]
        header_bottoms = [y for y in horizontal if y < date_box[1] - 0.5]
        if not header_bottoms:
            return ()
        header_bottom = max(header_bottoms)
        day_edges = tuple(sorted((y for y in horizontal if table_bottom <= y <= header_bottom + 0.1), reverse=True))
        if not 2 <= len(day_edges) <= 10:
            return ()

        candidates = []
        for match in matches:
            boxes = [box for box in header_boxes[match.start(1):match.end(1)] if box is not None]
            if not boxes:
                continue
            left = min(box[0] for box in boxes)
            right = max(box[2] for box in boxes)
            candidates.append((match.group(1), left, right))
        cells = _group_cells(candidates, x_edges)
        if not cells:
            return ()
        service_right = min(left for left, _ in cells)
        if service_right <= time_box[2]:
            return ()
        return _locations_from_cells(cells, page_index, digest, x_edges, table_top, table_bottom, day_edges)
    finally:
        textpage.close()


def extract_groups(pdf_bytes: bytes) -> tuple[GroupLocation, ...]:
    """Index exact group identifiers, including aliases sharing one column."""
    _validate_bytes(pdf_bytes)
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    with _PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(pdf_bytes)
            try:
                if not 1 <= len(document) <= MAX_PDF_PAGES:
                    raise UnsupportedTimetableError("Unsupported number of timetable pages")
                locations = []
                for page_index in range(len(document)):
                    page = document[page_index]
                    try:
                        locations.extend(_page_locations(page, page_index, digest) or _block_locations(page, page_index, digest))
                    finally:
                        page.close()
            finally:
                document.close()
        except pdfium.PdfiumError as exc:
            raise UnsupportedTimetableError("The timetable PDF could not be read") from exc
    if not locations:
        raise UnsupportedTimetableError("No supported group columns were found in the timetable")
    return tuple(locations)


def _render_rectangle(page, rectangle, scale: float) -> Image.Image:
    left, bottom, right, top = rectangle
    width, height = page.get_size()
    left, bottom = max(0.0, left), max(0.0, bottom)
    right, top = min(width, right), min(height, top)
    if right <= left or top <= bottom:
        raise UnsupportedTimetableError("The timetable crop is outside the PDF page")
    bitmap = page.render(scale=scale, crop=(left, bottom, width - right, height - top))
    try:
        return bitmap.to_pil().convert("RGB").copy()
    finally:
        bitmap.close()


def _render_band(page, location: GroupLocation, bottom: float, top: float, scale: float) -> Image.Image:
    # Stay inside the thinnest supported half-point border. Wider padding can
    # leak a neighbouring group's coloured cell or another day into the crop.
    pad = 0.4
    if abs(location.column_left - location.service_right) < 1.5:
        return _render_rectangle(page, (location.table_left - pad, bottom - pad, location.column_right + pad, top + pad), scale)
    service = _render_rectangle(page, (location.table_left - pad, bottom - pad, location.service_right + pad, top + pad), scale)
    group = _render_rectangle(page, (location.column_left - pad, bottom - pad, location.column_right + pad, top + pad), scale)
    gap = 6
    combined = Image.new("RGB", (service.width + group.width + gap, max(service.height, group.height)), "white")
    combined.paste(service, (0, 0))
    combined.paste(group, (service.width + gap, 0))
    service.close()
    group.close()
    return combined


def render_group(
    pdf_bytes: bytes, location: GroupLocation, *,
    target_width: int = TARGET_IMAGE_WIDTH, max_height: int = MAX_IMAGE_HEIGHT,
) -> tuple[BytesIO, ...]:
    """Render original pixels in day-aligned parts with a repeated group header."""
    _validate_bytes(pdf_bytes)
    if hashlib.sha256(pdf_bytes).hexdigest() != location.pdf_sha256:
        raise UnsupportedTimetableError("The group location belongs to a different PDF")
    if not 600 <= target_width <= 1600 or not 800 <= max_height <= 2000:
        raise ValueError("Unsupported timetable image dimensions")
    if len(location.day_edges) < 2:
        raise UnsupportedTimetableError("The timetable has no safe day boundaries")
    padding = 16
    content_width = location.service_right - location.table_left + location.column_right - location.column_left + 3.6
    gap = 0 if abs(location.column_left - location.service_right) < 1.5 else 6
    scale = min(8.0, (target_width - padding * 2 - gap) / content_width)
    header_height = (location.table_top - location.day_edges[0] + 1.8) * scale
    body_limit = (max_height - padding * 2 - header_height - 8) / scale - 1.8
    parts: list[tuple[float, float]] = []
    start = location.day_edges[0]
    for index in range(1, len(location.day_edges)):
        bottom = location.day_edges[index]
        if start - bottom > body_limit:
            previous = location.day_edges[index - 1]
            if previous == start:
                raise UnsupportedTimetableError("A single timetable day is too tall to render safely")
            parts.append((previous, start))
            start = previous
    parts.append((location.day_edges[-1], start))
    if len(parts) > 8:
        raise UnsupportedTimetableError("The timetable requires too many image parts")

    with _PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(pdf_bytes)
            try:
                if not 0 <= location.page_index < len(document):
                    raise UnsupportedTimetableError("The group page no longer exists")
                page = document[location.page_index]
                try:
                    header = _render_band(page, location, location.day_edges[0], location.table_top, scale)
                    rendered = []
                    try:
                        for index, (bottom, top) in enumerate(parts):
                            body = _render_band(page, location, bottom, top, scale)
                            image = Image.new("RGB", (max(header.width, body.width) + padding * 2, header.height + body.height + padding * 2), "white")
                            try:
                                image.paste(header, (padding, padding))
                                image.paste(body, (padding, padding + header.height))
                                if image.height > max_height or image.width > target_width + 4:
                                    raise UnsupportedTimetableError("The rendered timetable exceeds its image limits")
                                output = BytesIO()
                                image.save(output, format="PNG", optimize=True)
                                output.name = f"group-{normalize_group(location.group)}-{index + 1}.png"
                                output.seek(0)
                                rendered.append(output)
                            finally:
                                image.close()
                                body.close()
                    finally:
                        header.close()
                finally:
                    page.close()
            finally:
                document.close()
        except pdfium.PdfiumError as exc:
            raise UnsupportedTimetableError("The timetable PDF could not be rendered") from exc
    return tuple(rendered)
