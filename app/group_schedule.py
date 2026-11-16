from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from difflib import get_close_matches
from io import BytesIO

from PIL import Image

from .group_pdf import GroupLocation, UnsupportedTimetableError, extract_groups, normalize_group, render_group
from .group_sources import (
    GROUP_SCHEDULE_PAGE_URL,
    GroupScheduleRepository,
    GroupScheduleSource,
    GroupSourceError,
    pick_sources,
    pick_sources_in_range,
)


LOGGER = logging.getLogger(__name__)
MAX_GROUP_IMAGES = 12
MAX_COMBINED_PIXELS = 30_000_000
MAX_PHOTO_BYTES = 10 * 1024 * 1024


def _single_week_image(images: tuple[BytesIO, ...], group: str) -> BytesIO:
    """Keep all published group continuations in one Telegram-sized image."""
    if not images or len(images) > MAX_GROUP_IMAGES:
        raise UnsupportedTimetableError("Invalid number of group timetable images")
    sizes: list[tuple[int, int]] = []
    for content in images:
        content.seek(0)
        with Image.open(content) as image:
            sizes.append(image.size)
    if sum(width * height for width, height in sizes) > MAX_COMBINED_PIXELS:
        raise UnsupportedTimetableError("The combined group timetable is too large")
    width = max(size[0] for size in sizes)
    height = sum(size[1] for size in sizes)
    if width <= 0 or height <= 0 or max(width / height, height / width) > 20:
        raise UnsupportedTimetableError("The combined timetable has unsupported proportions")
    scale = min(1.0, 1600 / width, 8000 / height, 9900 / (width + height))
    if len(images) == 1 and scale == 1.0 and images[0].getbuffer().nbytes < MAX_PHOTO_BYTES:
        images[0].seek(0)
        return images[0]
    canvas = Image.new("RGB", (width, height), "white")
    try:
        top = 0
        for content in images:
            content.seek(0)
            with Image.open(content) as image:
                canvas.paste(image, ((width - image.width) // 2, top))
                top += image.height
        if scale < 1.0:
            resized = canvas.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
            canvas.close()
            canvas = resized
        result = BytesIO()
        canvas.save(result, format="PNG", optimize=True)
        if result.getbuffer().nbytes >= MAX_PHOTO_BYTES:
            raise UnsupportedTimetableError("The weekly image exceeds the Telegram upload limit")
        result.name = f"group-{normalize_group(group)}-week.png"
        result.seek(0)
        return result
    finally:
        canvas.close()


class GroupScheduleError(RuntimeError):
    def __init__(self, message: str, *, source_url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.source_url = source_url or GROUP_SCHEDULE_PAGE_URL


@dataclass(frozen=True)
class GroupOption:
    group: str
    category: str
    category_label: str
    start: date
    end: date


class GroupNotFound(GroupScheduleError):
    def __init__(
        self, message: str, *, suggestions: tuple[GroupOption, ...] = (), source_url: str | None = None
    ) -> None:
        super().__init__(message, source_url=source_url)
        self.suggestions = suggestions


@dataclass(frozen=True)
class GroupTimetable:
    group: str
    source: GroupScheduleSource
    images: tuple[BytesIO, ...]


@dataclass(frozen=True)
class _IndexedGroup:
    source: GroupScheduleSource
    location: GroupLocation

    def option(self) -> GroupOption:
        return GroupOption(
            group=self.location.group,
            category=self.source.category,
            category_label=self.source.category_label,
            start=self.source.start,
            end=self.source.end,
        )


class GroupScheduleService:
    def __init__(
        self, repository: GroupScheduleRepository | None = None, *, max_index_documents: int = 12
    ) -> None:
        if max_index_documents < 1:
            raise ValueError("At least one indexed timetable document must be allowed")
        self.repository = repository or GroupScheduleRepository()
        self.max_index_documents = max_index_documents
        self._indexes: OrderedDict[tuple[str, str], tuple[GroupLocation, ...]] = OrderedDict()
        self._index_lock = threading.RLock()

    def _catalog(self, category: str | None) -> tuple[GroupScheduleSource, ...]:
        if category is not None and category not in {"vo", "spo", "part_time"}:
            raise GroupScheduleError("Не удалось определить раздел расписания. Выберите его заново.")
        try:
            catalog = self.repository.fetch_catalog()
        except (GroupSourceError, OSError) as exc:
            raise GroupScheduleError(
                "Сайт филиала временно недоступен. Попробуйте запросить расписание чуть позже."
            ) from exc
        if category is not None:
            catalog = tuple(source for source in catalog if source.category == category)
        return tuple(catalog)

    def list_periods(self, today: date, category: str | None = None) -> tuple[GroupScheduleSource, ...]:
        """List actual published periods, newest first, without downloading PDFs."""
        catalog = self._catalog(category)
        by_period = {(source.category, source.start, source.end): source for source in catalog}
        return tuple(sorted(
            by_period.values(), key=lambda source: (source.start, source.end, source.category), reverse=True,
        ))

    def _sources(
        self, today: date, category: str | None, period_start: date | None,
        period_end: date | None = None,
    ) -> tuple[GroupScheduleSource, ...]:
        if period_end is not None:
            if (
                type(period_start) is not date or type(period_end) is not date
                or not 0 <= (period_end - period_start).days < 31
            ):
                raise GroupScheduleError("Выберите корректный период от 1 до 31 дня.")
        catalog = self._catalog(category)
        target = period_start or today
        sources = (
            pick_sources_in_range(catalog, period_start, period_end)
            if period_end is not None
            else pick_sources(catalog, target=target, prefer="current" if period_start else "available")
        )
        if not sources:
            period = (
                f" за {period_start:%d.%m.%Y} — {period_end:%d.%m.%Y}"
                if period_end is not None
                else (f" на {target:%d.%m.%Y}" if period_start else "")
            )
            raise GroupScheduleError(
                f"Расписание{period} пока не найдено среди опубликованных документов. "
                "Проверьте официальный раздел филиала."
            )
        return sources

    def _locations(self, source: GroupScheduleSource, content: bytes) -> tuple[GroupLocation, ...]:
        key = (source.url, hashlib.sha256(content).hexdigest())
        with self._index_lock:
            if key in self._indexes:
                self._indexes.move_to_end(key)
                return self._indexes[key]
        locations = extract_groups(content)
        with self._index_lock:
            self._indexes[key] = locations
            self._indexes.move_to_end(key)
            while len(self._indexes) > self.max_index_documents:
                self._indexes.popitem(last=False)
        return locations

    def _groups(
        self, today: date, category: str | None, period_start: date | None,
        period_end: date | None = None,
    ) -> tuple[tuple[_IndexedGroup, ...], tuple[GroupScheduleSource, ...]]:
        groups: list[_IndexedGroup] = []
        errors: list[GroupScheduleSource] = []
        for source in self._sources(today, category, period_start, period_end):
            try:
                content = self.repository.get_pdf(source)
                locations = self._locations(source, content)
                if not locations:
                    raise UnsupportedTimetableError("No group columns were found")
                groups.extend(_IndexedGroup(source, location) for location in locations)
            except (GroupSourceError, UnsupportedTimetableError, OSError) as exc:
                LOGGER.warning("A published group timetable could not be indexed (%s)", type(exc).__name__)
                errors.append(source)
        if not groups:
            raise GroupScheduleError(
                "Не удалось прочитать группы в опубликованном расписании. "
                "Откройте официальный документ по ссылке ниже.",
                source_url=errors[0].url if errors else GROUP_SCHEDULE_PAGE_URL,
            )
        if errors:
            # A partial list must not silently claim a missing group does not exist.
            LOGGER.warning("The group timetable catalogue is temporarily incomplete")
        return tuple(groups), tuple(errors)

    def list_groups(
        self, today: date, category: str | None = None, period_start: date | None = None,
        *, period_end: date | None = None,
    ) -> tuple[GroupOption, ...]:
        groups, errors = self._groups(today, category, period_start, period_end)
        if errors:
            raise GroupScheduleError(
                "Часть опубликованных расписаний временно недоступна. "
                "Повторите запрос позже или откройте официальный документ.",
                source_url=errors[0].url,
            )
        options = {
            (item.source.category, normalize_group(item.location.group), item.source.start): item.option()
            for item in groups
        }
        return tuple(sorted(options.values(), key=lambda item: (item.category, item.group, item.start)))

    def get_timetable(
        self,
        query: str,
        today: date,
        category: str | None = None,
        period_start: date | None = None,
        *,
        period_end: date | None = None,
    ) -> GroupTimetable:
        needle = normalize_group(query)
        if not needle or len(query) > 80:
            raise GroupNotFound("Введите полный код группы, например ИСт-241.2.")
        groups, errors = self._groups(today, category, period_start, period_end)
        matches = [item for item in groups if normalize_group(item.location.group) == needle]
        # A combined header can expose the same alias twice. Use geometry, not
        # partial text matching, to identify the intended published column.
        by_source = {item.source.url: item for item in matches}
        if len(by_source) > 1:
            options = tuple(item.option() for item in by_source.values())
            raise GroupNotFound(
                "Эта группа встречается в нескольких расписаниях. Выберите нужный раздел.",
                suggestions=options,
            )
        if not by_source:
            if errors:
                raise GroupScheduleError(
                    "Не удалось проверить группу: часть опубликованных расписаний временно недоступна. "
                    "Повторите запрос позже или откройте официальный документ.",
                    source_url=errors[0].url,
                )
            by_name = {normalize_group(item.location.group): item for item in groups}
            suggestions = get_close_matches(needle, by_name, n=6, cutoff=0.55)
            raise GroupNotFound(
                "Такая группа не найдена в доступных расписаниях. Проверьте полный код или выберите группу из списка.",
                suggestions=tuple(by_name[name].option() for name in suggestions),
            )
        match = next(iter(by_source.values()))
        try:
            content = self.repository.get_pdf(match.source)
            # The URL may stay the same after the university replaces a PDF.
            # Re-index the exact bytes used for rendering before trusting a crop.
            locations = self._locations(match.source, content)
            matching_locations = [item for item in locations if normalize_group(item.group) == needle]
            if not matching_locations:
                raise UnsupportedTimetableError("The group changed while refreshing its timetable")
            distinct_locations = {
                (
                    item.page_index, item.column_left, item.column_right,
                    getattr(item, "table_top", None), getattr(item, "table_bottom", None),
                ): item
                for item in matching_locations
            }
            rendered: list[BytesIO] = []
            for location in distinct_locations.values():
                rendered.extend(render_group(content, location))
                if len(rendered) > MAX_GROUP_IMAGES:
                    raise UnsupportedTimetableError("The group timetable has too many image fragments")
            if not rendered:
                raise UnsupportedTimetableError("No timetable images were produced")
            images = (_single_week_image(tuple(rendered), matching_locations[0].group),)
        except (GroupSourceError, UnsupportedTimetableError, OSError) as exc:
            raise GroupScheduleError(
                "Не удалось подготовить изображение группы. Оригинальное расписание доступно по ссылке ниже.",
                source_url=match.source.url,
            ) from exc
        return GroupTimetable(group=matching_locations[0].group, source=match.source, images=images)
