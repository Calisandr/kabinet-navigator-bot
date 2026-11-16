from __future__ import annotations

import sys

from app.config import load_settings
from app.commands import build_set_my_commands_payloads
from app.telegram_admin import (
    TelegramAdminError,
    normalize_webhook_url,
    redact_admin_output,
    run_admin_action,
    telegram_api_call,
    validate_webhook_secret,
)


def main() -> None:
    run_admin_action(_set_webhook)


def _set_webhook() -> None:
    settings = load_settings()
    webhook_url = sys.argv[1].strip() if len(sys.argv) > 1 else settings.webhook_url
    if not webhook_url:
        raise TelegramAdminError(
            "Pass Vercel URL as an argument or set WEBHOOK_URL in .env. "
            "Example: python -m scripts.set_webhook https://name.vercel.app"
        )

    webhook_url = normalize_webhook_url(webhook_url)
    webhook_secret = validate_webhook_secret(settings.webhook_secret)

    webhook_payload = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
        "secret_token": webhook_secret,
    }
    set_webhook = telegram_api_call(settings.telegram_bot_token, "setWebhook", webhook_payload)

    set_commands_results = []
    for payload in build_set_my_commands_payloads():
        set_commands = telegram_api_call(settings.telegram_bot_token, "setMyCommands", payload)
        set_commands_results.append(
            {
                "scope": payload.get("scope"),
                "language_code": payload.get("language_code"),
                "result": set_commands,
            }
        )

    info = telegram_api_call(settings.telegram_bot_token, "getWebhookInfo")

    print("Webhook URL:", redact_admin_output(webhook_url, settings.telegram_bot_token, webhook_secret))
    print("setWebhook:", redact_admin_output(set_webhook, settings.telegram_bot_token, webhook_secret))
    print("setMyCommands:", redact_admin_output(set_commands_results, settings.telegram_bot_token, webhook_secret))
    print("getWebhookInfo:", redact_admin_output(info, settings.telegram_bot_token, webhook_secret))


if __name__ == "__main__":
    main()
