"""Phase 1: API surface, secret handling, demo lock, legacy parity."""
import pytest

from app.legacy import core
from app.services import risk
from app.services.deriv.client import DerivClient


def test_health_and_legacy_bootstrap_shape(client):
    assert client.get("/api/health").json()["ok"] is True
    data = client.get("/api/bootstrap").json()
    for key in ("settings", "market", "signals", "portfolio", "trade_history", "rules", "rebalance_plan",
                "setup", "stats"):
        assert key in data


def test_token_is_write_only_everywhere(client, tmp_path):
    secret = "pat_THIS_MUST_NEVER_BE_RETURNED"
    assert client.put("/api/secrets/deriv_token", json={"value": secret}).status_code == 200
    # the old form posting a token also lands in the secret store, not in state
    client.post("/api/config", json={"deriv_token": secret, "risk_profile": "aggressive"})
    for path in ("/api/bootstrap", "/api/secrets", "/api/broker/status"):
        assert secret not in client.get(path).text, path
    listed = {s["name"]: s for s in client.get("/api/secrets").json()["secrets"]}
    assert listed["deriv_token"]["is_set"] is True and listed["deriv_token"]["value"] is None
    assert client.get("/api/bootstrap").json()["settings"]["deriv_token_set"] is True
    assert secret in (tmp_path / ".env").read_text()  # stored only in the local .env


def test_real_money_unlock_is_not_settable_through_api(client):
    assert client.put("/api/secrets/allow_real", json={"value": "true"}).status_code == 400
    r = client.post("/api/risk/real/arm", json={"confirm_phrase": "I ACCEPT REAL MONEY RISK"})
    assert r.status_code == 403  # .env does not allow it


def _intent(is_virtual, stake=1.0):
    return risk.OrderIntent(symbol="frxEURUSD", stake=stake, contract_type="MULTUP", is_virtual=is_virtual)


def test_demo_lock_is_fail_closed():
    risk.check_order(_intent(True))  # demo passes
    with pytest.raises(risk.RiskBlocked):
        risk.check_order(_intent(None))  # unknown → blocked
    with pytest.raises(risk.RiskBlocked):
        risk.check_order(_intent(False))  # real, not unlocked → blocked


def test_real_needs_env_and_arm(monkeypatch):
    monkeypatch.setenv("COMMUNITY_ALLOW_REAL_TRADING", "true")
    with pytest.raises(risk.RiskBlocked):
        risk.check_order(_intent(False))  # env alone is not enough
    risk.arm_real("I ACCEPT REAL MONEY RISK")
    risk.check_order(_intent(False))
    risk.disarm_real()


def test_limits_and_kill_switch():
    with pytest.raises(risk.RiskBlocked):
        risk.check_order(_intent(True, stake=999))
    risk.set_kill_switch(True)
    with pytest.raises(risk.RiskBlocked):
        risk.check_order(_intent(True))


@pytest.mark.parametrize("endpoint,account,expected", [
    ("demo", {"account_type": "demo"}, True),
    ("real", {"account_type": "real"}, False),
    ("demo", {"account_type": "real"}, None),        # contradiction → unknown → blocked
    ("legacy", {"is_virtual": 1, "loginid": "VRTC1"}, True),
    ("legacy", {"is_virtual": 0, "loginid": "CR1"}, False),
    ("unknown", {"account_id": "x"}, None),
    ("public", {}, None),
])
def test_is_virtual_signals(endpoint, account, expected):
    c = DerivClient(token="")
    c.endpoint_kind, c.account = endpoint, account
    assert c.is_virtual is expected


def test_legacy_rule_scenarios_unchanged():
    summary = core.backtest_summary(core.DEFAULT_STATE)
    assert summary["scenario_passes"] == summary["scenario_total"] == 3
