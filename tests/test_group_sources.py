from __future__ import annotations

from datetime import date

import pytest
import requests

from app.group_sources import (
    GROUP_SCHEDULE_PAGE_URL,
    GroupScheduleRepository,
    GroupScheduleSource,
    GroupSourceError,
    discover_group_sources,
    pick_sources,
)


def source_html(title: str, href: str = "/upload/iblock/x/current.pdf", pane: str = "week") -> str:
    return (
        f'<a class="nav-link" href="#{pane}">{title}</a>'
        f'<div class="tab-pane" id="{pane}" role="tabpanel">'
        f'<a href="{href}"><img src="/preview.jpg"></a>'
        f'<div><a href="{href}" download>Скачать</a></div></div>'
    )


def make_source(category: str, start: date, end: date, name: str = "current") -> GroupScheduleSource:
    return GroupScheduleSource(name, category, f"https://www.prk.kuzstu.ru/upload/{name}.pdf", start, end)


def test_discovers_real_tab_relationships_and_deduplicates_repeated_panes():
    html = source_html(
        "Расписание занятий СПО 16.11.2026-22.11.2026 (четная неделя)",
        "/upload/iblock/947/СПО Расписание 16.11-22.11 2026-2027.pdf",
        "v-pills-el8572",
    )
    html += html
    sources = discover_group_sources(html)
    assert len(sources) == 1
    source = sources[0]
    assert source.category == "spo"
    assert source.category_label == "СПО"
    assert (source.start, source.end) == (date(2026, 11, 16), date(2026, 11, 22))
    assert source.page_url == GROUP_SCHEDULE_PAGE_URL
    assert "%D0%A1%D0%9F%D0%9E%20" in source.url
    assert " " not in source.url


def test_tab_dates_take_priority_over_a_different_filename_week():
    sources = discover_group_sources(source_html(
        "Расписание занятий ВО, Лицей 16.11.2026-22.11.2026",
        "/upload/old-09.11.2026-15.11.2026.pdf",
    ))
    assert sources[0].category == "vo"
    assert sources[0].start == date(2026, 11, 16)


@pytest.mark.parametrize(
    "label,start,end",
    [
        ("Расписание занятий ВО, Лицей 28.12.2026–03.01.2027", date(2026, 12, 28), date(2027, 1, 3)),
        ("Расписание занятий СПО 28.12–03.01.2027", date(2026, 12, 28), date(2027, 1, 3)),
        ("Расписание занятий СПО 28.12.2026–03.01", date(2026, 12, 28), date(2027, 1, 3)),
        ("Расписание занятий СПО 01.01.2027–07.01.2027", date(2027, 1, 1), date(2027, 1, 7)),
        ("Расписание занятий суббота 14.11.2026", date(2026, 11, 14), date(2026, 11, 14)),
    ],
)
def test_periods_support_single_days_january_and_year_boundaries(label, start, end):
    source = discover_group_sources(source_html(label))[0]
    assert (source.start, source.end) == (start, end)


def test_an_undated_tab_can_use_an_explicit_filename_period():
    source = discover_group_sources(source_html(
        "Расписание занятий СПО", "/upload/16.11.2026-22.11.2026.pdf"
    ))[0]
    assert source.start == date(2026, 11, 16)


@pytest.mark.parametrize(
    "label",
    [
        "График консультаций 16.11.2026-22.11.2026",
        "Промежуточная аттестация 16.11.2026-22.11.2026",
        "Расписание занятий в компьютерных аудиториях",
        "Сроки зимней сессии 2026-2027",
        "Расписание занятий СПО 32.11.2026-39.11.2026",
        "Расписание занятий СПО 16.11-22.11",
    ],
)
def test_non_group_documents_and_ambiguous_or_invalid_dates_are_not_published(label):
    assert discover_group_sources(source_html(label)) == ()


@pytest.mark.parametrize(
    "href",
    [
        "https://attacker.example/upload/plan.pdf",
        "http://www.prk.kuzstu.ru/upload/plan.pdf",
        "//attacker.example/upload/plan.pdf",
        "https://www.prk.kuzstu.ru@attacker.example/upload/plan.pdf",
        "https://user:password@www.prk.kuzstu.ru/upload/plan.pdf",
        "https://www.prk.kuzstu.ru:444/upload/plan.pdf",
        "/private/plan.pdf",
        "/upload/%2e%2e/private/plan.pdf",
    ],
)
def test_discovery_rejects_non_official_or_unexpected_download_addresses(href):
    assert discover_group_sources(source_html("Расписание занятий СПО 16.11.2026-22.11.2026", href)) == ()


def test_encoded_names_are_not_double_encoded_and_bare_official_host_is_supported():
    source = discover_group_sources(source_html(
        "Расписание занятий СПО 16.11.2026-22.11.2026",
        "https://prk.kuzstu.ru/upload/%D0%A1%D0%9F%D0%9E.pdf",
    ))[0]
    assert source.url == "https://www.prk.kuzstu.ru/upload/%D0%A1%D0%9F%D0%9E.pdf"


def test_selection_does_not_substitute_a_past_week_for_current():
    past = make_source("vo", date(2026, 11, 9), date(2026, 11, 15), "past")
    assert pick_sources([past], date(2026, 11, 16)) == ()
    assert pick_sources([past], date(2026, 11, 16), prefer="available") == (past,)
    assert past.end == date(2026, 11, 15)


def test_selection_preserves_each_category_and_checks_exact_date_boundaries():
    vo = make_source("vo", date(2026, 11, 16), date(2026, 11, 22), "vo")
    spo = make_source("spo", date(2026, 11, 16), date(2026, 11, 22), "spo")
    next_week = make_source("vo", date(2026, 11, 23), date(2026, 11, 29), "next")
    assert pick_sources([vo, spo, next_week], date(2026, 11, 16)) == (vo, spo)
    assert pick_sources([vo, spo, next_week], date(2026, 11, 22)) == (vo, spo)
    assert pick_sources([vo, spo, next_week], date(2026, 11, 22), prefer="next") == (next_week,)
    assert pick_sources([vo, spo, next_week], date(2026, 11, 23)) == (next_week,)


def test_available_prefers_current_then_nearest_future_then_latest_past():
    past = make_source("vo", date(2026, 11, 9), date(2026, 11, 15), "past")
    future = make_source("vo", date(2026, 11, 23), date(2026, 11, 29), "future")
    later = make_source("vo", date(2026, 12, 7), date(2026, 12, 13), "later")
    target = date(2026, 11, 16)
    assert pick_sources([later, past, future], target, prefer="available") == (future,)
    assert pick_sources([past, future, later], target, prefer="latest") == (later,)
    with pytest.raises(ValueError):
        pick_sources([past], target, prefer="unknown")


class FakeResponse:
    def __init__(self, content=b"%PDF-test", *, status=200, headers=None, url="", chunks=None):
        self.content = content
        self.status_code = status
        self.headers = headers or {}
        self.url = url
        self.chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("offline HTTP failure")

    def iter_content(self, chunk_size):
        yield from self.chunks if self.chunks is not None else [self.content]


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_catalog_and_pdfs_are_cached_but_refresh_after_expiry():
    clock = [0.0]
    html = source_html("Расписание занятий СПО 16.11.2026-22.11.2026").encode()
    session = FakeSession([FakeResponse(html), FakeResponse(), FakeResponse(html), FakeResponse(b"%PDF-new")])
    repository = GroupScheduleRepository(session=session, clock=lambda: clock[0], catalog_ttl_seconds=5, pdf_ttl_seconds=10)
    source = repository.fetch_catalog()[0]
    assert repository.fetch_catalog() == (source,)
    assert repository.get_pdf(source) == repository.get_pdf(source) == b"%PDF-test"
    assert len(session.calls) == 2
    clock[0] = 11
    repository.fetch_catalog()
    assert repository.get_pdf(source) == b"%PDF-new"
    assert len(session.calls) == 4
    assert all(call[1] == {"timeout": (5, 15), "stream": True, "allow_redirects": False} for call in session.calls)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(b"<html>Access denied</html>", headers={"Content-Type": "text/html"}),
        FakeResponse(b"not a pdf"),
        FakeResponse(headers={"Content-Length": "100"}),
        FakeResponse(chunks=[b"%PDF-", b"123456789"]),
    ],
)
def test_pdf_download_is_bounded_and_rejects_html_or_non_pdf_responses(response):
    repository = GroupScheduleRepository(session=FakeSession([response]), max_pdf_bytes=10)
    source = make_source("vo", date(2026, 11, 16), date(2026, 11, 22))
    with pytest.raises(GroupSourceError):
        repository.get_pdf(source)
    assert response.closed


def test_an_external_redirect_is_rejected_before_following_it():
    session = FakeSession([FakeResponse(status=302, headers={"Location": "https://attacker.example/secret.pdf"})])
    repository = GroupScheduleRepository(session=session)
    source = make_source("vo", date(2026, 11, 16), date(2026, 11, 22))
    with pytest.raises(GroupSourceError):
        repository.get_pdf(source)
    assert len(session.calls) == 1


def test_download_follows_only_valid_official_redirects():
    session = FakeSession([FakeResponse(status=302, headers={"Location": "/upload/redirect.pdf"}), FakeResponse()])
    repository = GroupScheduleRepository(session=session)
    source = make_source("vo", date(2026, 11, 16), date(2026, 11, 22))
    assert repository.get_pdf(source) == b"%PDF-test"
    assert session.calls[1][0] == "https://www.prk.kuzstu.ru/upload/redirect.pdf"


def test_pdf_lru_cache_has_a_total_byte_limit():
    session = FakeSession([FakeResponse(), FakeResponse(), FakeResponse()])
    repository = GroupScheduleRepository(session=session, max_cache_bytes=10)
    first = make_source("vo", date(2026, 11, 16), date(2026, 11, 22), "one")
    second = make_source("vo", date(2026, 11, 16), date(2026, 11, 22), "two")
    repository.get_pdf(first)
    repository.get_pdf(second)
    repository.get_pdf(first)
    assert len(session.calls) == 3


def test_network_failures_and_empty_catalogs_are_explicit_errors():
    repository = GroupScheduleRepository(session=FakeSession([requests.Timeout("offline timeout")]))
    with pytest.raises(GroupSourceError, match="temporarily unavailable"):
        repository.fetch_catalog()
    repository = GroupScheduleRepository(session=FakeSession([FakeResponse(b"<html>No published timetable</html>")]))
    with pytest.raises(GroupSourceError, match="No dated group timetables"):
        repository.fetch_catalog()
