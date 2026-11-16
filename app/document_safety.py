"""Bound office-document work before loading it into the spreadsheet parser."""
from __future__ import annotations

import posixpath
import re
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile
from defusedxml.ElementTree import ParseError, iterparse
from defusedxml.common import DefusedXmlException
from openpyxl.utils.cell import range_boundaries


MAX_XLSX_BYTES = 8 * 1024 * 1024
MAX_ZIP_MEMBERS = 1024
MAX_ZIP_EXPANDED_BYTES = 40 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 8 * 1024 * 1024
MAX_WORKSHEETS = 64
MAX_WORKSHEET_ROWS = 10_000
MAX_WORKSHEET_COLUMNS = 256
MAX_TOTAL_WORKSHEET_ROWS = 50_000
MAX_WORKSHEET_CELLS = 200_000
MAX_MERGED_CELLS = 200_000
MAX_XML_ELEMENTS = 500_000
_CELL = re.compile(r"[A-Za-z]{1,3}[1-9][0-9]{0,6}\Z")
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_PACKAGE_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORKBOOK_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_WORKBOOK_CONTENT_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    "application/vnd.ms-excel.template.macroEnabled.main+xml",
})


class UnsafeDocumentError(ValueError):
    """The source exceeds the supported document limits or contains unsafe XML."""


def validate_sheet_dimensions(rows: int, columns: int) -> None:
    if not 0 <= rows <= MAX_WORKSHEET_ROWS or not 0 <= columns <= MAX_WORKSHEET_COLUMNS:
        raise UnsafeDocumentError("The schedule worksheet exceeds supported dimensions")


def _range_size(reference: str) -> tuple[int, int]:
    parts = reference.split(":")
    if len(parts) not in (1, 2) or not all(_CELL.fullmatch(item) for item in parts):
        raise UnsafeDocumentError("Invalid schedule worksheet cell range")
    try:
        left, top, right, bottom = range_boundaries(reference)
        validate_sheet_dimensions(bottom, right)
        if top > bottom or left > right:
            raise ValueError("Reversed range")
    except (TypeError, ValueError) as exc:
        raise UnsafeDocumentError("Invalid schedule worksheet cell range") from exc
    return (right - left + 1) * (bottom - top + 1), bottom


def _metadata_elements(archive: ZipFile, name: str, root_tag: str):
    """Read package metadata with the same XML protections as document parts."""
    count = 0
    with archive.open(name) as source:
        for event, element in iterparse(
            source, events=("start", "end"),
            forbid_dtd=True, forbid_entities=True, forbid_external=True,
        ):
            if event == "end":
                if element.text is not None and len(element.text) > 32767:
                    raise UnsafeDocumentError("The workbook contains an oversized text value")
                element.clear()
                continue
            if count == 0 and element.tag != root_tag:
                raise UnsafeDocumentError("Unsupported workbook package metadata")
            count += 1
            if count > MAX_XML_ELEMENTS:
                raise UnsafeDocumentError("The schedule workbook contains too many XML elements")
            yield element.tag, element.attrib


def _xml_parts(archive: ZipFile, names: set[str]) -> set[str]:
    """Content types, rather than filename suffixes, identify XML package parts."""
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for tag, attributes in _metadata_elements(
        archive, "[Content_Types].xml", f"{{{_CONTENT_TYPES_NS}}}Types",
    ):
        if tag == f"{{{_CONTENT_TYPES_NS}}}Default":
            extension, content_type = attributes.get("Extension", ""), attributes.get("ContentType", "")
            if not extension or extension in defaults or len(defaults) > MAX_ZIP_MEMBERS:
                raise UnsafeDocumentError("Unsupported workbook content types")
            defaults[extension] = content_type
        elif tag == f"{{{_CONTENT_TYPES_NS}}}Override":
            part_name, content_type = attributes.get("PartName", ""), attributes.get("ContentType", "")
            if not part_name.startswith("/") or part_name[1:] not in names or part_name in overrides:
                raise UnsafeDocumentError("Unsupported workbook content types")
            overrides[part_name] = content_type
    # openpyxl selects its workbook from the manifest, so checking only for the
    # presence of xl/workbook.xml would leave a relocated workbook unchecked.
    workbook_parts = {name for name, content_type in overrides.items() if content_type in _WORKBOOK_CONTENT_TYPES}
    if workbook_parts != {"/xl/workbook.xml"}:
        raise UnsafeDocumentError("Unsupported workbook package layout")

    result = set()
    for name in names:
        content_type = overrides.get("/" + name, defaults.get(name.rsplit(".", 1)[-1], "")).lower()
        if (
            name.lower().endswith((".xml", ".rels"))
            or content_type.endswith("+xml")
            or content_type in {"application/xml", "text/xml", "application/vnd.openxmlformats-officedocument.vmldrawing"}
        ):
            result.add(name)
    return result


def _worksheet_parts(archive: ZipFile, names: set[str]) -> set[str]:
    """Resolve the actual worksheets that openpyxl will load, including aliases."""
    relationships: dict[str, dict[str, str]] = {}
    for tag, attributes in _metadata_elements(
        archive, "xl/_rels/workbook.xml.rels", f"{{{_PACKAGE_RELS_NS}}}Relationships",
    ):
        if tag != f"{{{_PACKAGE_RELS_NS}}}Relationship":
            continue
        identifier = attributes.get("Id", "")
        if not identifier or identifier in relationships or len(relationships) >= MAX_ZIP_MEMBERS:
            raise UnsafeDocumentError("Unsupported workbook relationships")
        relationships[identifier] = dict(attributes)

    result = set()
    for tag, attributes in _metadata_elements(archive, "xl/workbook.xml", f"{{{_WORKBOOK_NS}}}workbook"):
        if tag != f"{{{_WORKBOOK_NS}}}sheet":
            continue
        relationship = relationships.get(attributes.get(f"{{{_OFFICE_RELS_NS}}}id", ""), {})
        target = relationship.get("Target", "")
        if (
            relationship.get("Type") != f"{_OFFICE_RELS_NS}/worksheet"
            or relationship.get("TargetMode", "Internal") != "Internal"
            or not target or "\\" in target
        ):
            raise UnsafeDocumentError("Unsupported workbook worksheet relationship")
        name = target[1:] if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        if name not in names or name in result:
            raise UnsafeDocumentError("Unsupported workbook worksheet target")
        result.add(name)
        if len(result) > MAX_WORKSHEETS:
            raise UnsafeDocumentError("Too many schedule worksheets")
    if not result:
        raise UnsafeDocumentError("The workbook contains no schedule worksheets")
    return result


def validate_xlsx(content: bytes) -> None:
    """Reject expanded ZIP/XML bombs and oversized worksheet ranges before openpyxl."""
    if len(content) > MAX_XLSX_BYTES or not content.startswith(b"PK\x03\x04"):
        raise UnsafeDocumentError("The schedule source is not a supported XLSX document")
    try:
        with ZipFile(BytesIO(content)) as archive:
            members = archive.infolist()
            if not 1 <= len(members) <= MAX_ZIP_MEMBERS:
                raise UnsafeDocumentError("Too many files in the schedule workbook")
            names: set[str] = set()
            expanded = 0
            for item in members:
                path = PurePosixPath(item.filename)
                if (
                    item.filename in names or path.is_absolute() or ".." in path.parts
                    or "\\" in item.filename or item.flag_bits & 1
                    or not 0 <= item.file_size <= MAX_ZIP_MEMBER_BYTES
                ):
                    raise UnsafeDocumentError("Unsupported file in the schedule workbook")
                names.add(item.filename)
                expanded += item.file_size
                if expanded > MAX_ZIP_EXPANDED_BYTES:
                    raise UnsafeDocumentError("The expanded schedule workbook is too large")
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise UnsafeDocumentError("The source is not an Excel workbook")

            xml_parts = _xml_parts(archive, names)
            worksheet_parts = _worksheet_parts(archive, names)
            xml_parts.update(worksheet_parts)
            sheet_count = cells = merged_cells = elements = total_rows = 0
            for item in members:
                if item.filename not in xml_parts:
                    continue
                worksheet = item.filename in worksheet_parts
                if worksheet:
                    sheet_count += 1
                    if sheet_count > MAX_WORKSHEETS:
                        raise UnsafeDocumentError("Too many schedule worksheets")
                largest_row = 0
                with archive.open(item) as source:
                    for event, element in iterparse(
                        source, events=("start", "end"),
                        forbid_dtd=True, forbid_entities=True, forbid_external=True,
                    ):
                        if event == "end":
                            if element.text is not None and len(element.text) > 32767:
                                raise UnsafeDocumentError("The workbook contains an oversized text value")
                            element.clear()
                            continue
                        elements += 1
                        if elements > MAX_XML_ELEMENTS:
                            raise UnsafeDocumentError("The schedule workbook contains too many XML elements")
                        if not worksheet:
                            continue
                        tag = element.tag.rsplit("}", 1)[-1]
                        if tag in {"dimension", "mergeCell"}:
                            size, last_row = _range_size(element.attrib.get("ref", ""))
                            largest_row = max(largest_row, last_row)
                            if tag == "mergeCell":
                                merged_cells += size
                                if merged_cells > MAX_MERGED_CELLS:
                                    raise UnsafeDocumentError("The schedule workbook merges too many cells")
                        elif tag == "row":
                            row = int(element.attrib.get("r", "0"))
                            validate_sheet_dimensions(row, 0)
                            largest_row = max(largest_row, row)
                        elif tag == "c":
                            cells += 1
                            if cells > MAX_WORKSHEET_CELLS:
                                raise UnsafeDocumentError("The schedule workbook contains too many cells")
                            _, last_row = _range_size(element.attrib.get("r", ""))
                            largest_row = max(largest_row, last_row)
                total_rows += largest_row
                if total_rows > MAX_TOTAL_WORKSHEET_ROWS:
                    raise UnsafeDocumentError("The schedule workbook contains too many rows")
    except UnsafeDocumentError:
        raise
    except (BadZipFile, DefusedXmlException, ParseError, OSError, ValueError, KeyError, RuntimeError) as exc:
        raise UnsafeDocumentError("The schedule workbook could not be validated safely") from exc
