"""Write-only secret management backed by the project `.env`.

Values go in, but only `is_set` comes back out: no endpoint ever returns a
secret. The real-money unlock (`COMMUNITY_ALLOW_REAL_TRADING`) is deliberately
not managed here, so the UI can never flip it.
"""
from __future__ import annotations

import os
import secrets as pysecrets
from dataclasses import dataclass

from dotenv import set_key, unset_key

from app import config


@dataclass(frozen=True)
class SecretSpec:
    name: str
    env: str
    label: str
    group: str  # deriv | jev | tradingview | openbb | notifications
    secret: bool = True
    help: str = ""


SPECS: dict[str, SecretSpec] = {
    s.name: s
    for s in [
        SecretSpec("deriv_token", "COMMUNITY_DERIV_TOKEN", "Deriv API token", "deriv",
                   help="Create at app.deriv.com → API token with Read + Trade scopes only."),
        SecretSpec("deriv_app_id", "COMMUNITY_DERIV_APP_ID", "Deriv app ID", "deriv", secret=False,
                   help="Numeric app id; 1089 is Deriv's public test id."),
        SecretSpec("typesafe_api_key", "TYPESAFE_API_KEY", "TypeSafe (Jev) API key", "jev",
                   help="From console.typesafe.ai."),
        SecretSpec("tradingview_webhook_secret", "COMMUNITY_TV_WEBHOOK_SECRET", "TradingView webhook secret",
                   "tradingview", help="Shared secret TradingView alerts must include. Rotate to generate."),
        SecretSpec("fmp_api_key", "FMP_API_KEY", "FMP API key (OpenBB)", "openbb"),
        SecretSpec("fred_api_key", "FRED_API_KEY", "FRED API key (OpenBB)", "openbb"),
        SecretSpec("tiingo_token", "TIINGO_TOKEN", "Tiingo token (OpenBB)", "openbb"),
        SecretSpec("discord_webhook_url", "DISCORD_WEBHOOK_URL", "Discord webhook URL", "notifications"),
        SecretSpec("telegram_bot_token", "TELEGRAM_BOT_TOKEN", "Telegram bot token", "notifications"),
        SecretSpec("telegram_chat_id", "TELEGRAM_CHAT_ID", "Telegram chat ID", "notifications", secret=False),
    ]
}


class SecretError(ValueError):
    pass


def _spec(name: str) -> SecretSpec:
    spec = SPECS.get(name)
    if spec is None:
        raise SecretError(f"Unknown secret '{name}'.")
    return spec


def status() -> list[dict]:
    return [
        {
            "name": s.name,
            "label": s.label,
            "group": s.group,
            "secret": s.secret,
            "help": s.help,
            "is_set": bool(os.getenv(s.env, "").strip()),
            # Non-secret config values may be shown; secrets never are.
            "value": None if s.secret else os.getenv(s.env, "").strip() or None,
        }
        for s in SPECS.values()
    ]


def _ensure_env_file() -> None:
    path = config.ENV_PATH
    if not path.exists():
        path.write_text("# Local secrets: never commit this file.\n", encoding="utf-8")


def set_secret(name: str, value: str) -> None:
    spec = _spec(name)
    value = (value or "").strip()
    if not value:
        raise SecretError("Value is empty. Use DELETE to clear a secret.")
    if "\n" in value or "\r" in value:
        raise SecretError("Value must be a single line.")
    _ensure_env_file()
    set_key(str(config.ENV_PATH), spec.env, value, quote_mode="always")
    os.environ[spec.env] = value


def clear_secret(name: str) -> None:
    spec = _spec(name)
    if config.ENV_PATH.exists():
        unset_key(str(config.ENV_PATH), spec.env, quote_mode="always")
    os.environ.pop(spec.env, None)


def rotate_webhook_secret() -> str:
    """Generate a fresh TradingView secret. Returned once so it can be pasted into the alert."""
    value = pysecrets.token_urlsafe(24)
    set_secret("tradingview_webhook_secret", value)
    return value
