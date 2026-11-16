from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import threading
import time
from collections import OrderedDict
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
MAX_WEBHOOK_BODY_BYTES = 256 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 16000
MAX_TELEGRAM_ID = (1 << 52) - 1
RUNNER_WAIT_SECONDS = 1.0
BODY_READ_TIMEOUT_SECONDS = 10.0
REPLAY_CACHE_SECONDS = 24 * 60 * 60
MAX_REPLAY_CACHE_ITEMS = 4096
_completed_updates: OrderedDict[int, float] = OrderedDict()


class InvalidWebhook(ValueError):
    def __init__(self, status: HTTPStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class WebhookBusy(RuntimeError):
    pass


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad json")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad json")


def _validate_json_tree(payload) -> None:
    stack = [(payload, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad json")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
            stack.extend((key, depth + 1) for key in value)
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad json") from exc
        elif isinstance(value, float) and not math.isfinite(value):
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad json")


def _integer(value, *, minimum: int = 0, maximum: int = MAX_TELEGRAM_ID) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _validate_user(user) -> None:
    if (
        not isinstance(user, dict)
        or not _integer(user.get("id"), minimum=1)
        or type(user.get("is_bot")) is not bool
        or not isinstance(user.get("first_name"), str)
    ):
        raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")


def _validate_message(message) -> None:
    if not isinstance(message, dict) or not _integer(message.get("message_id")):
        raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
    chat = message.get("chat")
    if (
        not isinstance(chat, dict)
        or not _integer(chat.get("id"), minimum=-MAX_TELEGRAM_ID)
        or chat.get("id") == 0
        or chat.get("type") not in {"private", "group", "supergroup", "channel"}
        or not _integer(message.get("date"), maximum=253402300799)
    ):
        raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
    if "from" in message:
        _validate_user(message["from"])
    text = message.get("text")
    if text is not None and (not isinstance(text, str) or len(text) > 4096):
        raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
    if "entities" in message:
        entities = message["entities"]
        if not isinstance(entities, list) or len(entities) > 4096:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
        text_length = len((text or "").encode("utf-16-le")) // 2
        for entity in entities:
            if (
                not isinstance(entity, dict)
                or not isinstance(entity.get("type"), str)
                or not _integer(entity.get("offset"), maximum=text_length)
                or not _integer(entity.get("length"), minimum=1, maximum=text_length)
                or entity["offset"] + entity["length"] > text_length
            ):
                raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")


def validate_update(payload) -> bool:
    """Validate handled update fields; acknowledge genuine unused types cheaply."""
    if not isinstance(payload, dict) or not _integer(payload.get("update_id")):
        raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
    supported = [name for name in ("message", "callback_query") if name in payload]
    if len(supported) > 1:
        raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
    if "message" in payload:
        _validate_message(payload["message"])
        return True
    if "callback_query" in payload:
        query = payload["callback_query"]
        if not isinstance(query, dict):
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
        _validate_user(query.get("from"))
        if not isinstance(query.get("id"), str) or not 1 <= len(query["id"]) <= 256:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
        if not isinstance(query.get("chat_instance"), str) or len(query["chat_instance"]) > 256:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
        data = query.get("data")
        if data is not None and (not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64):
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad update")
        if "message" in query:
            _validate_message(query["message"])
        return True
    return False


class handler(BaseHTTPRequestHandler):
    def version_string(self) -> str:
        return "Webhook"

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
            if not settings.webhook_secret:
                LOGGER.error("Webhook authentication is not configured")
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "unavailable"})
                return
            if not self._has_valid_secret(settings.webhook_secret):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "bad secret"})
                return

            payload = self._read_json_body()
            if validate_update(payload):
                run_async(_process_webhook_update(payload))
            self._send_json(HTTPStatus.OK, {"ok": True})
        except InvalidWebhook as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.reason})
        except WebhookBusy:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "busy"})
        except Exception as exc:
            # HTTP exception messages/tracebacks may contain a Telegram token.
            LOGGER.error("Webhook processing failed (%s)", type(exc).__name__)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "internal error"},
            )

    def _read_json_body(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad body framing")
        if hasattr(self.headers, "get_all") and len(self.headers.get_all("Content-Length", [])) != 1:
            raise InvalidWebhook(HTTPStatus.LENGTH_REQUIRED, "content length required")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise InvalidWebhook(HTTPStatus.LENGTH_REQUIRED, "content length required")
        if not raw_length.isascii() or not raw_length.isdecimal() or len(raw_length) > 9:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad content length")
        content_length = int(raw_length)
        if content_length > MAX_WEBHOOK_BODY_BYTES:
            raise InvalidWebhook(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        if content_length == 0:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad json")
        content_type = self.headers.get("Content-Type", "application/json").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise InvalidWebhook(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json required")
        connection = getattr(self, "connection", None)
        set_timeout = getattr(connection, "settimeout", None)
        if callable(set_timeout):
            set_timeout(BODY_READ_TIMEOUT_SECONDS)
        try:
            raw_body = self.rfile.read(content_length)
        except TimeoutError as exc:
            raise InvalidWebhook(HTTPStatus.REQUEST_TIMEOUT, "body timeout") from exc
        if len(raw_body) != content_length:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "incomplete body")
        try:
            payload = json.loads(
                raw_body.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            _validate_json_tree(payload)
            return payload
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise InvalidWebhook(HTTPStatus.BAD_REQUEST, "bad json") from exc

    def _has_valid_secret(self, expected: str) -> bool:
        if hasattr(self.headers, "get_all") and len(self.headers.get_all("X-Telegram-Bot-Api-Secret-Token", [])) != 1:
            return False
        received = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        return hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.close_connection = True
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
        application = build_application(get_settings())
        try:
            await application.initialize()
            await set_bot_commands(application)
        except BaseException:
            try:
                try:
                    await application.shutdown()
                finally:
                    # Application.shutdown() skips a partially initialized bot.
                    await application.bot.shutdown()
            except Exception as exc:
                LOGGER.error("Application startup cleanup failed (%s)", type(exc).__name__)
            raise
        _application = application
    return _application


async def process_telegram_update(payload: dict) -> None:
    application = await get_application()
    update = Update.de_json(payload, application.bot)
    if update is None:
        return
    await application.process_update(update)


async def _process_webhook_update(payload: dict) -> None:
    # Serialized by run_async(). Retried delivery must not repeat a successful
    # command in this process. The cache is bounded and is not a global ledger.
    now = time.monotonic()
    while _completed_updates and next(iter(_completed_updates.values())) < now - REPLAY_CACHE_SECONDS:
        _completed_updates.popitem(last=False)
    update_id = payload["update_id"]
    if update_id in _completed_updates:
        return
    await process_telegram_update(payload)
    _completed_updates[update_id] = time.monotonic()
    while len(_completed_updates) > MAX_REPLAY_CACHE_ITEMS:
        _completed_updates.popitem(last=False)


def run_async(coro) -> object:
    if not _runner_lock.acquire(timeout=RUNNER_WAIT_SECONDS):
        coro.close()
        raise WebhookBusy("Webhook processor is busy")
    try:
        return _runner_loop.run_until_complete(coro)
    finally:
        _runner_lock.release()
