from __future__ import annotations

from app.config import load_settings
from app.commands import build_set_my_commands_payloads
from app.telegram_admin import redact_admin_output, run_admin_action, telegram_api_call


def main() -> None:
    run_admin_action(_set_commands)


def _set_commands() -> None:
    settings = load_settings()
    for payload in build_set_my_commands_payloads():
        result = telegram_api_call(
            settings.telegram_bot_token, "setMyCommands", payload
        )
        print(
            payload.get("scope"), payload.get("language_code"),
            redact_admin_output(result, settings.telegram_bot_token, settings.webhook_secret),
        )


if __name__ == "__main__":
    main()
