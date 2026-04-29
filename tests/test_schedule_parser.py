from __future__ import annotations

from datetime import date

from openpyxl import Workbook

from app.schedule import parse_workbook


def test_parse_sheet_with_excel_dates_and_teacher_names() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "МАЙ 2026"
    worksheet["B1"] = "Расписание компьютерных кабинетов"
    worksheet["C5"] = "суббота 02"
    worksheet["G5"] = date(2026, 5, 2)
    worksheet["B6"] = 1
    worksheet["C6"] = 2
    worksheet["D6"] = 3
    worksheet["E6"] = 4
    worksheet["A7"] = "Григорьев М. Ю."
    worksheet["B7"] = 321
    worksheet["C7"] = 321
    worksheet["D7"] = 203

    entries = parse_workbook(workbook)

    assert len(entries) == 3
    assert entries[0].day == date(2026, 5, 2)
    assert entries[0].teacher == "Григорьев М.Ю."
    assert entries[0].lesson == "1"
    assert entries[0].room == "321"
