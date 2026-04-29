from __future__ import annotations

import os
from datetime import date

from dotenv import load_dotenv

from app.config import DEFAULT_SHEET_ID
from app.formatting import format_day_schedule
from app.schedule import GoogleSheetScheduleRepository


def main() -> None:
    load_dotenv()
    sheet_id = os.getenv("GOOGLE_SHEET_ID", DEFAULT_SHEET_ID)
    schedule = GoogleSheetScheduleRepository(sheet_id).load()
    today = date.today()

    print(f"Entries: {len(schedule.entries)}")
    print(f"Dates: {len(schedule.dates)}")
    print(f"Teachers/events: {len(schedule.teachers)}")
    print("Next dates:", ", ".join(item.isoformat() for item in schedule.next_dates(today, 8)))

    first_date = schedule.next_dates(today, 1)
    if first_date:
        print()
        print(format_day_schedule(first_date[0], schedule.entries_for_date(first_date[0])))


if __name__ == "__main__":
    main()
