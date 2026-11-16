from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app import group_schedule as module
from app.group_sources import GroupScheduleSource, GroupSourceError, pick_sources_in_range


TODAY = date(2026, 11, 16)


def publication(start, end, name="current", category="spo"):
    return GroupScheduleSource(
        name, category, f"https://www.prk.kuzstu.ru/upload/{name}.pdf", start, end,
    )


class Repository:
    def __init__(self, sources):
        self.sources = tuple(sources)
        self.reads = []
        self.catalog_reads = 0

    def fetch_catalog(self):
        self.catalog_reads += 1
        return self.sources

    def get_pdf(self, source):
        self.reads.append(source)
        return source.url.encode()


def configure_group(monkeypatch):
    location = SimpleNamespace(
        group="ИСт-241.2", page_index=0, column_left=100, column_right=200,
    )
    monkeypatch.setattr(module, "extract_groups", lambda _: (location,))

    def render(*_):
        output = BytesIO()
        with Image.new("RGB", (100, 120), "white") as image:
            image.save(output, "PNG")
        output.seek(0)
        return (output,)

    monkeypatch.setattr(module, "render_group", render)


def test_full_week_range_includes_monday_to_saturday_document_even_on_sunday(monkeypatch):
    source = publication(date(2026, 11, 16), date(2026, 11, 21))
    repository = Repository([source])
    configure_group(monkeypatch)
    service = module.GroupScheduleService(repository)

    result = service.get_timetable(
        "ИСт-241.2", date(2026, 11, 22), category="spo",
        period_start=date(2026, 11, 16), period_end=date(2026, 11, 22),
    )

    assert result.source is source
    assert result.source.end == date(2026, 11, 21)
    assert len(result.images) == 1


@pytest.mark.parametrize(
    "start,end",
    [
        (date(2026, 12, 28), date(2027, 1, 3)),
        (date(2026, 11, 30), date(2026, 12, 6)),
        (date(2028, 2, 28), date(2028, 3, 5)),
    ],
)
def test_period_selection_handles_month_year_and_leap_day_boundaries(start, end):
    source = publication(start, end)
    assert pick_sources_in_range([source], start, end) == (source,)
    assert pick_sources_in_range([source], end, end) == (source,)


def test_a_document_covering_the_week_beats_a_later_one_day_intersection():
    full_week = publication(TODAY, TODAY + timedelta(days=5), "week")
    one_day = publication(TODAY + timedelta(days=5), TODAY + timedelta(days=5), "single")
    assert pick_sources_in_range(
        [full_week, one_day], TODAY, TODAY + timedelta(days=6),
    ) == (full_week,)


def test_range_ties_prefer_later_period_then_last_catalogue_entry():
    first = publication(TODAY, TODAY + timedelta(days=2), "first")
    later = publication(TODAY + timedelta(days=3), TODAY + timedelta(days=5), "later")
    replacement = publication(later.start, later.end, "replacement")
    assert pick_sources_in_range([first, later, replacement], TODAY, TODAY + timedelta(days=6)) == (replacement,)


@pytest.mark.parametrize("exact_first", [True, False])
def test_selecting_a_published_period_prefers_exact_document_over_a_longer_one(exact_first):
    exact = publication(TODAY, TODAY + timedelta(days=5), "selected")
    same_start_longer = publication(TODAY, TODAY + timedelta(days=6), "longer")
    surrounds = publication(TODAY - timedelta(days=1), TODAY + timedelta(days=6), "surrounds")
    sources = [exact, surrounds, same_start_longer] if exact_first else [same_start_longer, surrounds, exact]

    assert pick_sources_in_range(sources, exact.start, exact.end) == (exact,)


def test_exact_period_tie_still_prefers_latest_catalogue_replacement():
    old = publication(TODAY, TODAY + timedelta(days=5), "old")
    replacement = publication(old.start, old.end, "replacement")
    longer = publication(old.start, old.end + timedelta(days=1), "longer")
    assert pick_sources_in_range([old, replacement, longer], old.start, old.end) == (replacement,)


def test_range_selection_keeps_categories_separate_in_stable_order():
    sources = [publication(TODAY, TODAY + timedelta(days=5), category=category, name=category) for category in ("part_time", "spo", "vo")]
    result = pick_sources_in_range(sources, TODAY, TODAY + timedelta(days=6))
    assert [source.category for source in result] == ["vo", "spo", "part_time"]


@pytest.mark.parametrize("offset", [-14, 7, 14])
def test_unpublished_requested_range_does_not_use_a_different_week(offset):
    current = publication(TODAY, TODAY + timedelta(days=5))
    repository = Repository([current])
    start = TODAY + timedelta(days=offset)

    with pytest.raises(module.GroupScheduleError, match="пока не найдено") as error:
        module.GroupScheduleService(repository).get_timetable(
            "ИСт-241.2", TODAY, period_start=start, period_end=start + timedelta(days=6),
        )

    assert start.strftime("%d.%m.%Y") in error.value.message
    assert not repository.reads


def test_single_day_range_checks_inclusive_start_and_end_boundaries():
    current = publication(TODAY, TODAY + timedelta(days=5))
    assert pick_sources_in_range([current], current.start, current.start) == (current,)
    assert pick_sources_in_range([current], current.end, current.end) == (current,)
    assert pick_sources_in_range([current], current.end + timedelta(days=1), current.end + timedelta(days=1)) == ()


@pytest.mark.parametrize(
    "start,end",
    [
        (None, TODAY),
        (TODAY, TODAY - timedelta(days=1)),
        (TODAY, TODAY + timedelta(days=31)),
        ("16.11.2026", TODAY),
        (TODAY, "22.11.2026"),
        (datetime(2026, 11, 16), TODAY),
        (True, TODAY),
    ],
)
def test_invalid_ranges_are_rejected_before_catalogue_download(start, end):
    repository = Repository([])
    with pytest.raises(module.GroupScheduleError, match="от 1 до 31 дня"):
        module.GroupScheduleService(repository).list_groups(TODAY, period_start=start, period_end=end)
    assert repository.catalog_reads == 0
    with pytest.raises(ValueError):
        pick_sources_in_range([], start, end)


def test_31_day_range_is_allowed_and_keeps_the_original_document_period():
    source = publication(TODAY, TODAY + timedelta(days=5))
    assert pick_sources_in_range([source], TODAY, TODAY + timedelta(days=30)) == (source,)
    assert (source.start, source.end) == (TODAY, TODAY + timedelta(days=5))


def test_list_periods_uses_actual_dates_deduplicates_replacements_and_never_reads_pdf():
    old = publication(TODAY - timedelta(days=7), TODAY - timedelta(days=2), "old")
    current = publication(TODAY, TODAY + timedelta(days=5), "current")
    replacement = publication(current.start, current.end, "replacement")
    other_category = publication(current.start, current.end, "vo", "vo")
    future = publication(TODAY + timedelta(days=7), TODAY + timedelta(days=12), "future")
    repository = Repository([current, other_category, old, replacement, future])
    service = module.GroupScheduleService(repository)

    periods = service.list_periods(TODAY)

    assert periods[0] is future
    assert periods[-1] is old
    assert set(source.url for source in periods) == {source.url for source in (future, replacement, other_category, old)}
    assert service.list_periods(TODAY, category="spo") == (future, replacement, old)
    assert not repository.reads


def test_empty_published_periods_list_is_empty_without_fallback_or_pdf_work():
    repository = Repository([])
    assert module.GroupScheduleService(repository).list_periods(TODAY) == ()
    assert not repository.reads


def test_period_list_rejects_unknown_category_before_fetching():
    repository = Repository([])
    with pytest.raises(module.GroupScheduleError, match="раздел"):
        module.GroupScheduleService(repository).list_periods(TODAY, category="https://evil.invalid")
    assert repository.catalog_reads == 0


def test_period_list_wraps_source_error_without_exposing_internals():
    class FailingRepository:
        def fetch_catalog(self):
            raise GroupSourceError("PRIVATE_INTERNAL_DIAGNOSTIC")

    with pytest.raises(module.GroupScheduleError, match="Сайт филиала временно недоступен") as error:
        module.GroupScheduleService(FailingRepository()).list_periods(TODAY)
    assert "PRIVATE_INTERNAL_DIAGNOSTIC" not in error.value.message


def test_range_group_list_uses_intersection_and_retains_document_dates(monkeypatch):
    source = publication(TODAY + timedelta(days=1), TODAY + timedelta(days=5))
    repository = Repository([source])
    configure_group(monkeypatch)

    options = module.GroupScheduleService(repository).list_groups(
        TODAY, category="spo", period_start=TODAY, period_end=TODAY + timedelta(days=6),
    )

    assert len(options) == 1
    assert options[0].start == source.start
    assert options[0].end == source.end
    assert options[0].group == "ИСт-241.2"


def test_legacy_explicit_target_date_is_still_strict_without_range_end():
    source = publication(TODAY, TODAY + timedelta(days=5))
    repository = Repository([source])
    service = module.GroupScheduleService(repository)
    with pytest.raises(module.GroupScheduleError, match="пока не найдено"):
        service.list_groups(TODAY, category="spo", period_start=TODAY + timedelta(days=6))
    assert not repository.reads
