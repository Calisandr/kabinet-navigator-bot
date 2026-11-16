from __future__ import annotations

import asyncio
import http.client
import io
import json
import socket
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import Image
from telegram.ext import Application
from telegram.request import BaseRequest

from api import webhook
from app import bot as bot_module
from app.config import Settings
from app.group_schedule import GroupOption, GroupTimetable
from app.group_sources import GroupScheduleSource
from app.schedule import Schedule, ScheduleEntry
from app.stats import MemoryUserStatsStore


FAKE_TOKEN = "123456:FAKE_TOKEN_FOR_OFFLINE_TEST"
SECRET = "offline_test_secret"
SETTINGS = Settings(
    telegram_bot_token=FAKE_TOKEN, google_sheet_id="offline_sheet", timezone="UTC",
    cache_minutes=15, webhook_secret=SECRET, webhook_url="https://example.invalid",
    admin_user_ids=frozenset(), user_stats_db_path="unused", upstash_redis_rest_url="",
    upstash_redis_rest_token="", user_stats_key_prefix="offline",
)


def message_payload(command="/start", *, update_id=1, user_id=9001):
    message = {
        "message_id": update_id, "date": 1700000000,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Offline User"},
        "text": command,
    }
    if command.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command.split()[0])}]
    return {"update_id": update_id, "message": message}


def callback_payload(data="date:2026-11-16", *, update_id=2):
    message = message_payload()["message"]
    message["from"] = {"id": 123456, "is_bot": True, "first_name": "Offline Bot"}
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"query-{update_id}", "chat_instance": "offline-instance",
            "from": {"id": 9001, "is_bot": False, "first_name": "Offline User"},
            "message": message, "data": data,
        },
    }


@pytest.fixture(autouse=True)
def isolated_webhook(monkeypatch):
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(webhook, "_runner_loop", loop)
    monkeypatch.setattr(webhook, "_runner_lock", threading.Lock())
    monkeypatch.setattr(webhook, "_completed_updates", webhook.OrderedDict())
    monkeypatch.setattr(webhook, "_settings", SETTINGS)
    monkeypatch.setattr(webhook, "_application", None)
    yield
    application = webhook._application
    if application is not None:
        loop.run_until_complete(application.shutdown())
    loop.close()


class QuietHandler(webhook.handler):
    def log_message(self, _format, *args):
        pass


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def post(server, payload=None, *, raw=None, headers=None, headers_only=False):
    body = raw if raw is not None else json.dumps(payload).encode()
    request_headers = {"Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": SECRET}
    if headers:
        request_headers.update(headers)
    connection = http.client.HTTPConnection(*server.server_address, timeout=4)
    try:
        if headers_only:
            # Expect rejection from the headers before uploading any body.
            # Closing a Windows socket with unread body bytes may reset it.
            request_headers.setdefault("Content-Length", str(len(body)))
            connection.putrequest("POST", "/api/webhook")
            for name, value in request_headers.items():
                connection.putheader(name, value)
            connection.endheaders()
        else:
            connection.request("POST", "/api/webhook", body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        connection.close()


@pytest.mark.parametrize("secret", ["", "incorrect", "секрет"])
def test_unauthenticated_requests_are_rejected_before_body_read(monkeypatch, secret):
    instance = object.__new__(webhook.handler)
    instance.headers = {"X-Telegram-Bot-Api-Secret-Token": secret}
    instance.rfile = SimpleNamespace(read=lambda _: pytest.fail("An unauthorized request body must not be read"))
    responses = []
    instance._send_json = lambda status, body: responses.append((status.value, body))

    instance.do_POST()

    assert responses == [(403, {"ok": False, "error": "bad secret"})]


def test_missing_server_secret_is_fail_closed_without_initializing_bot(monkeypatch, http_server):
    monkeypatch.setattr(webhook, "_settings", SimpleNamespace(webhook_secret=""))
    monkeypatch.setattr(webhook, "build_application", lambda *_: pytest.fail("No bot initialization without authentication"))

    status, body, _ = post(http_server, message_payload(), headers_only=True)

    assert status == 503 and body == {"ok": False, "error": "unavailable"}


@pytest.mark.parametrize(
    "raw,status",
    [
        (b"", 400), (b"{", 400), (b"\xff", 400),
        (b"[]", 400), (b"null", 400), (b"1", 400),
        (b'{"update_id": 1, "update_id": 2}', 400),
        (b'{"update_id": 1, "value": NaN}', 400),
        (b'{"update_id": 1, "value": Infinity}', 400),
        (b'{"update_id": 1, "value": 1e10000}', 400),
        (b'{"update_id": ' + b"1" * 5000 + b'}', 400),
        (b'{"update_id": 1, "value": "\\ud800"}', 400),
        (b'{"update_id": 1, "value":' + b"[" * 40 + b"0" + b"]" * 40 + b"}", 400),
        (b'{"update_id": 1, "value":[' + b"0," * 20000 + b"0]}", 400),
        (b" " * (webhook.MAX_WEBHOOK_BODY_BYTES + 1), 413),
    ],
)
def test_malformed_and_oversized_json_is_rejected_over_real_http(monkeypatch, http_server, raw, status):
    monkeypatch.setattr(webhook, "build_application", lambda *_: pytest.fail("Malformed JSON must not reach Telegram"))

    actual, body, headers = post(http_server, raw=raw, headers_only=len(raw) > webhook.MAX_WEBHOOK_BODY_BYTES)

    assert actual == status
    assert body["ok"] is False
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Server"] == "Webhook"


def raw_post(server, header_lines, *, body=b""):
    with socket.create_connection(server.server_address, timeout=3) as connection:
        connection.settimeout(3)
        request = (
            "POST /api/webhook HTTP/1.1\r\nHost: localhost\r\n"
            f"X-Telegram-Bot-Api-Secret-Token: {SECRET}\r\n"
            + "\r\n".join(header_lines) + "\r\n\r\n"
        ).encode("ascii") + body
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status, json.loads(response.read())


@pytest.mark.parametrize(
    "headers,body,status",
    [
        ([], b"", 411),
        (["Content-Length: -1"], b"", 400),
        (["Content-Length: abc"], b"", 400),
        (["Content-Length: 00000000001"], b"x", 400),
        (["Content-Length: 2", "Content-Length: 2"], b"{}", 411),
        (["Transfer-Encoding: chunked"], b"0\r\n\r\n", 400),
        (["Content-Length: 10"], b"{}", 400),
        (["Content-Length: 2", "Content-Type: text/plain"], b"{}", 415),
    ],
)
def test_ambiguous_body_framing_is_rejected_without_reading_to_eof(http_server, headers, body, status):
    actual, payload = raw_post(http_server, headers, body=body)
    assert actual == status and payload["ok"] is False


def test_duplicate_secret_headers_are_rejected(monkeypatch, http_server):
    monkeypatch.setattr(webhook, "build_application", lambda *_: pytest.fail("Duplicate auth headers must not initialize the bot"))
    status, _ = raw_post(http_server, [f"X-Telegram-Bot-Api-Secret-Token: {SECRET}", "Content-Length: 2"], body=b"{}")
    assert status == 403


def test_authenticated_slow_body_is_bounded_by_read_timeout(monkeypatch, http_server):
    monkeypatch.setattr(webhook, "BODY_READ_TIMEOUT_SECONDS", 0.05)
    status, body, _ = post(http_server, raw=b"{}", headers={"Content-Length": "100"})
    assert status == 408 and body == {"ok": False, "error": "body timeout"}


@pytest.mark.parametrize(
    "path,value",
    [
        (("update_id",), True), (("update_id",), -1), (("update_id",), "1"),
        (("message", "from", "id"), "1 OR 1=1"),
        (("message", "from", "id"), True),
        (("message", "from", "id"), 2**60),
        (("message", "chat", "id"), 0),
        (("message", "chat", "type"), "file://etc/passwd"),
        (("message", "text"), ["/start"]),
        (("message", "text"), "A" * 4097),
        (("message", "date"), 2**60),
        (("message", "entities"), [{"type": "bot_command", "offset": -1, "length": 6}]),
        (("message", "entities"), [{"type": "bot_command", "offset": 0, "length": 7000}]),
    ],
)
def test_invalid_update_identity_and_command_entities_never_reach_application(monkeypatch, http_server, path, value):
    payload = message_payload()
    target = payload
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    monkeypatch.setattr(webhook, "build_application", lambda *_: pytest.fail("Invalid update must not reach the application"))

    status, _, _ = post(http_server, payload)

    assert status == 400


@pytest.mark.parametrize("data", ["А" * 33, 42, ""])
def test_invalid_callback_data_is_rejected(monkeypatch, http_server, data):
    monkeypatch.setattr(webhook, "build_application", lambda *_: pytest.fail("Invalid callback must not reach the application"))
    status, _, _ = post(http_server, callback_payload(data))
    assert status == 400


def test_unused_telegram_update_types_are_acknowledged_without_bot_or_database_access(monkeypatch, http_server):
    monkeypatch.setattr(webhook, "build_application", lambda *_: pytest.fail("Unused updates need no bot initialization"))
    for update_id, kind in enumerate(("edited_message", "my_chat_member", "message_reaction", "widget_query"), 100):
        status, body, _ = post(http_server, {"update_id": update_id, kind: {"id": "future-compatible-field"}})
        assert status == 200 and body == {"ok": True}


def test_runtime_errors_do_not_expose_tokens_or_raw_exception_details(monkeypatch, http_server, caplog):
    async def fail(_payload):
        raise RuntimeError(f"POST https://api.telegram.org/bot{FAKE_TOKEN}/getMe failed; private data")
    monkeypatch.setattr(webhook, "process_telegram_update", fail)

    status, body, _ = post(http_server, message_payload())

    assert status == 500 and body == {"ok": False, "error": "internal error"}
    assert FAKE_TOKEN not in caplog.text
    assert "private data" not in caplog.text


def test_busy_runner_returns_retryable_response_without_unbounded_wait(monkeypatch, http_server):
    monkeypatch.setattr(webhook, "RUNNER_WAIT_SECONDS", 0.01)
    webhook._runner_lock.acquire()
    try:
        status, body, _ = post(http_server, message_payload())
    finally:
        webhook._runner_lock.release()
    assert status == 503 and body == {"ok": False, "error": "busy"}


def test_retried_successful_delivery_is_processed_once_and_cache_is_bounded(monkeypatch, http_server):
    calls = []
    async def process(payload):
        calls.append(payload["update_id"])
    monkeypatch.setattr(webhook, "process_telegram_update", process)
    monkeypatch.setattr(webhook, "MAX_REPLAY_CACHE_ITEMS", 2)
    for update_id in (1, 1, 2, 3, 3):
        status, _, _ = post(http_server, message_payload(update_id=update_id))
        assert status == 200
    assert calls == [1, 2, 3]
    assert list(webhook._completed_updates) == [2, 3]


def test_failed_delivery_is_retried_instead_of_marked_completed(monkeypatch, http_server):
    calls = []
    async def process(payload):
        calls.append(payload["update_id"])
        if len(calls) == 1:
            raise TimeoutError("temporary transport failure")
    monkeypatch.setattr(webhook, "process_telegram_update", process)

    assert post(http_server, message_payload())[0] == 500
    assert post(http_server, message_payload())[0] == 200
    assert calls == [1, 1]


class FakeTelegramRequest(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if endpoint == "getMe":
            result = {"id": 123456, "is_bot": True, "first_name": "Offline Bot", "username": "offline_bot"}
        elif endpoint in {"sendMessage", "sendPhoto"}:
            result = {"message_id": 1, "date": 0, "chat": {"id": 9001, "type": "private"}, "text": "offline response"}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.fixture
def application_transport(monkeypatch):
    transport = FakeTelegramRequest()
    builder = Application.builder().request(transport).get_updates_request(FakeTelegramRequest())
    monkeypatch.setattr(Application, "builder", lambda: builder)
    monkeypatch.setattr(bot_module, "build_user_stats_store", lambda _: MemoryUserStatsStore())
    application = bot_module.build_application(SETTINGS)
    today = datetime.now(timezone.utc).date()
    schedule = Schedule(
        entries=(ScheduleEntry(today, "Offline Teacher", "1", "219", "Offline", 1),),
        loaded_at=datetime.now(),
    )
    class FakeCache:
        async def get(self, **kwargs):
            return schedule
    application.bot_data["schedule_cache"] = FakeCache()
    output = io.BytesIO()
    with Image.new("RGB", (32, 32), "white") as image:
        image.save(output, "PNG")
    source = GroupScheduleSource("Offline timetable", "spo", "https://www.prk.kuzstu.ru/upload/offline.pdf", today, today + timedelta(days=6))
    class FakeGroupService:
        def list_groups(self, *args, **kwargs):
            return (GroupOption("ИСт-241.2", "spo", "СПО", source.start, source.end),)
        def get_timetable(self, *args, **kwargs):
            return GroupTimetable("ИСт-241.2", source, (io.BytesIO(output.getvalue()),))
    application.bot_data["group_schedule_service"] = FakeGroupService()
    monkeypatch.setattr(webhook, "build_application", lambda _: application)
    return transport


def test_all_existing_commands_cross_real_http_to_real_ptb_handlers_without_external_network(http_server, application_transport):
    commands = (
        "/start", "/help", "/today", "/tomorrow", "/yesterday", "/next", "/date завтра",
        "/teacher Offline", "/teachers", "/refresh", "/stats", "/groups спо", "/group ИСт-241.2",
    )
    for update_id, command in enumerate(commands, 200):
        before = len(application_transport.calls)
        status, body, _ = post(http_server, message_payload(command, update_id=update_id, user_id=9001 + update_id))
        assert status == 200 and body == {"ok": True}
        assert any(item in {"sendMessage", "sendPhoto"} for item in application_transport.calls[before:]), command
    assert "sendPhoto" in application_transport.calls
    assert "sendMessage" in application_transport.calls


def test_callback_flow_crosses_http_and_keeps_group_button_and_date_button_working(http_server, application_transport):
    for update_id, data in enumerate(("date:2026-11-16", "gl:spo:0"), 400):
        status, body, _ = post(http_server, callback_payload(data, update_id=update_id))
        assert status == 200 and body == {"ok": True}
    assert application_transport.calls.count("answerCallbackQuery") == 2
    assert "sendPhoto" in application_transport.calls


@pytest.mark.parametrize("command", ["/start", "/today", "/group ИСт-241.2"])
def test_anonymous_admin_commands_cross_http_to_real_handlers(http_server, application_transport, command):
    payload = message_payload(command, update_id=450)
    payload["message"]["from"] = {"id": 1087968824, "is_bot": True, "first_name": "Group"}
    group = {"id": -1001234567890, "type": "supergroup", "title": "Students"}
    payload["message"]["chat"] = group
    payload["message"]["sender_chat"] = group

    status, body, _ = post(http_server, payload)

    assert status == 200 and body == {"ok": True}
    assert any(method in {"sendMessage", "sendPhoto"} for method in application_transport.calls)


def test_nontext_media_and_anonymous_group_messages_remain_valid_updates(monkeypatch, http_server):
    calls = []
    async def process(payload):
        calls.append(payload)
    monkeypatch.setattr(webhook, "process_telegram_update", process)
    payload = message_payload(update_id=500)
    payload["message"].pop("text")
    payload["message"].pop("entities")
    payload["message"].pop("from")
    payload["message"]["chat"] = {"id": -100123456789, "type": "supergroup"}
    payload["message"]["photo"] = [{"file_id": "offline-file", "file_unique_id": "offline", "width": 100, "height": 100}]

    status, body, _ = post(http_server, payload)

    assert status == 200 and body == {"ok": True}
    assert calls == [payload]
