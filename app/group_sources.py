from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests


GROUP_SCHEDULE_PAGE_URL = "https://www.prk.kuzstu.ru/studentu/raspisanie/"
OFFICIAL_HOSTS = frozenset({"prk.kuzstu.ru", "www.prk.kuzstu.ru"})
_DATE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(20\d{2}))?(?!\d)")


class GroupSourceError(RuntimeError):
    """The official timetable source could not be read safely."""


@dataclass(frozen=True)
class GroupScheduleSource:
    title: str
    category: str
    url: str
    start: date
    end: date
    page_url: str = GROUP_SCHEDULE_PAGE_URL

    @property
    def category_label(self) -> str:
        return {"vo": "ВО и лицей", "spo": "СПО", "part_time": "Заочная форма"}[self.category]


def _official_url(url: str, base_url: str, *, pdf: bool) -> str:
    try:
        parsed = urlsplit(urljoin(base_url, url))
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname not in OFFICIAL_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Not an official HTTPS address")
        path = unquote(parsed.path)
        if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
            raise ValueError("Invalid timetable path")
        if pdf:
            if not path.startswith("/upload/") or not path.lower().endswith(".pdf"):
                raise ValueError("Not an uploaded PDF timetable")
        elif path.rstrip("/") != "/studentu/raspisanie":
            raise ValueError("Not the official timetable page")
        # The www endpoint works with standard TLS clients; both names belong
        # to the same official site. Encode Unicode filenames exactly once.
        return urlunsplit(
            (
                "https",
                "www.prk.kuzstu.ru",
                quote(path, safe="/!$&'()*+,;=:@-._~"),
                quote(parsed.query, safe="=&%;:+,/?@"),
                "",
            )
        )
    except (TypeError, ValueError) as exc:
        raise GroupSourceError("Invalid official timetable address") from exc


class _SchedulePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.div_ids: list[str] = []
        self.anchor: dict[str, str] | None = None
        self.labels: dict[str, str] = {}
        self.links: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "div":
            self.div_ids.append(attributes.get("id", ""))
        if tag == "a":
            href = attributes.get("href", "")
            self.anchor = {"href": href, "text": ""}
            if ".pdf" in href.lower():
                for element_id in self.div_ids:
                    if element_id:
                        self.links.setdefault(element_id, []).append(href)

    def handle_data(self, data: str) -> None:
        if self.anchor is not None:
            self.anchor["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.div_ids:
            self.div_ids.pop()
        if tag == "a" and self.anchor is not None:
            href = self.anchor["href"]
            if href.startswith("#") and len(href) > 1:
                label = " ".join(self.anchor["text"].split())
                if label:
                    self.labels[href[1:]] = label
            self.anchor = None


def _category(title: str) -> str | None:
    title = title.casefold()
    if not title.startswith("расписание занятий") or "компьютерн" in title:
        return None
    if re.search(r"\bспо\b", title):
        return "spo"
    if re.search(r"\bво\b", title) or "лицей" in title:
        return "vo"
    if "суббот" in title:
        return "part_time"
    return None


def _period(text: str) -> tuple[date, date] | None:
    tokens = list(_DATE.finditer(text))
    if not tokens or len(tokens) > 2:
        return None
    if not any(token.group(3) for token in tokens):
        return None
    first = tokens[0]
    last = tokens[-1]
    first_year = int(first.group(3)) if first.group(3) else None
    last_year = int(last.group(3)) if last.group(3) else None
    first_month_day = int(first.group(2)), int(first.group(1))
    last_month_day = int(last.group(2)), int(last.group(1))
    if first_year is None:
        first_year = last_year - (first_month_day > last_month_day)
    if last_year is None:
        last_year = first_year + (last_month_day < first_month_day)
    try:
        start = date(first_year, *first_month_day)
        end = date(last_year, *last_month_day)
    except ValueError:
        return None
    if end < start or (end - start).days > 31:
        return None
    return start, end


def discover_group_sources(
    html: str, base_url: str = GROUP_SCHEDULE_PAGE_URL
) -> tuple[GroupScheduleSource, ...]:
    page_url = _official_url(base_url, GROUP_SCHEDULE_PAGE_URL, pdf=False)
    parser = _SchedulePageParser()
    parser.feed(html)
    result: list[GroupScheduleSource] = []
    seen: set[str] = set()
    for element_id, title in parser.labels.items():
        category = _category(title)
        if category is None:
            continue
        for link in parser.links.get(element_id, ()):
            try:
                url = _official_url(link, page_url, pdf=True)
            except GroupSourceError:
                continue
            if url in seen:
                continue
            period = _period(title)
            # A dated tab is authoritative. A filename is only a fallback for
            # otherwise undated labels, never a replacement for invalid dates.
            if period is None and not _DATE.search(title):
                period = _period(unquote(urlsplit(url).path.rsplit("/", 1)[-1]))
            if period is None:
                continue
            seen.add(url)
            result.append(GroupScheduleSource(title, category, url, *period, page_url))
    return tuple(result)


def pick_sources(
    sources: Iterable[GroupScheduleSource], target: date | None = None, *, prefer: str = "current"
) -> tuple[GroupScheduleSource, ...]:
    """Select one published document per category without changing its dates.

    ``current`` is strict and only returns documents covering the target date.
    ``available`` additionally falls back to the nearest future document, then
    the latest past document. Callers must display the returned source period.
    ``next`` and ``latest`` explicitly select future/latest publications.
    """
    if prefer not in {"current", "available", "next", "latest"}:
        raise ValueError("Unknown timetable selection mode")
    target = target or date.today()
    categories: dict[str, list[tuple[int, GroupScheduleSource]]] = {}
    for index, source in enumerate(sources):
        categories.setdefault(source.category, []).append((index, source))
    result: list[GroupScheduleSource] = []
    for category in ("vo", "spo", "part_time"):
        candidates = categories.get(category, [])
        current = [item for item in candidates if item[1].start <= target <= item[1].end]
        future = [item for item in candidates if item[1].start > target]
        if prefer in {"current", "available"} and current:
            result.append(max(current, key=lambda item: (item[1].start, item[1].end, item[0]))[1])
        elif prefer in {"next", "available"} and future:
            earliest = min(item[1].start for item in future)
            result.append(max((item for item in future if item[1].start == earliest), key=lambda item: item[0])[1])
        elif prefer == "latest" or (prefer == "available" and candidates):
            if candidates:
                result.append(max(candidates, key=lambda item: (item[1].start, item[1].end, item[0]))[1])
    return tuple(result)


def pick_sources_in_range(
    sources: Iterable[GroupScheduleSource], start: date, end: date
) -> tuple[GroupScheduleSource, ...]:
    """Select an intersecting publication per category without a date fallback.

    The original document period is preserved. Prefer an exact published period,
    then the most requested days, latest period and last catalogue entry.
    This selects a publication, not individual lessons or a synthetic timetable.
    """
    if type(start) is not date or type(end) is not date or not 0 <= (end - start).days < 31:
        raise ValueError("The requested schedule period must contain 1 to 31 days")
    candidates: dict[str, list[tuple[bool, int, date, int, GroupScheduleSource]]] = {}
    for index, source in enumerate(sources):
        first, last = max(source.start, start), min(source.end, end)
        if first <= last:
            candidates.setdefault(source.category, []).append((
                source.start == start and source.end == end,
                (last - first).days + 1, source.start, index, source,
            ))
    return tuple(
        max(candidates[category], key=lambda item: item[:4])[4]
        for category in ("vo", "spo", "part_time") if category in candidates
    )


class GroupScheduleRepository:
    def __init__(
        self,
        page_url: str = GROUP_SCHEDULE_PAGE_URL,
        *,
        catalog_ttl_seconds: float = 300,
        pdf_ttl_seconds: float = 900,
        max_pdf_bytes: int = 4 * 1024 * 1024,
        max_cache_bytes: int = 40 * 1024 * 1024,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if catalog_ttl_seconds < 0 or pdf_ttl_seconds < 0 or max_pdf_bytes <= 0 or max_cache_bytes < 0:
            raise ValueError("Invalid timetable cache limits")
        self.page_url = _official_url(page_url, GROUP_SCHEDULE_PAGE_URL, pdf=False)
        self.catalog_ttl_seconds = catalog_ttl_seconds
        self.pdf_ttl_seconds = pdf_ttl_seconds
        self.max_pdf_bytes = max_pdf_bytes
        self.max_cache_bytes = max_cache_bytes
        self._session = session or requests.Session()
        self._clock = clock
        self._lock = threading.RLock()
        self._catalog: tuple[GroupScheduleSource, ...] | None = None
        self._catalog_loaded_at = 0.0
        self._pdf_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._cache_bytes = 0

    def fetch_catalog(self, force: bool = False) -> tuple[GroupScheduleSource, ...]:
        with self._lock:
            if (
                self._catalog is not None
                and not force
                and self._clock() - self._catalog_loaded_at < self.catalog_ttl_seconds
            ):
                return self._catalog
            content = self._download(self.page_url, pdf=False, limit=2 * 1024 * 1024)
            sources = discover_group_sources(content.decode("utf-8-sig", errors="replace"), self.page_url)
            if not sources:
                raise GroupSourceError("No dated group timetables were found on the official page")
            self._catalog = sources
            self._catalog_loaded_at = self._clock()
            return sources

    def get_pdf(self, source: GroupScheduleSource, force: bool = False) -> bytes:
        url = _official_url(source.url, self.page_url, pdf=True)
        with self._lock:
            cached = self._pdf_cache.get(url)
            if cached is not None and not force and self._clock() - cached[0] < self.pdf_ttl_seconds:
                self._pdf_cache.move_to_end(url)
                return cached[1]
            content = self._download(url, pdf=True, limit=self.max_pdf_bytes)
            if cached is not None:
                self._cache_bytes -= len(self._pdf_cache.pop(url)[1])
            while self._pdf_cache and self._cache_bytes + len(content) > self.max_cache_bytes:
                _, (_, removed) = self._pdf_cache.popitem(last=False)
                self._cache_bytes -= len(removed)
            if len(content) <= self.max_cache_bytes:
                self._pdf_cache[url] = self._clock(), content
                self._cache_bytes += len(content)
            return content

    def _download(self, url: str, *, pdf: bool, limit: int) -> bytes:
        deadline = time.monotonic() + 30.0
        try:
            for _ in range(5):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GroupSourceError("The official timetable exceeded its download deadline")
                # Follow redirects manually: reject an untrusted Location before
                # issuing a request to it, rather than validating only afterward.
                with self._session.get(
                    url, timeout=(min(5.0, remaining), min(15.0, remaining)), stream=True, allow_redirects=False
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise GroupSourceError("Official timetable redirect has no destination")
                        url = _official_url(location, url, pdf=pdf)
                        continue
                    response.raise_for_status()
                    _official_url(response.url or url, url, pdf=pdf)
                    content_type = response.headers.get("Content-Type", "").lower()
                    if pdf and "html" in content_type:
                        raise GroupSourceError("The official server returned HTML instead of a PDF")
                    content_length = response.headers.get("Content-Length", "")
                    if content_length.isdigit() and int(content_length) > limit:
                        raise GroupSourceError("The official timetable exceeds the download limit")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_content(chunk_size=65536):
                        if time.monotonic() > deadline:
                            raise GroupSourceError("The official timetable exceeded its download deadline")
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > limit:
                            raise GroupSourceError("The official timetable exceeds the download limit")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if pdf and not content.lstrip().startswith(b"%PDF-"):
                        raise GroupSourceError("The official server did not return a PDF document")
                    return content
            raise GroupSourceError("Too many redirects from the official timetable server")
        except requests.RequestException as exc:
            raise GroupSourceError("The official timetable server is temporarily unavailable") from exc
