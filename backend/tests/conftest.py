import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own DB, data dir and .env; real keys never leak into tests."""
    monkeypatch.setenv("COMMUNITY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COMMUNITY_DB_PATH", str(tmp_path / "data" / "test.db"))
    env_file = tmp_path / ".env"
    monkeypatch.setenv("COMMUNITY_ENV_FILE", str(env_file))
    for name in ("COMMUNITY_DERIV_TOKEN", "DERIV_API_TOKEN", "TYPESAFE_API_KEY", "COMMUNITY_ALLOW_REAL_TRADING",
                 "COMMUNITY_TV_WEBHOOK_SECRET", "FMP_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from app import config
    monkeypatch.setattr(config, "ENV_PATH", env_file)
    from app.services import risk
    risk.set_kill_switch(False)
    risk.disarm_real()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


os.environ.setdefault("COMMUNITY_HOST", "127.0.0.1")
os.environ["COMMUNITY_NO_OPENBB_WARM"] = "1"
