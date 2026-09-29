"""Phase 5: paper engine lifecycle, demo sizing, webhook safety."""
import asyncio

import pytest

from app import db
from app.services import engine as E
from app.services import risk
from app.services.ict import features as F
from app.services.ict import strategy as S


def _engine(mode="paper"):
    eng = E.LiveEngine()
    eng.cfg = E.EngineConfig(symbols=["frxEURUSD"], mode=mode, risk_amount=2.0)
    eng.schema = S.get("ict_2022_model")
    eng.runtime = S.Runtime(eng.schema, "code")
    eng.states = {"frxEURUSD": E.SymbolState("frxEURUSD")}
    return eng


def _passing_eval(entry=1.1000, stop=1.0980, target=1.1050):
    setup = F.Setup("long", 0, 1.0985, "pdl", 1.0982, 2, 1.0995, F.FVG(3, "bullish", 1.1002, 1.0998), 1.1010,
                    1.0982, 1.8, 2, 4)
    plan = S.Plan("long", entry, stop, target, abs(entry - stop), abs(target - entry) / abs(entry - stop), "pdh")
    return S.Evaluation(setup, plan, [S.NodeResult("killzone", "code", True, "ny_am")])


def test_paper_lifecycle_signal_fill_target(monkeypatch):
    eng = _engine()
    st = eng.states["frxEURUSD"]
    monkeypatch.setattr(eng, "_cost", lambda symbol: 0.0002)
    t0 = 1_790_000_000
    bars = F.Bars([t0 + i * 300 for i in range(5)], [1.1] * 5, [1.101] * 5, [1.099] * 5, [1.1] * 5, 300)
    eng._record_signal(st, _passing_eval(), bars)
    assert len(st.pending) == 1

    async def ticks():
        await eng._on_tick(st, 1.1012, t0 + 1600)   # above entry: still pending
        assert st.position is None and st.pending
        await eng._on_tick(st, 1.0999, t0 + 1700)   # through the FVG entry: filled
        assert st.position and st.position.entry == 1.0999 and not st.pending
        await eng._on_tick(st, 1.1051, t0 + 2000)   # target
        assert st.position is None
    asyncio.run(ticks())
    with db.connect() as conn:
        trade = db.row(conn, "SELECT * FROM trades")
        sig = db.row(conn, "SELECT * FROM signals")
    assert trade["mode"] == "paper" and trade["status"] == "closed" and sig["status"] == "accepted"
    expected_r = (1.1050 - 1.0999) / (1.0999 - 1.0980) - 0.0002 / (1.0999 - 1.0980)
    assert trade["r_multiple"] == pytest.approx(expected_r, abs=1e-3)
    assert trade["pnl"] == pytest.approx(round(expected_r * 2.0, 2))


def test_pending_cancelled_when_target_trades_first():
    eng = _engine()
    st = eng.states["frxEURUSD"]
    t0 = 1_790_000_000
    bars = F.Bars([t0 + i * 300 for i in range(5)], [1.1] * 5, [1.101] * 5, [1.099] * 5, [1.1] * 5, 300)
    eng._record_signal(st, _passing_eval(), bars)
    asyncio.run(eng._on_tick(st, 1.1055, t0 + 1600))
    assert not st.pending and st.position is None


def test_rejected_signal_is_logged_but_not_traded():
    eng = _engine()
    st = eng.states["frxEURUSD"]
    ev = _passing_eval()
    ev.results.append(S.NodeResult("htf_draw", "code", False, "sell_side_below"))
    bars = F.Bars([1_790_000_000 + i * 300 for i in range(5)], [1.1] * 5, [1.1] * 5, [1.1] * 5, [1.1] * 5, 300)
    eng._record_signal(st, ev, bars)
    assert not st.pending
    with db.connect() as conn:
        assert db.row(conn, "SELECT status FROM signals")["status"] == "rejected"


def test_multiplier_sizing_respects_accepted_multipliers_and_stop_out(monkeypatch):
    eng = _engine("demo")
    monkeypatch.setattr(E.profile, "get", lambda symbol=None: {"multipliers": [50, 100, 150, 250, 500]})
    m, stake = eng._choose_multiplier("frxEURUSD", entry=1.1000, stop=1.0980)  # 18 pip stop ≈ 0.18%
    frac = 0.0020 / 1.1
    assert m == 250 and frac < 0.8 / m            # 500 would stop out before our stop
    assert stake == pytest.approx(2.0 / (m * frac), rel=0.01)


def test_demo_order_blocked_on_real_account(monkeypatch):
    eng = _engine("demo")

    class FakeBroker:
        connected, is_virtual = True, False

        async def proposal(self, **fields):
            return {"id": "p1", "ask_price": fields["amount"], "spot": 1.1}

        async def buy(self, *a):
            raise AssertionError("must never buy on a real account")
    eng.broker = FakeBroker()
    monkeypatch.setattr(E.profile, "get", lambda symbol=None: {"multipliers": [100]})
    pos = E.Position("t1", "frxEURUSD", "long", 1.1, 1.098, 1.105, 2.0, 0, None, mode="demo")
    with pytest.raises(risk.RiskBlocked):
        asyncio.run(eng._broker_order("frxEURUSD", pos))


def test_tradingview_webhook_requires_secret_and_dedupes(client, monkeypatch):
    monkeypatch.setenv("COMMUNITY_TV_WEBHOOK_SECRET", "s3cret")
    body = {"secret": "wrong", "ticker": "FX:EURUSD", "id": "a1"}
    assert client.post("/api/webhooks/tradingview", json=body).status_code == 401
    body["secret"] = "s3cret"
    first = client.post("/api/webhooks/tradingview", json=body).json()
    assert first["status"] == "accepted" and first["symbol"] == "frxEURUSD" and first["engine_watching"] is False
    assert client.post("/api/webhooks/tradingview", json=body).json()["status"] == "duplicate"


def test_background_job_through_api_runs_to_completion(client, monkeypatch):
    """Regression: sync endpoints run in a worker thread; starting a job there used to 500."""
    import random
    import time as _t
    from app.services.deriv import history, profile

    async def no_network_backfill(*a, **k):  # keep the test offline; history is seeded below
        return {"fetched": 0}

    async def no_network_calibrate(*a, **k):
        return {}
    monkeypatch.setattr(history, "backfill", no_network_backfill)
    monkeypatch.setattr(profile, "calibrate", no_network_calibrate)
    rng, price, candles = random.Random(3), 100.0, []
    for i in range(3000):
        o = price
        c = o + rng.gauss(0, 0.35) + (1.4 if rng.random() < 0.02 else 0) * rng.choice((-1, 1))
        candles.append({"epoch": 1_773_000_000 + i * 60, "open": o, "high": max(o, c) + 0.1, "low": min(o, c) - 0.1,
                        "close": c})
        price = c
    history.upsert("TEST", 60, candles)
    r = client.post("/api/backtests/run", json={"symbol": "TEST", "start": 1_773_000_000, "end": 1_773_180_000})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "failed"}:
            break
        _t.sleep(0.1)
    assert job["status"] == "done", job
    run = client.get(f"/api/backtests/{job['result']['id']}").json()
    assert run["status"] == "done" and "code" in run["results"]


def test_metrics_overview_shape(client):
    data = client.get("/api/metrics/overview").json()
    for key in ("performance", "execution", "strategy", "data", "risk", "engine"):
        assert key in data


def test_deriv_ticket_for_manual_orders(monkeypatch):
    """Paper signals carry what to type into Deriv Trader: stake, multiplier, TP/SL amounts."""
    from app.services import notify
    eng = _engine()
    eng.cfg.risk_amount = 1.0
    monkeypatch.setattr(E.profile, "get", lambda symbol=None: {"multipliers": [50, 100, 150, 250, 500]})
    t = eng.deriv_ticket("frxEURUSD", "long", entry=1.1000, stop=1.0980, target=1.1050)
    assert t["direction"] == "Up" and t["stop_loss"] == 1.0 and t["take_profit"] == 2.5
    assert t["multiplier"] == 250 and t["stake"] >= 1.0   # 500 would stop out before the chart stop
    assert t["effective_risk"] == pytest.approx(1.0, abs=0.05) and "note" not in t
    # 1R too small for Deriv's $1 minimum stake: smallest multiplier, and the overshoot is spelled out
    eng.cfg.risk_amount = 0.02
    monkeypatch.setattr(E.profile, "get", lambda symbol=None: {"multipliers": [5, 10, 20]})
    small = eng.deriv_ticket("frxEURUSD", "short", entry=1.1000, stop=1.1100, target=1.0800)
    assert small["direction"] == "Down" and small["stake"] == 1.0 and small["multiplier"] == 5
    assert small["effective_risk"] > 0.02 and "minimum stake" in small["note"]
    eng.cfg.risk_amount = 1.0
    text = notify.ticket_lines(t)
    assert "Stake" in text and "Take profit 2.50 USD" in text and "Stop loss 1.00 USD" in text


def test_accepted_signal_stores_ticket():
    eng = _engine()
    st = eng.states["frxEURUSD"]
    bars = F.Bars([1_790_000_000 + i * 300 for i in range(5)], [1.1] * 5, [1.101] * 5, [1.099] * 5, [1.1] * 5, 300)
    eng._record_signal(st, _passing_eval(), bars)
    with db.connect() as conn:
        decision = db.loads(db.row(conn, "SELECT decision_json FROM signals")["decision_json"], {})
    assert decision["ticket"]["take_profit"] == 5.0 and decision["ticket"]["direction"] == "Up"


def test_real_mode_refuses_to_start_unless_enabled_and_armed(monkeypatch):
    eng = E.LiveEngine()
    cfg = E.EngineConfig(symbols=["frxEURUSD"], mode="real", risk_amount=1.0)
    with pytest.raises(risk.RiskBlocked, match="COMMUNITY_ALLOW_REAL_TRADING"):
        asyncio.run(eng.start(cfg))
    monkeypatch.setenv("COMMUNITY_ALLOW_REAL_TRADING", "true")
    with pytest.raises(risk.RiskBlocked, match="armed"):   # .env alone is not enough
        asyncio.run(eng.start(cfg))
    risk.arm_real("I ACCEPT REAL MONEY RISK")
    with pytest.raises(risk.RiskBlocked, match="max_risk_per_trade"):
        asyncio.run(eng.start(cfg.model_copy(update={"risk_amount": 50.0})))
    assert not eng.running


def test_orders_refuse_account_of_the_wrong_kind(monkeypatch):
    """A real-mode engine never trades a demo account, and a demo-mode engine never trades a real one."""
    monkeypatch.setattr(E.profile, "get", lambda symbol=None: {"multipliers": [100]})

    class Broker:
        connected = True

        def __init__(self, virtual):
            self.is_virtual = virtual

        async def proposal(self, **fields):
            return {"id": "p1", "ask_price": fields["amount"], "spot": 1.1}

        async def buy(self, *a):
            raise AssertionError("must not buy")
    pos = E.Position("t1", "frxEURUSD", "long", 1.1, 1.098, 1.105, 2.0, 0, None)
    real = _engine("demo")
    real.cfg = real.cfg.model_copy(update={"mode": "real"})
    real.broker = Broker(True)
    with pytest.raises(risk.RiskBlocked, match="not verified as real"):
        asyncio.run(real._broker_order("frxEURUSD", pos))
    demo = _engine("demo")
    demo.broker = Broker(False)
    with pytest.raises(risk.RiskBlocked, match="not verified as demo"):
        asyncio.run(demo._broker_order("frxEURUSD", pos))


def test_disarm_stops_a_real_engine(client, monkeypatch):
    stopped = {}

    async def fake_stop(reason="stopped"):
        stopped["reason"] = reason
        return {}
    monkeypatch.setattr(E, "stop", fake_stop)
    monkeypatch.setattr(E.ENGINE, "cfg", E.EngineConfig(mode="real"))
    monkeypatch.setattr(type(E.ENGINE), "running", property(lambda self: True))
    assert client.post("/api/risk/real/disarm").status_code == 200
    assert stopped["reason"] == "real trading disarmed"
