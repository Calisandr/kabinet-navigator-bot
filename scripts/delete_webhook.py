from __future__ import annotations

import requests

from app.config import load_settings


def main() -> None:
    settings = load_settings()
    api_base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    response = requests.post(
        f"{api_base}/deleteWebhook",
        json={"drop_pending_updates": False},
        timeout=30,
    )
    response.raise_for_status()
    print(response.json())


if __name__ == "__main__":
    main()
