from __future__ import annotations

import pytest

from app import config


@pytest.mark.parametrize(
    "legacy_url,legacy_token,marketplace_url,marketplace_token,expected",
    [
        ("https://legacy.invalid", "legacy", "https://market.invalid", "market", ("https://legacy.invalid", "legacy")),
        ("", "", "https://market.invalid", "market", ("https://market.invalid", "market")),
        ("https://legacy.invalid", "", "https://market.invalid", "market", ("https://market.invalid", "market")),
        ("https://legacy.invalid", "", "", "market", ("https://legacy.invalid", "")),
        ("  ", " ", " https://market.invalid ", " market ", ("https://market.invalid", "market")),
    ],
)
def test_redis_credentials_accept_marketplace_without_mixing_pairs(
    monkeypatch, legacy_url, legacy_token, marketplace_url, marketplace_token, expected
):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:OFFLINE_TEST_TOKEN")
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", legacy_url)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", legacy_token)
    monkeypatch.setenv("KV_REST_API_URL", marketplace_url)
    monkeypatch.setenv("KV_REST_API_TOKEN", marketplace_token)

    settings = config.load_settings()

    assert (settings.upstash_redis_rest_url, settings.upstash_redis_rest_token) == expected
