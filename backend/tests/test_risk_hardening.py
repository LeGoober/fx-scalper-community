"""Phase 0 hardening: controls that must hold before the desk may trade on its own."""
import asyncio
import importlib
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.services import engine as E
from app.services import risk


def _intent(risk_amount, stake=1.0):
    return risk.OrderIntent(symbol="frxEURUSD", stake=stake, contract_type="MULTUP", is_virtual=True,
                            risk_amount=risk_amount)


def _open_trade(trade_id, mode="demo", risk_amount=1.5, contract_id="123", status="open"):
    with db.connect() as conn:
        conn.execute("INSERT INTO trades(id, mode, symbol, direction, contract_type, stake, entry_price, stop_price, "
                     "target_price, status, opened_at, contract_id, risk_amount) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (trade_id, mode, "frxEURUSD", "long", "multiplier", 5.0, 1.1, 1.098, 1.105, status,
                      db.utc_now(), contract_id, risk_amount))


def test_kill_switch_survives_a_restart():
    risk.set_kill_switch(True)
    importlib.reload(risk)  # a fresh process: module state is gone, the stored switch is not
    assert risk.kill_switch_active() is True
    with pytest.raises(risk.RiskBlocked, match="Kill switch"):
        risk.check_order(_intent(1.0))
    risk.set_kill_switch(False)


def test_open_risk_counts_toward_the_daily_loss_limit():
    risk.set_limits({"max_daily_loss": 2.0})
    _open_trade("t-open", risk_amount=1.5)
    assert risk.today_stats()["open_risk"] == 1.5
    with pytest.raises(risk.RiskBlocked, match="stopped out"):
        risk.check_order(_intent(1.0))   # 1.5 open + 1.0 new > 2.0
    risk.check_order(_intent(0.4))       # 1.9 fits


def test_limits_reset_on_the_new_york_trading_day():
    ny = ZoneInfo("America/New_York")
    before = datetime(2026, 9, 28, 16, 59, tzinfo=ny).timestamp()
    after = datetime(2026, 9, 28, 17, 1, tzinfo=ny).timestamp()
    assert risk.trading_day(before) == "2026-09-28"
    assert risk.trading_day(after) == "2026-09-29"   # 17:00 New York rollover, not UTC midnight


def test_size_for_risk_rounds_down_and_never_silently_risks_more():
    size, at_risk = risk.size_for_risk(10.0, entry=1.1000, stop=1.0980, value_per_point=1.0, size_step=100,
                                       min_size=100)
    assert size == 5000 and at_risk == pytest.approx(10.0)
    size, at_risk = risk.size_for_risk(10.0, entry=1.1000, stop=1.0983, value_per_point=1.0, size_step=100,
                                       min_size=100)
    assert size == 5800 and at_risk <= 10.0      # 5882 rounded DOWN to the step
    with pytest.raises(risk.RiskBlocked, match="Minimum size"):
        risk.size_for_risk(0.05, entry=1.1000, stop=1.0980, value_per_point=1.0, size_step=100, min_size=100)
    size, _ = risk.size_for_risk(0.2, entry=1.1000, stop=1.0990, value_per_point=1.0, size_step=100,
                                 min_size=200)  # minimum risks 0.2: within budget, allowed
    assert size == 200


def test_a_second_process_cannot_take_the_trading_lock(tmp_path):
    from app import instance_lock
    lock = tmp_path / "backend.lock"
    assert instance_lock.acquire(lock) and instance_lock.owned()
    code = ("import sys; sys.path.insert(0, '.'); from pathlib import Path; from app import instance_lock; "
            f"print(instance_lock.acquire(Path(r'{lock}')))")
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert other.stdout.strip() == "False", other.stderr
    instance_lock.release()
    assert not instance_lock.owned()


def test_engine_refuses_broker_modes_without_the_trading_lock():
    from app import instance_lock
    instance_lock.release()
    eng = E.LiveEngine()
    with pytest.raises(RuntimeError, match="trading lock"):
        asyncio.run(eng.start(E.EngineConfig(symbols=["frxEURUSD"], mode="demo")))


def test_restart_settles_contracts_that_closed_while_offline_and_abandons_paper():
    _open_trade("t-demo", mode="demo", risk_amount=2.0, contract_id="777")
    _open_trade("t-paper", mode="paper", contract_id=None)
    _open_trade("t-nocontract", mode="demo", contract_id=None)

    class Broker:
        async def request(self, payload):
            assert payload == {"proposal_open_contract": 1, "contract_id": 777}
            return {"proposal_open_contract": {"is_sold": 1, "status": "lost", "profit": -2.0, "exit_tick": "1.098"}}

    eng = E.LiveEngine()
    eng.cfg = E.EngineConfig(symbols=["frxEURUSD"], mode="demo", risk_amount=2.0)
    eng.broker = Broker()
    eng.states = {"frxEURUSD": E.SymbolState("frxEURUSD")}
    summary = asyncio.run(eng._reconcile_open_trades())
    assert summary == {"abandoned_paper": 1, "settled": 1, "resumed": 0, "unknown": 1}
    with db.connect() as conn:
        rows = {r["id"]: r for r in db.rows(conn, "SELECT id, status, pnl, r_multiple FROM trades")}
    assert rows["t-demo"]["status"] == "closed" and rows["t-demo"]["pnl"] == -2.0 and rows["t-demo"]["r_multiple"] == -1
    assert rows["t-paper"]["status"] == "abandoned" and rows["t-nocontract"]["status"] == "unknown"
    assert risk.today_stats()["open"] == 0   # nothing stale left blocking max_concurrent


def test_tradingview_alert_never_triggers_evaluation(client, monkeypatch):
    monkeypatch.setenv("COMMUNITY_TV_WEBHOOK_SECRET", "s3cret")

    async def boom(*_):
        raise AssertionError("alerts must not drive the engine")
    monkeypatch.setattr(E.LiveEngine, "_evaluate", boom, raising=False)
    r = client.post("/api/webhooks/tradingview", json={"secret": "s3cret", "ticker": "EURUSD", "id": "x1"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
