from __future__ import annotations

import asyncio
from datetime import date

from app.config import parse_admin_user_ids
from app.stats import SQLiteUserStatsStore


def test_sqlite_user_stats_counts_unique_users(tmp_path) -> None:
    store = SQLiteUserStatsStore(tmp_path / "stats.sqlite3")
    today = date(2026, 6, 4)
    yesterday = date(2026, 6, 3)

    asyncio.run(store.record_user(1, yesterday))
    asyncio.run(store.record_user(1, today))
    asyncio.run(store.record_user(2, today))

    stats = asyncio.run(store.get_stats(today))

    assert stats.total_users == 2
    assert stats.active_today == 2
    assert stats.active_now == 2
    assert stats.storage_name == "sqlite"
    assert stats.is_persistent is True


def test_parse_admin_user_ids_accepts_commas_and_semicolons() -> None:
    assert parse_admin_user_ids("123, 456;789") == frozenset({123, 456, 789})
