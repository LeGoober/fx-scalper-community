"""Runtime configuration.

Everything comes from environment variables, loaded once from the project `.env`.
Values are read at call time (not cached) so key updates made through
`/api/secrets` take effect without a restart.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
ENV_PATH = Path(os.getenv("COMMUNITY_ENV_FILE") or PROJECT_DIR / ".env")
STRATEGIES_DIR = BACKEND_DIR / "strategies"

load_dotenv(ENV_PATH, override=False)

DEFAULT_DERIV_REST_URL = "https://api.derivws.com"
# Deriv's current API. The legacy wss://ws.derivws.com/websockets/v3 now answers HTTP 520.
# Authenticated sessions use OTP URLs: .../ws/demo?otp=... or .../ws/real?otp=...
DEFAULT_DERIV_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
DEFAULT_DERIV_APP_ID = "1089"  # Deriv's public test app id


def env(name: str, default: str = "", *aliases: str) -> str:
    """First non-empty value among `name` and its aliases."""
    for key in (name, *aliases):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def host() -> str:
    return env("COMMUNITY_HOST", "127.0.0.1")


def port() -> int:
    return int(env("COMMUNITY_PORT", "5001"))


def data_dir() -> Path:
    path = Path(env("COMMUNITY_DATA_DIR", str(BACKEND_DIR / "data"))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return Path(env("COMMUNITY_DB_PATH", str(data_dir() / "platform.db")))


def transcripts_dir() -> Path:
    path = data_dir() / "transcripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


# Deriv ----------------------------------------------------------------------
def deriv_app_id() -> str:
    value = env("COMMUNITY_DERIV_APP_ID", DEFAULT_DERIV_APP_ID, "DERIV_APP_ID")
    return value if value.isdigit() else DEFAULT_DERIV_APP_ID


def deriv_token() -> str:
    return env("COMMUNITY_DERIV_TOKEN", "", "DERIV_API_TOKEN")


def deriv_rest_url() -> str:
    return env("COMMUNITY_DERIV_API_URL", DEFAULT_DERIV_REST_URL).rstrip("/")


def deriv_ws_url() -> str:
    return env("COMMUNITY_DERIV_WS_URL", DEFAULT_DERIV_WS_URL)


def allow_real_trading() -> bool:
    """Real-money unlock. Only settable by hand-editing `.env`; `/api/secrets` refuses it."""
    return env_bool("COMMUNITY_ALLOW_REAL_TRADING", False)


# Jev (TypeSafe) ---------------------------------------------------------------
def typesafe_api_key() -> str:
    return env("TYPESAFE_API_KEY")


def jev_model() -> str:
    return env("COMMUNITY_JEV_MODEL", "jev-latest")


# TradingView -----------------------------------------------------------------
def tradingview_webhook_secret() -> str:
    return env("COMMUNITY_TV_WEBHOOK_SECRET")
