from __future__ import annotations

import asyncio

import api.webhook as webhook
from app.bot import set_bot_commands
from app.commands import COMMANDS


class FakeBot:
    def __init__(self) -> None:
        self.commands = None

    async def set_my_commands(self, commands) -> None:
        self.commands = commands


class FakeApplication:
    def __init__(self) -> None:
        self.bot = FakeBot()
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True


def test_command_descriptions_are_not_mojibake() -> None:
    descriptions = [item["description"] for item in COMMANDS]

    assert all("?" not in item for item in descriptions)
    assert any("преподавател" in item for item in descriptions)


def test_stats_command_is_registered_for_telegram_suggestions() -> None:
    application = FakeApplication()

    asyncio.run(set_bot_commands(application))

    command_names = [command.command for command in application.bot.commands]
    assert command_names == [item["command"] for item in COMMANDS]
    assert "stats" in command_names


def test_webhook_initialization_registers_bot_commands(monkeypatch) -> None:
    settings = object()
    application = FakeApplication()
    calls = []

    def fake_build_application(received_settings):
        calls.append(("build", received_settings))
        return application

    async def fake_set_bot_commands(received_application) -> None:
        calls.append(("commands", received_application))

    monkeypatch.setattr(webhook, "_application", None)
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook, "build_application", fake_build_application)
    monkeypatch.setattr(webhook, "set_bot_commands", fake_set_bot_commands)

    result = asyncio.run(webhook.get_application())

    assert result is application
    assert application.initialized is True
    assert calls == [("build", settings), ("commands", application)]
