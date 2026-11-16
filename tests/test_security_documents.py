from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from app import document_safety, group_pdf, group_sources, schedule
from app.document_safety import UnsafeDocumentError, validate_xlsx


def workbook_bytes() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Ноябрь 2026"
    sheet.append([datetime(2026, 11, 16)])
    sheet.append(["Преподаватель", 1, 2, 3, 4, 5, 6])
    sheet.append(["Иванов И.И.", 219, 219, 321])
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def replace_member(content: bytes, name: str, replacement: bytes) -> bytes:
    output = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for item in original.infolist():
            archive.writestr(item.filename, replacement if item.filename == name else original.read(item))
    return output.getvalue()


def relocate_xml_member(content: bytes, name: str, destination: str) -> bytes:
    """Keep the manifest and package relationships valid when renaming a part."""
    output = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for item in original.infolist():
            data = original.read(item)
            if item.filename.lower().endswith((".xml", ".rels")):
                data = data.replace(("/" + name).encode(), ("/" + destination).encode())
            archive.writestr(destination if item.filename == name else item.filename, data)
    return output.getvalue()


def sheet_xml(body: str) -> bytes:
    return (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + body + "</worksheet>"
    ).encode()


def test_valid_xlsx_passes_preflight_and_retains_schedule_entries(monkeypatch):
    content = workbook_bytes()
    validate_xlsx(content)
    repository = schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")
    monkeypatch.setattr(repository, "_download", lambda: content)

    result = repository.load()

    assert len(result.entries) == 3
    assert result.dates == [date(2026, 11, 16)]
    assert {entry.room for entry in result.entries} == {"219", "321"}


@pytest.mark.parametrize("destination", ["xl/worksheets/sheet1.bin", "custom/schedule.xml", "custom/schedule.bin"])
def test_actual_worksheet_relationships_allow_safe_nonstandard_part_names(monkeypatch, destination):
    content = relocate_xml_member(workbook_bytes(), "xl/worksheets/sheet1.xml", destination)
    repository = schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")
    monkeypatch.setattr(repository, "_download", lambda: content)

    result = repository.load()

    assert len(result.entries) == 3
    assert {entry.room for entry in result.entries} == {"219", "321"}


@pytest.mark.parametrize("destination", ["xl/worksheets/sheet1.bin", "custom/schedule.xml", "custom/schedule.bin"])
def test_renamed_worksheet_merge_bomb_is_rejected_before_openpyxl(monkeypatch, destination):
    content = replace_member(
        workbook_bytes(), "xl/worksheets/sheet1.xml",
        sheet_xml('<sheetData/><mergeCells><mergeCell ref="A1:IV10000"/></mergeCells>'),
    )
    content = relocate_xml_member(content, "xl/worksheets/sheet1.xml", destination)
    repository = schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")
    monkeypatch.setattr(repository, "_download", lambda: content)
    monkeypatch.setattr(schedule, "load_workbook", lambda *_args, **_kwargs: pytest.fail("Merge bomb reached openpyxl"))

    with pytest.raises(UnsafeDocumentError, match="merges too many cells"):
        repository.load()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("name,destination,root", [
    ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet1.bin", "worksheet"),
    ("xl/worksheets/sheet1.xml", "custom/schedule.bin", "worksheet"),
    ("xl/styles.xml", "custom/styles.bin", "styleSheet"),
])
def test_renamed_xml_parts_cannot_hide_dtd_from_preflight(monkeypatch, encoding, name, destination, root):
    malicious = (
        '<?xml version="1.0" encoding="' + encoding + '"?>'
        '<!DOCTYPE ' + root + ' [<!ENTITY secret SYSTEM "file:///does-not-exist">]>'
        '<' + root + ' xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">&secret;</' + root + '>'
    ).encode(encoding)
    content = relocate_xml_member(replace_member(workbook_bytes(), name, malicious), name, destination)
    repository = schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")
    monkeypatch.setattr(repository, "_download", lambda: content)
    monkeypatch.setattr(schedule, "load_workbook", lambda *_args, **_kwargs: pytest.fail("Aliased XML reached openpyxl"))

    with pytest.raises(UnsafeDocumentError):
        repository.load()


def test_workbook_sheet_cannot_disguise_its_relationship_type():
    content = workbook_bytes()
    with ZipFile(BytesIO(content)) as archive:
        rels = archive.read("xl/_rels/workbook.xml.rels")
    content = replace_member(
        content, "xl/_rels/workbook.xml.rels", rels.replace(b"/worksheet\"", b"/image\""),
    )
    with pytest.raises(UnsafeDocumentError, match="worksheet relationship"):
        validate_xlsx(content)


def test_repeated_worksheet_target_cannot_multiply_openpyxl_allocations():
    content = workbook_bytes()
    with ZipFile(BytesIO(content)) as archive:
        workbook = archive.read("xl/workbook.xml")
    # A second sheet referring to the same valid relationship would otherwise
    # load and allocate the complete worksheet again in openpyxl.
    duplicate = (
        b'<sheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        b'name="Duplicate" sheetId="2" r:id="rId1"/>'
    )
    content = replace_member(content, "xl/workbook.xml", workbook.replace(b"</sheets>", duplicate + b"</sheets>"))
    with pytest.raises(UnsafeDocumentError, match="worksheet target"):
        validate_xlsx(content)


def test_relocated_workbook_cannot_bypass_metadata_checks_with_decoy_canonical_part():
    content = relocate_xml_member(workbook_bytes(), "xl/workbook.xml", "custom/workbook.bin")
    output = BytesIO()
    with ZipFile(BytesIO(content)) as original, ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for item in original.infolist():
            archive.writestr(item.filename, original.read(item))
        archive.writestr("xl/workbook.xml", b"<decoy/>")
    with pytest.raises(UnsafeDocumentError, match="package layout"):
        validate_xlsx(output.getvalue())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_xml_doctype_and_entities_are_rejected_before_openpyxl(monkeypatch, encoding):
    malicious = (
        '<?xml version="1.0" encoding="' + encoding + '"?>'
        '<!DOCTYPE workbook [<!ENTITY secret SYSTEM "file:///does-not-exist">]>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">&secret;</workbook>'
    ).encode(encoding)
    content = replace_member(workbook_bytes(), "xl/workbook.xml", malicious)
    repository = schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")
    monkeypatch.setattr(repository, "_download", lambda: content)
    monkeypatch.setattr(schedule, "load_workbook", lambda *_args, **_kwargs: pytest.fail("Unsafe XML reached openpyxl"))

    with pytest.raises(UnsafeDocumentError):
        repository.load()


def test_small_compressed_zip_with_large_expansion_is_rejected(monkeypatch):
    content = replace_member(workbook_bytes(), "xl/worksheets/sheet1.xml", b"A" * 100_000)
    assert len(content) < 10_000
    monkeypatch.setattr(document_safety, "MAX_ZIP_MEMBER_BYTES", 50_000)
    with pytest.raises(UnsafeDocumentError, match="Unsupported file"):
        validate_xlsx(content)


def test_zip_total_expansion_and_member_count_are_bounded(monkeypatch):
    content = workbook_bytes()
    monkeypatch.setattr(document_safety, "MAX_ZIP_EXPANDED_BYTES", 1024)
    with pytest.raises(UnsafeDocumentError, match="expanded"):
        validate_xlsx(content)
    monkeypatch.setattr(document_safety, "MAX_ZIP_EXPANDED_BYTES", 40 * 1024 * 1024)
    monkeypatch.setattr(document_safety, "MAX_ZIP_MEMBERS", 2)
    with pytest.raises(UnsafeDocumentError, match="Too many files"):
        validate_xlsx(content)


@pytest.mark.parametrize(
    "body",
    [
        '<dimension ref="A1:XFD1048576"/><sheetData/>',
        '<sheetData><row r="1048576"/></sheetData>',
        '<sheetData><row r="1"><c r="XFD1"/></row></sheetData>',
        '<sheetData/><mergeCells><mergeCell ref="A1:IV10000"/></mergeCells>',
        '<dimension ref="A1:A1048576"/><sheetData/>',
        '<dimension ref="A:A"/><sheetData/>',
    ],
)
def test_declared_dimensions_actual_cells_and_merges_are_validated(body):
    content = replace_member(workbook_bytes(), "xl/worksheets/sheet1.xml", sheet_xml(body))
    with pytest.raises(UnsafeDocumentError):
        validate_xlsx(content)


def test_actual_cell_count_is_bounded_even_when_dimensions_claim_small_sheet(monkeypatch):
    body = '<dimension ref="A1:A1"/><sheetData><row r="1">' + ''.join(f'<c r="{column}1"/>' for column in "ABCD") + '</row></sheetData>'
    content = replace_member(workbook_bytes(), "xl/worksheets/sheet1.xml", sheet_xml(body))
    monkeypatch.setattr(document_safety, "MAX_WORKSHEET_CELLS", 3)
    with pytest.raises(UnsafeDocumentError, match="too many cells"):
        validate_xlsx(content)


def test_direct_workbook_parser_cannot_iterate_a_sparse_million_row_sheet():
    workbook = Workbook()
    workbook.active.cell(row=1_048_576, column=1, value="sparse")
    with pytest.raises(UnsafeDocumentError, match="dimensions"):
        schedule.parse_workbook(workbook)
    workbook.close()


def test_invalid_archive_xml_fails_safely():
    content = replace_member(
        workbook_bytes(), "xl/workbook.xml",
        b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><unclosed>',
    )
    with pytest.raises(UnsafeDocumentError, match="validated safely"):
        validate_xlsx(content)


@pytest.mark.parametrize("sheet_id", ["https://example.org/sheet", "../other", "abc1234567?format=pdf", "a/bc1234567", "a" * 129])
def test_sheet_id_cannot_inject_export_path_or_query(sheet_id):
    with pytest.raises(ValueError, match="spreadsheet ID"):
        schedule.GoogleSheetScheduleRepository(sheet_id)


class Response:
    def __init__(self, data=b"PK\x03\x04data", *, headers=None, status=200, url="", chunks=None):
        self.data, self.headers, self.status_code, self.url = data, headers or {}, status, url
        self.chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self.chunks if self.chunks is not None else [self.data]


@pytest.mark.parametrize("destination", [
    "http://127.0.0.1/private", "https://169.254.169.254/latest/meta-data/", "https://docs.google.com.evil.test/export",
    "https://user:password@docs.google.com/export", "https://docs.google.com:444/export", "https://evil.test/file.xlsx",
])
def test_google_export_rejects_untrusted_redirect_before_sending_request(monkeypatch, destination):
    response = Response(status=302, headers={"Location": destination})
    calls = []
    monkeypatch.setattr(schedule.requests, "get", lambda url, **kwargs: calls.append((url, kwargs)) or response)
    with pytest.raises(UnsafeDocumentError, match="official Google"):
        schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")._download()
    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["stream"] is True
    assert response.closed


def test_google_export_accepts_googleusercontent_download_redirect(monkeypatch):
    responses = [Response(status=302, headers={"Location": "https://doc-0a-test-sheets.googleusercontent.com/export/file"}), Response()]
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return responses.pop(0)
    monkeypatch.setattr(schedule.requests, "get", get)
    result = schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")._download()
    assert result.startswith(b"PK")
    assert len(calls) == 2


@pytest.mark.parametrize("response", [Response(headers={"Content-Length": "100"}), Response(chunks=[b"123456", b"789012"])])
def test_google_export_bounds_declared_and_streamed_bytes(monkeypatch, response):
    monkeypatch.setattr(schedule, "MAX_XLSX_BYTES", 10)
    monkeypatch.setattr(schedule.requests, "get", lambda *_args, **_kwargs: response)
    with pytest.raises(UnsafeDocumentError, match="download limit"):
        schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")._download()
    assert response.closed


def test_google_export_deadline_applies_during_streaming(monkeypatch):
    clock = [0.0]
    response = Response()
    def chunks(chunk_size):
        clock[0] = 31.0
        yield b"data"
    response.iter_content = chunks
    monkeypatch.setattr(schedule.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(schedule.requests, "get", lambda *_args, **_kwargs: response)
    with pytest.raises(UnsafeDocumentError, match="deadline"):
        schedule.GoogleSheetScheduleRepository("safe_sheet_id_123")._download()
    assert response.closed


def test_official_pdf_stream_checks_total_download_deadline(monkeypatch):
    clock = [0.0]
    response = Response()
    def chunks(chunk_size):
        clock[0] = 31.0
        yield b"%PDF-content"
    response.iter_content = chunks
    monkeypatch.setattr(group_sources.time, "monotonic", lambda: clock[0])
    repository = group_sources.GroupScheduleRepository(session=SimpleNamespace(get=lambda *_args, **_kwargs: response))
    with pytest.raises(group_sources.GroupSourceError, match="deadline"):
        repository._download("https://www.prk.kuzstu.ru/upload/file.pdf", pdf=True, limit=100)
    assert response.closed


@pytest.mark.parametrize("rectangle,scale", [((0, 0, 4000, 4000), 8), ((0, 0, float("inf"), 10), 1), ((0, 0, 10, 10), float("nan"))])
def test_pdf_bitmap_budget_is_checked_before_native_render(rectangle, scale):
    page = SimpleNamespace(get_size=lambda: (4000, 4000), render=lambda **_kwargs: pytest.fail("Unsafe bitmap reached native PDFium"))
    with pytest.raises(group_pdf.UnsupportedTimetableError):
        group_pdf._render_rectangle(page, rectangle, scale)


def test_pdf_object_count_has_an_upper_bound(monkeypatch):
    monkeypatch.setattr(group_pdf, "MAX_PAGE_OBJECTS", 2)
    fake_object = SimpleNamespace(type=-1)
    page = SimpleNamespace(get_size=lambda: (600, 1000), get_objects=lambda: iter([fake_object] * 3))
    with pytest.raises(group_pdf.UnsupportedTimetableError, match="too many drawing objects"):
        group_pdf._table_geometry(page)
