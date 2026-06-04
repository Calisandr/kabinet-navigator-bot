from __future__ import annotations

import asyncio

import api.webhook as webhook
from app.bot import set_bot_commands
from app.commands import COMMANDS, build_set_my_commands_payloads


class FakeBot:
    def __init__(self) -> None:
        self.calls = []

    async def set_my_commands(self, commands, **kwargs) -> None:
        self.calls.append((commands, kwargs))


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

    assert len(application.bot.calls) == len(build_set_my_commands_payloads())
    for commands, kwargs in application.bot.calls:
        command_names = [command.command for command in commands]
        assert command_names == [item["command"] for item in COMMANDS]
        assert "stats" in command_names

    scope_types = {kwargs["scope"].type for _, kwargs in application.bot.calls}
    language_codes = {kwargs.get("language_code") for _, kwargs in application.bot.calls}
    assert scope_types == {"default", "all_private_chats"}
    assert language_codes == {None, "ru"}


def test_set_my_commands_payloads_cover_desktop_and_mobile_clients() -> None:
    payloads = build_set_my_commands_payloads()

    assert len(payloads) == 4
    assert {payload["scope"]["type"] for payload in payloads} == {
        "default",
        "all_private_chats",
    }
    assert {payload.get("language_code") for payload in payloads} == {None, "ru"}
    for payload in payloads:
        command_names = [item["command"] for item in payload["commands"]]
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
