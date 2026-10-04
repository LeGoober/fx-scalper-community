"""cTrader Open API adapter against a scripted JSON-over-WebSocket server (no network)."""
import asyncio
import json
import time

import pytest

from app import config, db
from app.services import decision, execution, risk
from app.services.brokers import ctrader as C
from app.services.brokers.base import BrokerError, OrderRequest

ACCOUNTS = [{"ctidTraderAccountId": 111, "isLive": False, "traderLogin": 5001, "brokerTitleShort": "Deriv"},
            {"ctidTraderAccountId": 222, "isLive": True, "traderLogin": 9001, "brokerTitleShort": "Deriv"}]
STATIC = {
    2100: (2101, {}),
    2173: (2174, {"accessToken": "good", "refreshToken": "r2", "tokenType": "bearer", "expiresIn": 2628000}),
    2121: (2122, {"trader": {"ctidTraderAccountId": 111, "balance": 1000000, "moneyDigits": 2, "depositAssetId": 1}}),
    2112: (2113, {"asset": [{"assetId": 1, "name": "USD"}, {"assetId": 2, "name": "EUR"}]}),
    2114: (2115, {"symbol": [{"symbolId": 1, "symbolName": "EURUSD", "enabled": True},
                             {"symbolId": 41, "symbolName": "XAUUSD", "enabled": True}]}),
    2116: (2117, {"symbol": [{"symbolId": 1, "digits": 5, "pipPosition": 4, "minVolume": 100000,
                              "stepVolume": 100000, "slDistance": 10, "distanceSetIn": 1}]}),
}


class Venue:
    """Scripted cTrader server: one handler per payloadType; tests flip behaviours."""

    def __init__(self):
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue | None = None
        self.silent_orders = False
        self.reject_orders = False
        self.positions: list[dict] = []
        self.orders: list[dict] = []
        self.handlers = {2149: self.accounts, 2102: self.account_auth, 2127: self.spots, 2106: self.new_order,
                         2124: self.reconcile, 2108: self.cancel, 2111: self.close_position, 2179: self.deals}

    async def factory(self, url):
        self.inbox = asyncio.Queue()
        return self

    def _put(self, payload_type, body, mid=None):
        frame = {"payloadType": payload_type, "payload": body} | ({"clientMsgId": mid} if mid else {})
        self.inbox.put_nowait(json.dumps(frame))

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        pt, mid = msg["payloadType"], msg.get("clientMsgId")
        if pt in STATIC:
            self._put(*STATIC[pt], mid)
        elif pt in self.handlers:
            self.handlers[pt](msg["payload"], mid)

    def accounts(self, p, mid):
        if p["accessToken"] != "good":
            self._put(2142, {"errorCode": "CH_ACCESS_TOKEN_INVALID", "description": "expired"}, mid)
        else:
            self._put(2150, {"accessToken": "good", "ctidTraderAccount": ACCOUNTS}, mid)

    def account_auth(self, p, mid):
        self._put(2103, {"ctidTraderAccountId": p["ctidTraderAccountId"]}, mid)

    def spots(self, p, mid):
        self._put(2128, {}, mid)
        self._put(2131, {"ctidTraderAccountId": 111, "symbolId": 1, "bid": 110000, "ask": 110010})

    def new_order(self, p, mid):
        if self.silent_orders:
            return
        if self.reject_orders:
            self._put(2132, {"ctidTraderAccountId": 111, "errorCode": "NOT_ENOUGH_MONEY"}, mid)
            return
        order = {"orderId": 77, "clientOrderId": p["clientOrderId"],
                 "tradeData": {"symbolId": 1, "volume": p["volume"], "tradeSide": p["tradeSide"], "label": p["label"]}}
        self._put(2126, {"executionType": 2, "order": order}, mid)
        if p["orderType"] == 1:  # market: the fill arrives as a separate, unsolicited event
            self._put(2126, {"executionType": 3, "order": order, "position": {"positionId": 88, "price": 1.1001},
                             "deal": {"executionPrice": 1.1001, "positionId": 88}})

    def reconcile(self, p, mid):
        self._put(2125, {"position": self.positions, "order": self.orders}, mid)

    def cancel(self, p, mid):
        self._put(2126, {"executionType": 5, "order": {"orderId": p["orderId"]}}, mid)

    def close_position(self, p, mid):
        self._put(2126, {"executionType": 3, "deal": {"executionPrice": 1.0995, "positionId": p["positionId"]}}, mid)

    def deals(self, p, mid):
        detail = {"grossProfit": -100, "moneyDigits": 2, "commission": -3, "swap": 0, "entryPrice": 1.099, "balance": 1}
        self._put(2180, {"deal": [{"executionPrice": 1.0970, "positionId": p["positionId"],
                                   "closePositionDetail": detail}], "hasMore": False}, mid)

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        pass


def broker(venue, **kw):
    args = {"client_id": "cid", "client_secret": "sec", "access_token": "good", "refresh_token": "r1",
            "account_id": "", "url": "wss://demo.test:5036", "ws_factory": venue.factory} | kw
    return C.CTraderBroker("demo", **args)


def with_broker(venue, body, **kw):
    """Connect, run `body(b)`, close; all inside one event loop."""
    async def go():
        b = broker(venue, **kw)
        await b.connect()
        try:
            return b, await body(b)
        finally:
            await b.close()
    return asyncio.run(go())


async def nothing(b):
    return None


def _req(**kw):
    return OrderRequest(**({"client_order_id": "co-abc", "symbol": "frxEURUSD", "side": "buy", "size": 2000.0,
                            "order_type": "limit", "level": 1.0990, "stop_level": 1.0970, "tp_level": 1.1030,
                            "good_till": 1_790_000_000} | kw))


def _sent_order(venue):
    return next(m["payload"] for m in venue.sent if m["payloadType"] == 2106)


def test_connect_authorises_app_then_the_demo_account():
    venue = Venue()
    b, _ = with_broker(venue, nothing)
    assert [m["payloadType"] for m in venue.sent[:3]] == [2100, 2149, 2102]
    assert venue.sent[0]["payload"] == {"clientId": "cid", "clientSecret": "sec"}
    assert b.account_id == 111 and b.account_kind == "demo"
    info = b.info()
    assert info["currency"] == "USD" and info["balance"] == 10000.0 and info["account_id"] == "5001"
    assert b._symbol_ids == {"frxEURUSD": 1, "frxXAUUSD": 41}


def test_a_live_account_is_never_traded_on_the_demo_host():
    with pytest.raises(BrokerError, match="is live, not demo"):
        with_broker(Venue(), nothing, account_id="9001")


def test_live_host_needs_the_real_money_lock():
    with pytest.raises(BrokerError, match="real-money lock"):
        C.CTraderBroker("real", client_id="c", client_secret="s", access_token="t")


def test_expired_access_token_is_refreshed_and_stored():
    b, _ = with_broker(Venue(), nothing, access_token="old")
    assert b.access_token == "good" and b.refresh_token == "r2"
    assert config.ctrader_access_token() == "good" and config.ctrader_refresh_token() == "r2"  # persisted


def test_symbol_rules_become_an_instrument_spec():
    async def body(b):
        return await b.instrument("frxEURUSD"), await b.quote("frxEURUSD")
    _, (spec, q) = with_broker(Venue(), body)
    assert spec.min_size == 1000 and spec.size_step == 1000 and spec.currency == "USD"
    assert spec.min_stop_distance == pytest.approx(0.0001) and spec.tradeable
    assert (q.bid, q.ask) == (1.1, 1.1001)


def test_limit_order_with_absolute_stops_and_expiry_rests_at_the_broker():
    venue = Venue()
    _, ack = with_broker(venue, lambda b: b.place(_req()))
    sent = _sent_order(venue)
    assert ack.status == "working" and ack.deal_id == "77"
    assert sent["orderType"] == 2 and sent["volume"] == 200000 and sent["limitPrice"] == 1.099
    assert sent["stopLoss"] == 1.097 and sent["takeProfit"] == 1.103
    assert sent["timeInForce"] == 1 and sent["expirationTimestamp"] == 1_790_000_000_000
    assert sent["label"] == sent["clientOrderId"] == "co-abc"


def test_market_order_uses_relative_stops_and_waits_for_the_fill():
    venue = Venue()
    _, ack = with_broker(venue, lambda b: b.place(_req(order_type="market", level=None)))
    sent = _sent_order(venue)
    assert "stopLoss" not in sent and sent["relativeStopLoss"] == 310 and sent["relativeTakeProfit"] == 290
    assert ack.status == "filled" and ack.deal_id == "88" and ack.fill_price == 1.1001


def test_a_server_error_is_a_definite_rejection():
    venue = Venue()
    venue.reject_orders = True
    _, ack = with_broker(venue, lambda b: b.place(_req()))
    assert ack.status == "rejected" and "NOT_ENOUGH_MONEY" in ack.reason


def test_an_unanswered_order_is_unknown_and_sent_once():
    venue = Venue()

    async def body(b):
        venue.silent_orders = True
        original = b._req

        async def short(kind, payload, timeout=C.REQUEST_TIMEOUT_S):
            return await original(kind, payload, timeout=0.3 if kind == "new_order" else timeout)
        b._req = short
        return await b.place(_req())
    _, ack = with_broker(venue, body)
    assert ack.status == "unknown"
    assert sum(1 for m in venue.sent if m["payloadType"] == 2106) == 1


def test_positions_and_orders_carry_our_client_order_id():
    venue = Venue()
    venue.positions = [{"positionId": 88, "positionStatus": 1, "price": 1.0991, "stopLoss": 1.097, "takeProfit": 1.103,
                        "tradeData": {"symbolId": 1, "volume": 200000, "tradeSide": 1, "label": "co-abc"}}]
    venue.orders = [{"orderId": 77, "orderType": 2, "orderStatus": 1, "limitPrice": 1.099, "clientOrderId": "co-xyz",
                     "tradeData": {"symbolId": 1, "volume": 200000, "tradeSide": "SELL"}}]

    async def body(b):
        return await b.positions(), await b.working_orders(), await b.activity("88")
    _, (pos, work, acts) = with_broker(venue, body)
    assert pos[0].deal_id == "88" and pos[0].symbol == "frxEURUSD" and pos[0].size == 2000
    assert pos[0].client_order_id == "co-abc" and pos[0].side == "buy"
    assert work[0].side == "sell" and work[0].client_order_id == "co-xyz"   # enum given as a name also works
    assert acts == [{"price": 1.097, "gross": -1.0, "commission": -0.03, "swap": 0.0, "source": None}]


def test_full_pipeline_on_ctrader_matches_the_fill_by_label(monkeypatch):
    venue = Venue()
    monkeypatch.setattr(execution, "SERVICE", execution.ExecutionService(factory=lambda name, kind: broker(venue)))
    risk.set_limits({"max_daily_loss": 50.0})
    risk.set_autonomy("on")

    async def go():
        def proposal(key):
            return decision.Proposal(source="ict_engine", symbol="frxEURUSD", direction="long", stop=1.0970,
                                     target=1.1030, entry=1.0990, entry_type="limit",
                                     expires_at=int(time.time()) + 3600, setup_key=key)
        # This account's minimum is 1000 units: at a 20-pip stop that risks $2, so a $1 budget is refused
        # (never silently risk more), and $2 fits exactly.
        refused = await decision.submit(proposal("ct1"), broker="ctrader", account_kind="demo", risk_amount=1.0)
        out = await decision.submit(proposal("ct2"), broker="ctrader", account_kind="demo", risk_amount=2.0)
        sent = _sent_order(venue)
        # The broker fills the limit: the position carries our label; a decoy of the same shape doesn't.
        trade = {"symbolId": 1, "volume": sent["volume"], "tradeSide": 1}
        venue.positions = [{"positionId": 5, "positionStatus": 1, "price": 1.0990, "tradeData": trade | {"label": "x"}},
                           {"positionId": 88, "positionStatus": 1, "price": 1.0990,
                            "tradeData": trade | {"label": sent["label"]}}]
        summary = await execution.SERVICE.reconcile()
        await execution.SERVICE.close()
        return refused, out, summary
    refused, out, summary = asyncio.run(go())
    risk.set_autonomy("off")
    assert refused["verdict"] == "blocked" and "Minimum size" in refused["reasons"][0]
    assert out["verdict"] == "executed" and summary["filled"] == 1
    with db.connect() as conn:
        t = db.row(conn, "SELECT status, deal_id FROM trades WHERE id = ?", (out["trade_id"],))
    assert t == {"status": "open", "deal_id": "88"}   # matched by label, not the decoy
