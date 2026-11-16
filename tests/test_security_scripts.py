from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from app import telegram_admin
from scripts import delete_webhook, set_commands, set_webhook


TOKEN = "123456:FAKE_PRIVATE_ADMIN_TOKEN"
SECRET = "offline_webhook_secret"


class Response:
    def __init__(self, payload=None, *, status=200, headers=None, chunks=None, error=None):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks if chunks is not None else [json.dumps(payload).encode()]
        self.error = error
        self.closed = False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


def settings(**changes):
    result = dict(
        telegram_bot_token=TOKEN, webhook_secret=SECRET,
        webhook_url="https://example.vercel.app",
    )
    result.update(changes)
    return SimpleNamespace(**result)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.vercel.app", "https://example.vercel.app/api/webhook"),
        ("https://example.vercel.app/", "https://example.vercel.app/api/webhook"),
        ("https://example.vercel.app/api/webhook", "https://example.vercel.app/api/webhook"),
        ("https://example.vercel.app/api/webhook/", "https://example.vercel.app/api/webhook"),
        ("https://EXAMPLE.vercel.app:443", "https://example.vercel.app/api/webhook"),
        ("https://example.vercel.app.", "https://example.vercel.app/api/webhook"),
    ],
)
def test_webhook_normalization_preserves_the_expected_public_endpoint(url, expected):
    assert set_webhook.normalize_webhook_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "", "http://example.vercel.app", "https://user:secret@example.vercel.app",
        "https://example.vercel.app?token=secret", "https://example.vercel.app/#secret",
        "https://example.vercel.app:8443", "https://example.vercel.app/custom/path",
        "https://example.vercel.app/api/%77ebhook", "https://example.vercel.app/api/../webhook",
        "https://localhost", "https://device.local", "https://x.localhost", "https://host.internal",
        "https://127.0.0.1", "https://127.1", "https://0177.0.0.1", "https://127.0.0.1.",
        "https://169.254.169.254", "https://[::1]", "https://8.8.8.8", "https://invalid_domain.example",
        "https://example.vercel.app\n", " https://example.vercel.app", "https://example.vercel.app\\@evil.invalid",
    ],
)
def test_unsafe_webhook_urls_are_rejected_without_echoing_user_supplied_credentials(url):
    with pytest.raises(telegram_admin.TelegramAdminError) as error:
        telegram_admin.normalize_webhook_url(url)
    assert "user:secret" not in str(error.value)
    assert "token=secret" not in str(error.value)


@pytest.mark.parametrize("secret", ["", None, "space forbidden", "кириллица", "x" * 257, "line\nbreak", "<tag>"])
def test_webhook_secret_is_required_and_uses_telegram_allowed_characters(secret):
    with pytest.raises(telegram_admin.TelegramAdminError, match="WEBHOOK_SECRET"):
        telegram_admin.validate_webhook_secret(secret)


@pytest.mark.parametrize("secret", ["x", "a_b-c_123", "X" * 256])
def test_supported_webhook_secrets_are_accepted(secret):
    assert telegram_admin.validate_webhook_secret(secret) == secret


def test_set_webhook_keeps_pending_updates_and_sends_secret_before_any_other_admin_call(monkeypatch, capsys):
    monkeypatch.setattr(set_webhook, "load_settings", settings)
    monkeypatch.setattr(set_webhook.sys, "argv", ["set_webhook", "https://example.vercel.app/"])
    calls = []

    def fake_call(token, method, payload=None):
        calls.append((token, method, payload))
        if method == "getWebhookInfo":
            return {"url": "https://example.vercel.app/api/webhook", "last_error_message": TOKEN + " " + SECRET}
        return True

    monkeypatch.setattr(set_webhook, "telegram_api_call", fake_call)
    set_webhook.main()

    assert calls[0] == (TOKEN, "setWebhook", {
        "url": "https://example.vercel.app/api/webhook",
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
        "secret_token": SECRET,
    })
    assert [method for _, method, _ in calls] == ["setWebhook"] + ["setMyCommands"] * 4 + ["getWebhookInfo"]
    text = capsys.readouterr().out
    assert "[REDACTED]" in text
    assert TOKEN not in text and SECRET not in text


@pytest.mark.parametrize("secret", ["", "bad secret"])
def test_set_webhook_invalid_secret_cannot_make_a_network_change(monkeypatch, secret):
    monkeypatch.setattr(set_webhook, "load_settings", lambda: settings(webhook_secret=secret))
    monkeypatch.setattr(set_webhook.sys, "argv", ["set_webhook"])
    monkeypatch.setattr(set_webhook, "telegram_api_call", lambda *a, **k: pytest.fail("No API call is allowed without a valid secret"))
    with pytest.raises(SystemExit, match="WEBHOOK_SECRET"):
        set_webhook.main()


def test_delete_webhook_retains_queued_updates_and_redacts_output(monkeypatch, capsys):
    monkeypatch.setattr(delete_webhook, "load_settings", settings)
    calls = []
    monkeypatch.setattr(delete_webhook, "telegram_api_call", lambda *args: calls.append(args) or {"private": TOKEN})
    delete_webhook.main()
    assert calls == [(TOKEN, "deleteWebhook", {"drop_pending_updates": False})]
    assert TOKEN not in capsys.readouterr().out


def test_set_commands_uses_all_supported_scopes_and_redacts_output(monkeypatch, capsys):
    monkeypatch.setattr(set_commands, "load_settings", settings)
    calls = []
    monkeypatch.setattr(set_commands, "telegram_api_call", lambda *args: calls.append(args) or {"private": TOKEN})
    set_commands.main()
    assert len(calls) == 4
    assert all(call[0] == TOKEN and call[1] == "setMyCommands" for call in calls)
    assert TOKEN not in capsys.readouterr().out


def test_admin_transport_is_fixed_to_telegram_with_bounded_streaming_and_no_redirect(monkeypatch):
    calls = []
    response = Response({"ok": True, "result": {"pending_update_count": 2}})
    monkeypatch.setattr(telegram_admin.requests, "post", lambda url, **kwargs: calls.append((url, kwargs)) or response)

    result = telegram_admin.telegram_api_call(TOKEN, "getWebhookInfo")

    assert result == {"pending_update_count": 2}
    assert calls[0][0] == f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo"
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["stream"] is True
    assert calls[0][1]["timeout"] == telegram_admin.ADMIN_REQUEST_TIMEOUT
    assert response.closed


@pytest.mark.parametrize("method", ["sendMessage", "../getMe", "getWebhookInfo?token=x", "https://attacker.invalid"])
def test_operator_method_names_cannot_change_endpoint_or_trigger_arbitrary_actions(monkeypatch, method):
    monkeypatch.setattr(telegram_admin.requests, "post", lambda *a, **k: pytest.fail("No request expected"))
    with pytest.raises(telegram_admin.TelegramAdminError, match="method"):
        telegram_admin.telegram_api_call(TOKEN, method)


@pytest.mark.parametrize("token", ["", "123:abc/../getMe", "123:abc?x=secret", "123:abc\n", "123:abc#secret", "https://attacker.invalid/token"])
def test_bot_token_is_path_safe_before_building_the_fixed_endpoint(monkeypatch, token):
    monkeypatch.setattr(telegram_admin.requests, "post", lambda *a, **k: pytest.fail("No request expected"))
    with pytest.raises(telegram_admin.TelegramAdminError, match="TOKEN"):
        telegram_admin.telegram_api_call(token, "getWebhookInfo")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_admin_redirects_never_forward_a_bot_token(monkeypatch, status):
    response = Response(status=status, headers={"Location": "https://attacker.invalid/steal"})
    calls = []
    monkeypatch.setattr(telegram_admin.requests, "post", lambda *a, **k: calls.append((a, k)) or response)
    with pytest.raises(telegram_admin.TelegramAdminError, match="redirects"):
        telegram_admin.telegram_api_call(TOKEN, "getWebhookInfo")
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False
    assert response.closed


@pytest.mark.parametrize(
    "response",
    [
        Response({"ok": False, "description": TOKEN, "error_code": 401}),
        Response({"ok": 1, "result": True}), Response({"ok": True}), Response([]),
        Response(chunks=[b"invalid " + TOKEN.encode()]), Response(chunks=[b"\xff"]),
        Response(chunks=[b"[" * 2000 + b"1" + b"]" * 2000]),
        Response({"ok": True, "result": True}, headers={"Content-Length": str(telegram_admin.MAX_ADMIN_RESPONSE_BYTES + 1)}),
        Response(chunks=[b"x" * (telegram_admin.MAX_ADMIN_RESPONSE_BYTES + 1)]),
        Response(error=requests.HTTPError(f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo")),
    ],
)
def test_admin_http_and_api_errors_fail_closed_without_exposing_tokens(monkeypatch, response):
    monkeypatch.setattr(telegram_admin.requests, "post", lambda *a, **k: response)
    with pytest.raises(telegram_admin.TelegramAdminError) as error:
        telegram_admin.telegram_api_call(TOKEN, "getWebhookInfo")
    assert TOKEN not in str(error.value)
    assert response.closed


@pytest.mark.parametrize("script", [set_webhook, delete_webhook, set_commands])
def test_cli_configuration_and_transport_failure_exit_without_secret_traceback(monkeypatch, script, capsys):
    def failure():
        raise RuntimeError("private configuration " + TOKEN + " " + SECRET)

    monkeypatch.setattr(script, "load_settings", failure)
    with pytest.raises(SystemExit) as error:
        script.main()
    assert TOKEN not in str(error.value) and SECRET not in str(error.value)
    assert capsys.readouterr().err == ""


def test_connection_error_is_sanitized_before_it_can_be_printed(monkeypatch):
    def failure(*args, **kwargs):
        raise requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/setWebhook")

    monkeypatch.setattr(telegram_admin.requests, "post", failure)
    with pytest.raises(telegram_admin.TelegramAdminError, match="request failed") as error:
        telegram_admin.telegram_api_call(TOKEN, "setWebhook")
    assert TOKEN not in str(error.value)
    assert error.value.__suppress_context__


def test_console_redaction_covers_plain_and_url_encoded_token_values():
    encoded = TOKEN.replace(":", "%3A")
    text = telegram_admin.redact_admin_output(
        {"plain": TOKEN, "encoded": encoded, "encoded_lower": encoded.replace("%3A", "%3a"), "secret": SECRET},
        TOKEN, SECRET,
    )
    assert TOKEN not in text and encoded not in text and SECRET not in text
    assert "[REDACTED]" in text
