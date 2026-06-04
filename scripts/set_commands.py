from __future__ import annotations

import requests

from app.config import load_settings
from app.commands import build_set_my_commands_payloads


def main() -> None:
    settings = load_settings()
    for payload in build_set_my_commands_payloads():
        response = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/setMyCommands",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        print(payload.get("scope"), payload.get("language_code"), response.json())


if __name__ == "__main__":
    main()
