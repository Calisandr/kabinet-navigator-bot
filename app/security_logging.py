"""Redact credentials before any application or third-party handler sees a log."""
from __future__ import annotations

import logging
import os
import re
import traceback


_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(?:bot)?\d{5,15}:[A-Za-z0-9_-]{8,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET", "UPSTASH_REDIS_REST_TOKEN",
    "KV_REST_API_TOKEN", "KV_REST_API_READ_ONLY_TOKEN",
)


def redact_secrets(text: str) -> str:
    for name in _SECRET_ENV_NAMES:
        secret = os.getenv(name, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return _BEARER.sub(r"\1[REDACTED]", _TOKEN.sub("[REDACTED]", text))


def install_secret_redaction() -> None:
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_bot_redacts_secrets", False):
        return

    def safe_record(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_secrets("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return record

    safe_record._bot_redacts_secrets = True
    logging.setLogRecordFactory(safe_record)
