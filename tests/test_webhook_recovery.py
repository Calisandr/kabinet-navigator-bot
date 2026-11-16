from __future__ import annotations

import asyncio
import json

import pytest
from telegram.error import TimedOut
from telegram.ext import Application, CommandHandler
from telegram.request import BaseRequest

from api import webhook
from app.bot import start


class FakeRequest(BaseRequest):
    def __init__(self, fail_endpoint: str | None = None) -> None:
        self.fail_endpoint = fail_endpoint
        self.calls: list[str] = []
        self.shutdown_calls = 0

    @property
    def read_timeout(self) -> float:
        return 5.0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        self.shutdown_calls += 1

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if endpoint == self.fail_endpoint:
            self.fail_endpoint = None
            raise TimedOut("simulated one-time startup timeout")
        if endpoint == "getMe":
            result = {
                "id": 123456,
                "is_bot": True,
                "first_name": "Offline Test Bot",
                "username": "offline_test_bot",
            }
        elif endpoint == "sendMessage":
            result = {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 9001, "type": "private"},
                "text": "offline response",
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.mark.parametrize("fail_endpoint", ["getMe", "setMyCommands"])
def test_webhook_retries_startup_and_replies_to_start(monkeypatch, fail_endpoint) -> None:
    applications: list[Application] = []
    requests: list[tuple[FakeRequest, FakeRequest]] = []

    def build_application(_settings):
        api_request = FakeRequest(fail_endpoint if not applications else None)
        updates_request = FakeRequest()
        application = (
            Application.builder()
            .token("123456:FAKE_TOKEN_FOR_OFFLINE_TEST")
            .request(api_request)
            .get_updates_request(updates_request)
            .build()
        )
        application.add_handler(CommandHandler("start", start))
        applications.append(application)
        requests.append((api_request, updates_request))
        return application

    monkeypatch.setattr(webhook, "_application", None)
    monkeypatch.setattr(webhook, "get_settings", lambda: None)
    monkeypatch.setattr(webhook, "build_application", build_application)
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": 9001, "type": "private"},
            "from": {"id": 9001, "is_bot": False, "first_name": "Offline User"},
            "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        },
    }

    async def run_scenario():
        with pytest.raises(TimedOut, match="one-time startup timeout"):
            await webhook.process_telegram_update(payload)
        assert webhook._application is None
        assert all(request.shutdown_calls == 1 for request in requests[0])

        try:
            await webhook.process_telegram_update(payload)
            assert webhook._application is applications[1]
            assert requests[1][0].calls == [
                "getMe",
                "setMyCommands",
                "setMyCommands",
                "setMyCommands",
                "setMyCommands",
                "sendMessage",
            ]
            assert await webhook.get_application() is applications[1]
            assert len(applications) == 2
        finally:
            await applications[1].shutdown()

    asyncio.run(run_scenario())
