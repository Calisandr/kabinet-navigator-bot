from __future__ import annotations

import requests

from app.config import load_settings
from app.commands import COMMANDS


def main() -> None:
    settings = load_settings()
    response = requests.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/setMyCommands",
        json={"commands": COMMANDS},
        timeout=30,
    )
    response.raise_for_status()
    print(response.json())


if __name__ == "__main__":
    main()
