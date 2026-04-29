from __future__ import annotations

from app.commands import COMMANDS


def test_command_descriptions_are_not_mojibake() -> None:
    descriptions = [item["description"] for item in COMMANDS]

    assert all("?" not in item for item in descriptions)
    assert any("преподавател" in item for item in descriptions)
