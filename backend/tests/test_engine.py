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
        asyncio.run(eng._demo_order("frxEURUSD", pos))


def test_tradingview_webhook_requires_secret_and_dedupes(client, monkeypatch):
    monkeypatch.setenv("COMMUNITY_TV_WEBHOOK_SECRET", "s3cret")
    body = {"secret": "wrong", "ticker": "FX:EURUSD", "id": "a1"}
    assert client.post("/api/webhooks/tradingview", json=body).status_code == 401
    body["secret"] = "s3cret"
    first = client.post("/api/webhooks/tradingview", json=body).json()
    assert first["status"] == "accepted" and first["symbol"] == "frxEURUSD" and first["engine_rechecked"] is False
    assert client.post("/api/webhooks/tradingview", json=body).json()["status"] == "duplicate"


def test_background_job_through_api_runs_to_completion(client):
    """Regression: sync endpoints run in a worker thread; starting a job there used to 500."""
    import random
    import time as _t
    from app.services.deriv import history
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
