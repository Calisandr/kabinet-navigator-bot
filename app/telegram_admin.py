"""Small, bounded Telegram API client for operator-only administration scripts."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from collections.abc import Callable
from urllib.parse import quote, urlsplit, urlunsplit

import requests


ADMIN_METHODS = frozenset({"getWebhookInfo", "setWebhook", "setMyCommands", "deleteWebhook"})
MAX_ADMIN_RESPONSE_BYTES = 64 * 1024
ADMIN_REQUEST_TIMEOUT = (3.0, 5.0)
_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{1,256}\Z")
_REDACT_TOKEN = re.compile(r"(?:bot)?[0-9]{1,20}:[A-Za-z0-9_-]{8,256}")
_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class TelegramAdminError(RuntimeError):
    """Safe operator message that contains no endpoint credentials."""


def validate_webhook_secret(value: str) -> str:
    if not isinstance(value, str) or _SECRET.fullmatch(value) is None:
        raise TelegramAdminError(
            "WEBHOOK_SECRET is required and must contain 1-256 letters, digits, underscores or hyphens."
        )
    return value


def normalize_webhook_url(value: str) -> str:
    try:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError
        if any(ord(character) <= 32 for character in value) or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        hostname = parsed.hostname.rstrip(".") if parsed.hostname else ""
        if (
            parsed.scheme != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or parsed.path not in ("", "/", "/api/webhook", "/api/webhook/")
        ):
            raise ValueError
        hostname = hostname.encode("idna").decode("ascii").lower()
        labels = hostname.split(".")
        if (
            len(labels) < 2 or len(hostname) > 253
            or not all(_DNS_LABEL.fullmatch(label) for label in labels)
            or hostname.endswith((".localhost", ".local", ".internal", ".home"))
        ):
            raise ValueError
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            try:
                socket.inet_aton(hostname)
            except OSError:
                pass
            else:
                raise ValueError
        else:
            raise ValueError
        return urlunsplit(("https", hostname, "/api/webhook", "", ""))
    except (TypeError, ValueError, UnicodeError):
        raise TelegramAdminError(
            "Webhook URL must use HTTPS and a public domain, without credentials, parameters or a custom port. "
            "Use the site root or /api/webhook."
        ) from None


def redact_admin_output(value: object, *secrets: str) -> str:
    text = str(value)
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED]")
            encoded = quote(secret, safe="")
            if encoded != secret:
                text = re.sub(re.escape(encoded), "[REDACTED]", text, flags=re.IGNORECASE)
    return _REDACT_TOKEN.sub("[REDACTED]", text)


def telegram_api_call(token: str, method: str, payload: dict | None = None) -> object:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise TelegramAdminError("TELEGRAM_BOT_TOKEN has an invalid format.")
    if method not in ADMIN_METHODS:
        raise TelegramAdminError("This Telegram administration method is not supported.")
    response = None
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload or {},
            timeout=ADMIN_REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
        if 300 <= response.status_code < 400:
            raise TelegramAdminError("Telegram API redirects are not permitted.")
        response.raise_for_status()
        declared_size = response.headers.get("Content-Length", "")
        if declared_size.isdigit() and int(declared_size) > MAX_ADMIN_RESPONSE_BYTES:
            raise TelegramAdminError("Telegram API response exceeds the allowed size.")
        content = bytearray()
        for chunk in response.iter_content(chunk_size=4096):
            content.extend(chunk)
            if len(content) > MAX_ADMIN_RESPONSE_BYTES:
                raise TelegramAdminError("Telegram API response exceeds the allowed size.")
        data = json.loads(content)
    except requests.RequestException:
        raise TelegramAdminError("Telegram API request failed. Check connectivity and bot configuration.") from None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise TelegramAdminError("Telegram API returned an invalid response.") from None
    finally:
        if response is not None:
            response.close()
    if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
        raise TelegramAdminError("Telegram API rejected the administration request. Check bot configuration.")
    return data["result"]


def run_admin_action(action: Callable[[], None]) -> None:
    try:
        action()
    except TelegramAdminError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("Telegram administration failed. Check local configuration and try again.") from None
