from __future__ import annotations

from app.config import load_settings
from app.telegram_admin import redact_admin_output, run_admin_action, telegram_api_call


def main() -> None:
    run_admin_action(_delete_webhook)


def _delete_webhook() -> None:
    settings = load_settings()
    result = telegram_api_call(
        settings.telegram_bot_token, "deleteWebhook", {"drop_pending_updates": False}
    )
    print(redact_admin_output(result, settings.telegram_bot_token, settings.webhook_secret))


if __name__ == "__main__":
    main()
