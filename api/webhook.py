from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

from telegram import Update
from telegram.ext import Application

from app.bot import build_application, set_bot_commands
from app.config import Settings, load_settings


LOGGER = logging.getLogger(__name__)

_runner_loop = asyncio.new_event_loop()
_runner_lock = threading.Lock()
_application: Application | None = None
_settings: Settings | None = None


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "Kabinet Navigator Telegram webhook",
                "endpoint": "/api/webhook",
            },
        )

    def do_POST(self) -> None:
        try:
            settings = get_settings()
            if settings.webhook_secret and not self._has_valid_secret(settings.webhook_secret):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "bad secret"})
                return

            payload = self._read_json_body()
            run_async(process_telegram_update(payload))
            self._send_json(HTTPStatus.OK, {"ok": True})
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad json"})
        except Exception as exc:
            LOGGER.exception("Webhook processing failed")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": type(exc).__name__},
            )

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("content-length", "0"))
        raw_body = self.rfile.read(content_length)
        return json.loads(raw_body.decode("utf-8"))

    def _has_valid_secret(self, expected: str) -> bool:
        received = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        return hmac.compare_digest(received, expected)

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


async def get_application() -> Application:
    global _application
    if _application is None:
        _application = build_application(get_settings())
        await _application.initialize()
        await set_bot_commands(_application)
    return _application


async def process_telegram_update(payload: dict) -> None:
    application = await get_application()
    update = Update.de_json(payload, application.bot)
    if update is None:
        return
    await application.process_update(update)


def run_async(coro) -> object:
    with _runner_lock:
        return _runner_loop.run_until_complete(coro)
