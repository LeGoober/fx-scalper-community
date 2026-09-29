"""Capital.com adapter against a scripted HTTP transport (no network)."""
import asyncio
import json

import httpx
import pytest

from app.services.brokers.base import BrokerError, OrderRequest
from app.services.brokers.capital import CapitalBroker

MARKET = {"instrument": {"epic": "EURUSD", "currency": "USD", "lotSize": 1},
          "dealingRules": {"minDealSize": {"unit": "AMOUNT", "value": 100},
                           "minSizeIncrement": {"unit": "AMOUNT", "value": 100},
                           "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.01}},
          "snapshot": {"bid": 1.1000, "offer": 1.1001, "marketStatus": "TRADEABLE", "decimalPlacesFactor": 5}}


class Venue:
    """Scripted Capital.com: records every request; tests flip behaviours on and off."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.logins = 0
        self.expire_next = False
        self.timeout_orders = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        self.calls.append((method, path, body))
        if path == "/api/v1/session" and method == "POST":
            assert request.headers["X-CAP-API-KEY"] == "key" and body["identifier"] == "me@x.com"
            self.logins += 1
            return httpx.Response(200, json={}, headers={"CST": f"cst{self.logins}", "X-SECURITY-TOKEN": "sec"})
        assert request.headers.get("CST", "").startswith("cst"), "session headers missing"
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"errorCode": "error.invalid.session.token"})
        if path == "/api/v1/accounts":
            return httpx.Response(200, json={"accounts": [{"accountId": "A1", "preferred": True, "currency": "USD",
                                                           "balance": {"balance": 1000, "available": 1000}}]})
        if path == "/api/v1/positions" and method == "GET":
            return httpx.Response(200, json={"positions": []})
        if path == "/api/v1/markets/EURUSD":
            return httpx.Response(200, json=MARKET)
        if path in {"/api/v1/workingorders", "/api/v1/positions"} and method == "POST":
            if self.timeout_orders:
                raise httpx.ReadTimeout("venue went quiet", request=request)
            return httpx.Response(200, json={"dealReference": "o_ref1"})
        if path == "/api/v1/confirms/o_ref1":
            return httpx.Response(200, json={"dealStatus": "ACCEPTED", "dealId": "D1", "status": "OPEN",
                                             "level": 1.1001})
        return httpx.Response(404, json={"errorCode": "error.not-found"})


def _broker(venue):
    return CapitalBroker("demo", api_key="key", identifier="me@x.com", password="pw",
                         base_url="https://demo.test", transport=httpx.MockTransport(venue))


def _limit(**kw):
    return OrderRequest(**({"client_order_id": "co1", "symbol": "frxEURUSD", "side": "buy", "size": 500.0,
                            "order_type": "limit", "level": 1.0990, "stop_level": 1.0970, "tp_level": 1.1030,
                            "good_till": 1_790_000_000} | kw))


def test_connect_logs_in_and_verifies_a_demo_account():
    venue = Venue()
    b = _broker(venue)
    info = asyncio.run(b.connect())
    assert venue.logins == 1 and b.account_kind == "demo"
    assert info["account_id"] == "A1" and info["currency"] == "USD" and info["host"] == "demo.test"


def test_dealing_rules_become_an_instrument_spec():
    b = _broker(Venue())
    spec = asyncio.run(b.instrument("frxEURUSD"))
    assert spec.broker_symbol == "EURUSD" and spec.min_size == 100 and spec.size_step == 100
    assert spec.min_stop_distance == pytest.approx(1.1001 * 0.0001)  # 0.01% of price
    assert spec.tradeable and spec.currency == "USD"


def test_limit_order_body_and_confirmation():
    venue = Venue()
    ack = asyncio.run(_broker(venue).place(_limit()))
    assert ack.status == "working" and ack.deal_id == "D1"
    body = next(b for m, p, b in venue.calls if p == "/api/v1/workingorders")
    assert body == {"epic": "EURUSD", "direction": "BUY", "size": 500.0, "guaranteedStop": False,
                    "stopLevel": 1.097, "profitLevel": 1.103, "level": 1.099, "type": "LIMIT",
                    "goodTillDate": "2026-09-21T14:13:20"}


def test_expired_session_logs_in_again_once():
    venue = Venue()
    b = _broker(venue)
    asyncio.run(b.connect())
    venue.expire_next = True
    asyncio.run(b.positions())
    assert venue.logins == 2   # 401 → one re-login → request repeated (the 401 means it was never processed)


def test_an_order_that_times_out_is_unknown_and_never_resent():
    venue = Venue()
    b = _broker(venue)
    asyncio.run(b.instrument("frxEURUSD"))
    venue.timeout_orders = True
    ack = asyncio.run(b.place(_limit()))
    assert ack.status == "unknown"
    assert sum(1 for m, p, _ in venue.calls if p == "/api/v1/workingorders" and m == "POST") == 1


def test_live_host_needs_the_real_money_lock():
    with pytest.raises(BrokerError, match="real-money lock"):
        CapitalBroker("real", api_key="key", identifier="me@x.com", password="pw")


def test_missing_keys_refuse_to_connect():
    with pytest.raises(BrokerError, match="API key"):
        asyncio.run(CapitalBroker("demo", api_key="", identifier="", password="").connect())
