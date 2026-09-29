"""Decision pipeline + execution service + desk API with a scripted broker (no network)."""
import asyncio
import time

import pytest

from app import db
from app.services import decision, execution, risk
from app.services.brokers.base import BrokerPosition, InstrumentSpec, OrderAck, Quote, WorkingOrder


class FakeBroker:
    name = "capital"

    def __init__(self, kind="demo", acks=None, bid=1.1000, ask=1.1001):
        self.kind, self.account_kind = kind, None
        self.acks = list(acks or [])
        self.placed, self.cancelled, self.closed = [], [], []
        self.pos: list[BrokerPosition] = []
        self.work: list[WorkingOrder] = []
        self.acts: list[dict] = []
        self.q = Quote(bid, ask, int(time.time()))

    async def connect(self):
        self.account_kind = self.kind
        return self.info()

    async def close(self):
        pass

    async def instrument(self, symbol):
        return InstrumentSpec(symbol, "EURUSD", "USD", min_size=100, size_step=100, value_per_point=1.0,
                              min_stop_distance=0.0001)

    async def quote(self, symbol):
        return self.q

    async def place(self, req):
        self.placed.append(req)
        default = OrderAck(req.client_order_id, "working", deal_id=f"W{len(self.placed)}")
        ack = self.acks.pop(0) if self.acks else default
        ack.client_order_id = req.client_order_id
        return ack

    async def cancel_working(self, deal_id):
        self.cancelled.append(deal_id)

    async def close_position(self, deal_id):
        self.closed.append(deal_id)
        return OrderAck(deal_id, "filled", deal_id=deal_id, fill_price=1.1010)

    async def positions(self):
        return list(self.pos)

    async def working_orders(self):
        return list(self.work)

    async def activity(self, deal_id=None, last_seconds=3600):
        return list(self.acts)

    def info(self):
        return {"broker": "capital", "account_kind": self.account_kind, "currency": "USD", "account_id": "A1"}


@pytest.fixture
def venue(monkeypatch):
    broker = FakeBroker()
    service = execution.ExecutionService(factory=lambda name, kind: broker)
    monkeypatch.setattr(execution, "SERVICE", service)
    risk.set_limits({"max_daily_loss": 50.0, "max_risk_per_trade": 10.0, "max_stake": 100.0})
    yield broker
    risk.set_autonomy("off")


def _proposal(key="k1", entry=1.0990, stop=1.0970, target=1.1030, **kw):
    return decision.Proposal(source="ict_engine", symbol="frxEURUSD", direction="long", stop=stop, target=target,
                             entry=entry, entry_type="limit", expires_at=int(time.time()) + 3600, setup_key=key, **kw)


def _submit(p, **kw):
    return asyncio.run(decision.submit(p, broker="capital", account_kind=kw.pop("kind", "demo"),
                                       risk_amount=kw.pop("risk", 1.0), **kw))


def _age(trade_id, seconds=120):
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
    with db.connect() as conn:
        conn.execute("UPDATE trades SET opened_at=? WHERE id=?", (old, trade_id))


def _trade(trade_id):
    with db.connect() as conn:
        return db.row(conn, "SELECT * FROM trades WHERE id=?", (trade_id,))


def test_master_switch_off_means_no_automatic_order(venue):
    out = _submit(_proposal())
    assert out["verdict"] == "skipped" and "master switch OFF" in out["reasons"]
    assert venue.placed == []
    d = decision.dossier(out["decision_id"])
    assert d["decision"]["verdict"] == "skipped" and d["proposal"]["setup_key"] == "k1"


def test_master_on_places_one_sized_limit_order_with_broker_stops(venue):
    risk.set_autonomy("on")
    out = _submit(_proposal(), risk=1.0)
    assert out["verdict"] == "executed"
    req = venue.placed[0]
    assert req.order_type == "limit" and req.stop_level == 1.0970 and req.tp_level == 1.1030
    assert req.size == 500  # 1.0 / 0.0020 = 500, a multiple of the 100 step
    t = _trade(out["trade_id"])
    assert t["status"] == "working" and t["deal_id"] == "W1" and t["auto"] == 1
    assert t["risk_amount"] == pytest.approx(1.0 + 500 * 0.0001)  # the spread is money at risk too
    with db.connect() as conn:
        assert db.row(conn, "SELECT state FROM order_intents")["state"] == "working"
    assert _submit(_proposal())["status"] == "duplicate"  # same setup key → never ordered twice
    assert len(venue.placed) == 1


def test_real_account_is_blocked_without_the_3_gates_even_with_master_on(monkeypatch):
    broker = FakeBroker(kind="real")
    monkeypatch.setattr(execution, "SERVICE", execution.ExecutionService(factory=lambda n, k: broker))
    risk.set_autonomy("on")
    out = _submit(_proposal(), kind="real")
    assert out["verdict"] == "blocked" and "REAL" in out["reasons"][0] and broker.placed == []
    risk.set_autonomy("off")


def test_price_gap_between_feeds_blocks_the_trade(venue):
    risk.set_autonomy("on")
    out = _submit(_proposal(), reference_price=1.0990)  # venue mid 1.10005 vs 1.0990: 0.53R gap
    assert out["verdict"] == "blocked" and "Price gap" in out["reasons"][0] and venue.placed == []


def test_ambiguous_order_is_linked_by_reconcile_not_resent(venue):
    risk.set_autonomy("on")
    venue.acks = [OrderAck("x", "unknown", reason="timeout")]
    out = _submit(_proposal())
    assert out["verdict"] == "unknown" and _trade(out["trade_id"])["status"] == "unknown"
    _age(out["trade_id"])
    venue.work = [WorkingOrder("W9", "frxEURUSD", "buy", 500, 1.0990)]
    summary = asyncio.run(execution.SERVICE.reconcile())
    assert summary["linked"] == 1 and _trade(out["trade_id"])["deal_id"] == "W9"
    assert len(venue.placed) == 1


def test_an_order_the_broker_never_saw_fails_after_two_checks(venue):
    risk.set_autonomy("on")
    venue.acks = [OrderAck("x", "unknown", reason="timeout")]
    out = _submit(_proposal())
    _age(out["trade_id"])
    asyncio.run(execution.SERVICE.reconcile())
    assert _trade(out["trade_id"])["status"] == "unknown"
    asyncio.run(execution.SERVICE.reconcile())
    assert _trade(out["trade_id"])["status"] == "failed"
    assert risk.today_stats()["open"] == 0


def test_fill_then_stop_out_is_recorded_from_the_broker(venue):
    risk.set_autonomy("on")
    out = _submit(_proposal())
    venue.pos = [BrokerPosition("P1", "frxEURUSD", "buy", 500, 1.0990, 1.0970, 1.1030)]
    asyncio.run(execution.SERVICE.reconcile())            # working order gone, position appeared → open
    t = _trade(out["trade_id"])
    assert t["status"] == "open" and t["deal_id"] == "P1"
    venue.pos, venue.acts = [], [{"source": "SL", "dealId": "P1"}]
    asyncio.run(execution.SERVICE.reconcile())            # position gone, activity says stop-loss
    t = _trade(out["trade_id"])
    assert t["status"] == "closed" and t["exit_price"] == 1.0970 and t["r_multiple"] == pytest.approx(-1)
    assert t["pnl"] == pytest.approx(-1.0)


def test_master_off_via_api_cancels_and_closes_automated_trades(client, venue):
    risk.set_autonomy("on")
    working = _submit(_proposal("k-w"))["trade_id"]
    venue.acks = [OrderAck("x", "filled", deal_id="P2", fill_price=1.1001)]
    filled = _submit(_proposal("k-m", entry=None))["trade_id"]
    r = client.put("/api/autonomy", json={"master": "off"})
    assert r.status_code == 200 and r.json()["master"] == "off"
    assert venue.cancelled == ["W1"] and venue.closed == ["P2"]
    assert _trade(working)["status"] == "cancelled" and _trade(filled)["status"] == "closed"


def test_per_trade_off_button(client, venue):
    risk.set_autonomy("on")
    trade_id = _submit(_proposal())["trade_id"]
    r = client.post(f"/api/desk/trades/{trade_id}/close")
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and venue.cancelled == ["W1"]
    assert client.post("/api/desk/trades/nope/close").status_code == 404


def test_master_switch_cannot_turn_on_under_the_kill_switch(client):
    risk.set_kill_switch(True)
    assert client.put("/api/autonomy", json={"master": "on"}).status_code == 409
    risk.set_kill_switch(False)


def test_decisions_api_lists_and_explains(client, venue):
    risk.set_autonomy("on")
    out = _submit(_proposal(evidence={"signal": {"nodes": [{"id": "killzone", "passed": True}]}}))
    listed = client.get("/api/desk/decisions").json()["decisions"]
    assert listed[0]["id"] == out["decision_id"] and listed[0]["verdict"] == "executed"
    d = client.get(f"/api/desk/decisions/{out['decision_id']}").json()
    assert d["proposal"]["evidence"]["signal"]["nodes"][0]["id"] == "killzone"
    assert d["intents"][0]["request"]["size"] == 500 and d["trade"]["status"] == "working"


def test_engine_signal_becomes_a_capital_decision_with_its_evidence(venue):
    from app.services import engine as E
    from test_engine import _passing_eval
    from app.services.ict import features as F
    risk.set_autonomy("on")
    eng = E.LiveEngine()
    eng.cfg = E.EngineConfig(symbols=["frxEURUSD"], mode="demo", broker="capital", risk_amount=1.0)
    eng.schema = E.S.get("ict_2022_model")
    eng.states = {"frxEURUSD": E.SymbolState("frxEURUSD", last_price=1.10005)}
    now = int(time.time())
    bars = F.Bars([now - (4 - i) * 300 for i in range(5)], [1.1] * 5, [1.101] * 5, [1.099] * 5, [1.1] * 5, 300)

    async def run():
        eng._record_signal(eng.states["frxEURUSD"], _passing_eval(), bars)
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name().startswith("decide:")])
    asyncio.run(run())
    assert eng.states["frxEURUSD"].pending == []           # no local tick-triggered entry for Capital.com
    assert len(venue.placed) == 1 and venue.placed[0].order_type == "limit"
    listed = decision.recent()
    assert listed[0]["source"] == "ict_engine" and listed[0]["verdict"] == "executed"
    d = decision.dossier(listed[0]["id"])
    assert d["proposal"]["evidence"]["signal"]["nodes"][0]["id"] == "killzone"
    assert d["signal"]["status"] == "accepted"
