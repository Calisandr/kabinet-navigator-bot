from __future__ import annotations

from datetime import date
from io import BytesIO
from types import SimpleNamespace

import pytest

from app import group_schedule as module
from app.group_sources import GroupScheduleSource, GroupSourceError


TODAY = date(2026, 11, 16)


def source(name="current", *, category="spo", start=TODAY, end=date(2026, 11, 22)):
    return GroupScheduleSource(
        title=f"Расписание {name}", category=category,
        url=f"https://www.prk.kuzstu.ru/upload/{name}.pdf", start=start, end=end,
    )


def location(group, *, column=100, page=0):
    return SimpleNamespace(group=group, page_index=page, column_left=column, column_right=column + 100)


class Repository:
    def __init__(self, sources, documents):
        self.sources = tuple(sources)
        self.documents = documents
        self.reads = []

    def fetch_catalog(self):
        return self.sources

    def get_pdf(self, item):
        self.reads.append(item.url)
        document = self.documents[item.url]
        if isinstance(document, Exception):
            raise document
        if isinstance(document, list):
            return document.pop(0) if len(document) > 1 else document[0]
        return document


def test_group_list_selects_current_week_and_keeps_aliases(monkeypatch):
    current = source()
    old = source("old", start=date(2026, 11, 9), end=date(2026, 11, 15))
    repo = Repository([old, current], {current.url: b"current", old.url: b"old"})
    monkeypatch.setattr(module, "extract_groups", lambda data: [location("ИСт-241.2"), location("ИСтс-251.2")])

    options = module.GroupScheduleService(repo).list_groups(TODAY, category="spo")

    assert {item.group for item in options} == {"ИСт-241.2", "ИСтс-251.2"}
    assert all(item.start == TODAY for item in options)
    assert repo.reads == [current.url]


def test_explicit_unpublished_period_does_not_fall_back_to_an_old_week():
    old = source("old", start=date(2026, 11, 9), end=date(2026, 11, 15))
    repo = Repository([old], {old.url: b"old"})
    with pytest.raises(module.GroupScheduleError, match="пока не найдено"):
        module.GroupScheduleService(repo).list_groups(TODAY, category="spo", period_start=TODAY)
    assert not repo.reads


def test_last_published_schedule_retains_real_dates(monkeypatch):
    old = source("saturday", category="part_time", start=date(2026, 11, 14), end=date(2026, 11, 14))
    repo = Repository([old], {old.url: b"old"})
    monkeypatch.setattr(module, "extract_groups", lambda _: [location("ГМсозт-241.2")])
    options = module.GroupScheduleService(repo).list_groups(TODAY, category="part_time")
    assert options[0].start == date(2026, 11, 14)
    assert options[0].end < TODAY


def test_exact_group_match_renders_original_and_preserves_source(monkeypatch):
    current = source()
    repo = Repository([current], {current.url: b"current"})
    selected = location("МАб-241.2.1")
    other = location("МАб-241.2.2", column=200)
    monkeypatch.setattr(module, "extract_groups", lambda _: (selected, other))
    images = (BytesIO(b"image-1"), BytesIO(b"image-2"))
    calls = []
    monkeypatch.setattr(module, "render_group", lambda data, item: calls.append((data, item)) or images)

    result = module.GroupScheduleService(repo).get_timetable("маб-241.2.1", TODAY)

    assert result.group == selected.group
    assert result.source is current
    assert result.images == images
    assert calls == [(b"current", selected)]


def test_partial_group_code_only_suggests_and_never_selects_a_subgroup(monkeypatch):
    current = source()
    repo = Repository([current], {current.url: b"current"})
    monkeypatch.setattr(module, "extract_groups", lambda _: [location("ТАт-231.2.1"), location("ТАт-231.2.2", column=200)])
    monkeypatch.setattr(module, "render_group", lambda *_: pytest.fail("A partial query must not render a different group"))
    with pytest.raises(module.GroupNotFound) as error:
        module.GroupScheduleService(repo).get_timetable("ТАт-231.2", TODAY)
    assert {item.group for item in error.value.suggestions} == {"ТАт-231.2.1", "ТАт-231.2.2"}


def test_duplicate_group_in_different_categories_requires_an_explicit_selection(monkeypatch):
    vo = source("vo", category="vo")
    spo = source("spo", category="spo")
    repo = Repository([vo, spo], {vo.url: b"vo", spo.url: b"spo"})
    monkeypatch.setattr(module, "extract_groups", lambda _: [location("ГПс-231.2")])
    with pytest.raises(module.GroupNotFound) as error:
        module.GroupScheduleService(repo).get_timetable("ГПс-231.2", TODAY)
    assert {item.category for item in error.value.suggestions} == {"vo", "spo"}


def test_group_continuing_on_another_page_returns_all_its_fragments(monkeypatch):
    current = source()
    repo = Repository([current], {current.url: b"current"})
    first = location("ИСт-241.2", page=0)
    second = location("ИСт-241.2", page=1)
    monkeypatch.setattr(module, "extract_groups", lambda _: [first, second])
    calls = []
    monkeypatch.setattr(module, "render_group", lambda _, item: calls.append(item) or (BytesIO(str(item.page_index).encode()),))

    result = module.GroupScheduleService(repo).get_timetable("ИСт-241.2", TODAY)

    assert calls == [first, second]
    assert [image.getvalue() for image in result.images] == [b"0", b"1"]


def test_changed_pdf_at_the_same_url_is_reindexed_before_cropping(monkeypatch):
    current = source()
    repo = Repository([current], {current.url: [b"first", b"replacement"]})
    original = location("ИСт-241.2", column=100)
    replacement = location("ИСт-241.2", column=300)
    monkeypatch.setattr(module, "extract_groups", lambda data: [original if data == b"first" else replacement])
    calls = []
    monkeypatch.setattr(module, "render_group", lambda data, item: calls.append((data, item)) or (BytesIO(b"img"),))

    module.GroupScheduleService(repo).get_timetable("ИСт-241.2", TODAY)

    assert calls == [(b"replacement", replacement)]


def test_group_removed_during_refresh_returns_original_link_without_wrong_crop(monkeypatch):
    current = source()
    repo = Repository([current], {current.url: [b"first", b"replacement"]})
    monkeypatch.setattr(module, "extract_groups", lambda data: [location("ИСт-241.2" if data == b"first" else "ГПс-231.2")])
    monkeypatch.setattr(module, "render_group", lambda *_: pytest.fail("Stale geometry must not be used"))
    with pytest.raises(module.GroupScheduleError) as error:
        module.GroupScheduleService(repo).get_timetable("ИСт-241.2", TODAY)
    assert error.value.source_url == current.url


def test_unavailable_source_returns_a_safe_error_with_official_link():
    current = source()
    repo = Repository([current], {current.url: GroupSourceError("private transport detail")})
    with pytest.raises(module.GroupScheduleError) as error:
        module.GroupScheduleService(repo).list_groups(TODAY, category="spo")
    assert "private transport detail" not in str(error.value)
    assert error.value.source_url == current.url


def test_partial_catalogue_does_not_claim_a_group_from_unavailable_pdf_is_missing(monkeypatch):
    vo = source("vo", category="vo")
    spo = source("spo", category="spo")
    repo = Repository([vo, spo], {vo.url: b"vo", spo.url: GroupSourceError("connection failed")})
    monkeypatch.setattr(module, "extract_groups", lambda _: [location("ГПс-231.2")])

    with pytest.raises(module.GroupScheduleError, match="временно недоступна") as error:
        module.GroupScheduleService(repo).get_timetable("ИСт-241.2", TODAY)

    assert type(error.value) is module.GroupScheduleError
    assert error.value.source_url == spo.url


def test_available_group_still_works_if_an_unrelated_category_is_temporarily_down(monkeypatch):
    vo = source("vo", category="vo")
    spo = source("spo", category="spo")
    repo = Repository([vo, spo], {vo.url: b"vo", spo.url: GroupSourceError("connection failed")})
    monkeypatch.setattr(module, "extract_groups", lambda _: [location("ГПс-231.2")])
    monkeypatch.setattr(module, "render_group", lambda *_: (BytesIO(b"image"),))

    result = module.GroupScheduleService(repo).get_timetable("ГПс-231.2", TODAY)

    assert result.source is vo


def test_empty_query_and_invalid_category_do_not_fetch_documents():
    repo = Repository([], {})
    service = module.GroupScheduleService(repo)
    with pytest.raises(module.GroupNotFound):
        service.get_timetable(" ", TODAY)
    with pytest.raises(module.GroupScheduleError):
        service.list_groups(TODAY, category="untrusted")
    assert not repo.reads


def test_index_cache_is_bounded_and_changes_when_file_content_changes(monkeypatch):
    current = source()
    service = module.GroupScheduleService(Repository([current], {}), max_index_documents=2)
    calls = []
    monkeypatch.setattr(module, "extract_groups", lambda data: calls.append(data) or [location("ИСт-241.2")])
    for content in (b"one", b"two", b"two", b"three"):
        service._locations(current, content)
    assert calls == [b"one", b"two", b"three"]
    assert len(service._indexes) == 2
