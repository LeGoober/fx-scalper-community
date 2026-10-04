"""cTrader Open API adapter (Spotware), JSON over secure WebSocket. Default venue: Deriv cTrader.

Why cTrader: Deriv's TradingView trading panel connects to a Deriv **cTrader** account, and the
cTrader Open API reaches every account of any cTrader broker. So an order placed here from Python
lands on the same account TradingView shows: the trade appears on your chart. Available in South
Africa through your existing Deriv login. (Deriv's own public API covers only the Options account.)

Protocol (Spotware's published openapi-proto-messages; JSON encoding of the same messages):
  envelope  {"clientMsgId": str, "payloadType": int, "payload": {...}}
  auth      ApplicationAuthReq(2100) → GetAccountListByAccessTokenReq(2149) → AccountAuthReq(2102)
  heartbeat payloadType 51 every 10 s
  volume    in hundredths of a unit (1000 units = 100000); spot prices in 1/100000 of a price unit
  SL/TP     absolute prices on LIMIT orders; MARKET orders only accept relative distances
Details the docs leave open are handled defensively: enums are sent as numbers and read as numbers
or names; replies are matched by clientMsgId, and order events also by our clientOrderId/label.

Safety rules (same as every adapter):
  * account kind comes from the account's own isLive flag and must match the host asked for;
  * the live host only when the 3-gate real-money lock is effective;
  * an order with no definite answer is "unknown" and never resent; every order carries our
    client order id as its label, so reconciliation matches positions exactly.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from typing import Any, Awaitable, Callable

from app import config
from app.services.brokers.base import (AccountKind, BrokerError, BrokerPosition, InstrumentSpec, OrderAck,
                                       OrderRequest, Quote, WorkingOrder)

PT = {"heartbeat": 51, "app_auth": 2100, "app_auth_res": 2101, "acc_auth": 2102, "acc_auth_res": 2103,
      "new_order": 2106, "cancel_order": 2108, "close_position": 2111, "asset_list": 2112, "asset_list_res": 2113,
      "symbols_list": 2114, "symbols_list_res": 2115, "symbol_by_id": 2116, "symbol_by_id_res": 2117,
      "trader": 2121, "trader_res": 2122, "reconcile": 2124, "reconcile_res": 2125, "execution": 2126,
      "sub_spots": 2127, "sub_spots_res": 2128, "spot": 2131, "order_error": 2132, "error": 2142,
      "token_invalidated": 2147, "client_disconnect": 2148, "accounts_by_token": 2149,
      "accounts_by_token_res": 2150, "refresh_token": 2173, "refresh_token_res": 2174,
      "deals_by_position": 2179, "deals_by_position_res": 2180}
EXEC = {"ORDER_ACCEPTED": 2, "ORDER_FILLED": 3, "ORDER_REPLACED": 4, "ORDER_CANCELLED": 5, "ORDER_EXPIRED": 6,
        "ORDER_REJECTED": 7, "ORDER_CANCEL_REJECTED": 8, "ORDER_PARTIAL_FILL": 11}
SIDE = {"BUY": 1, "SELL": 2}
ORDER_TYPE = {"MARKET": 1, "LIMIT": 2, "STOP": 3}
ORDER_STATUS = {"ORDER_STATUS_ACCEPTED": 1, "ORDER_STATUS_FILLED": 2}
POSITION_STATUS = {"POSITION_STATUS_OPEN": 1}
GOOD_TILL_DATE = 1
SYMBOL_NAMES = {"frxEURUSD": ["EURUSD"], "frxGBPUSD": ["GBPUSD"], "frxUSDJPY": ["USDJPY"],
                "frxXAUUSD": ["XAUUSD", "GOLD"], "frxAUDUSD": ["AUDUSD"], "frxUSDCAD": ["USDCAD"],
                "frxEURGBP": ["EURGBP"], "OTC_NDX": ["US100", "NAS100", "USTEC", "USTECH100"],
                "OTC_SPC": ["US500", "SPX500", "USSP500"], "OTC_DJI": ["US30", "DJ30", "WS30"]}
PRICE_SCALE = 100_000
REQUEST_TIMEOUT_S = 15.0
FILL_WAIT_S = 10.0

WsFactory = Callable[[str], Awaitable[Any]]


def _norm(name: str) -> str:
    return "".join(ch for ch in name.upper() if ch.isalnum())


def _enum(value: Any, mapping: dict[str, int]) -> int | None:
    """Enums may arrive as numbers or as names."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return mapping.get(value, int(value) if value.isdigit() else None)
    return None


class CTraderBroker:
    name = "ctrader"

    def __init__(self, kind: AccountKind = "demo", *, client_id: str | None = None, client_secret: str | None = None,
                 access_token: str | None = None, refresh_token: str | None = None, account_id: str | None = None,
                 url: str | None = None, ws_factory: WsFactory | None = None) -> None:
        if kind == "real":
            from app.services import risk
            if not risk.real_trading_status()["effective"]:
                raise BrokerError("A live cTrader account needs the real-money lock (Settings → Risk).")
        self.kind: AccountKind = kind
        self.url = url or config.ctrader_url(kind)
        self.client_id = client_id if client_id is not None else config.ctrader_client_id()
        self.client_secret = client_secret if client_secret is not None else config.ctrader_client_secret()
        self.access_token = access_token if access_token is not None else config.ctrader_access_token()
        self.refresh_token = refresh_token if refresh_token is not None else config.ctrader_refresh_token()
        self.account_hint = account_id if account_id is not None else config.ctrader_account_id()
        self._ws_factory = ws_factory
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._order_waiters: dict[str, asyncio.Queue] = {}
        self._tasks: list[asyncio.Task] = []
        self._send_lock = asyncio.Lock()
        self.account_id: int | None = None
        self.account: dict = {}
        self.account_kind: AccountKind | None = None
        self.currency: str | None = None
        self.balance: float | None = None
        self._assets: dict[int, str] = {}
        self._symbol_ids: dict[str, int] = {}
        self._symbol_names: dict[int, str] = {}
        self._specs: dict[str, InstrumentSpec] = {}
        self._quotes: dict[int, tuple[float, float, int]] = {}
        self._subscribed: set[int] = set()

    # ------------------------------------------------------------ connection
    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.access_token)

    async def connect(self) -> dict:
        if not self.configured:
            raise BrokerError("cTrader needs an Open API app (client id + secret) and an access token "
                              "(Settings → API keys → cTrader, or the 'Connect cTrader' button).")
        await self._open()
        await self._req("app_auth", {"clientId": self.client_id, "clientSecret": self.client_secret})
        accounts = await self._accounts()
        account = self._pick_account(accounts)
        self.account_id = int(account["ctidTraderAccountId"])
        await self._req("acc_auth", {"ctidTraderAccountId": self.account_id, "accessToken": self.access_token})
        self.account_kind = self.kind  # verified in _pick_account: the account's isLive matches the host
        self.account = account
        trader = (await self._req("trader", {"ctidTraderAccountId": self.account_id})).get("trader") or {}
        assets = (await self._req("asset_list", {"ctidTraderAccountId": self.account_id})).get("asset") or []
        self._assets = {int(a["assetId"]): str(a.get("name") or a.get("displayName") or "") for a in assets}
        digits = int(trader.get("moneyDigits") or 2)
        self.currency = self._assets.get(int(trader.get("depositAssetId") or 0))
        self.balance = float(trader.get("balance") or 0) / 10 ** digits
        symbols = (await self._req("symbols_list", {"ctidTraderAccountId": self.account_id})).get("symbol") or []
        by_name = {_norm(str(s.get("symbolName") or "")): int(s["symbolId"]) for s in symbols if s.get("enabled", True)}
        for ours, names in SYMBOL_NAMES.items():
            sid = next((by_name[_norm(n)] for n in names if _norm(n) in by_name), None)
            if sid is not None:
                self._symbol_ids[ours], self._symbol_names[sid] = sid, ours
        return self.info()

    async def _open(self) -> None:
        await self.close()
        factory = self._ws_factory
        if factory is None:
            import websockets

            def factory(url: str):
                return websockets.connect(url, open_timeout=15, ping_interval=None, max_size=2 ** 24)
        try:
            self._ws = await factory(self.url)
        except Exception as exc:
            raise BrokerError(f"cTrader connection failed: {exc}", retryable=True) from exc
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._heartbeat())]

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(BrokerError("connection closed"))
        self._pending.clear()

    async def _accounts(self) -> list[dict]:
        try:
            res = await self._req("accounts_by_token", {"accessToken": self.access_token})
        except BrokerError as exc:
            if exc.code != "CH_ACCESS_TOKEN_INVALID" or not self.refresh_token:
                raise
            await self._refresh()
            res = await self._req("accounts_by_token", {"accessToken": self.access_token})
        return res.get("ctidTraderAccount") or []

    def _pick_account(self, accounts: list[dict]) -> dict:
        want_live = self.kind == "real"
        if self.account_hint:
            hint = str(self.account_hint)
            match = [a for a in accounts if hint in (str(a.get("ctidTraderAccountId")), str(a.get("traderLogin")))]
            if not match:
                raise BrokerError(f"cTrader account {hint} is not linked to this access token.")
            account = match[0]
        else:
            same_kind = [a for a in accounts if bool(a.get("isLive")) == want_live]
            if not same_kind:
                raise BrokerError(f"No {'live' if want_live else 'demo'} cTrader account on this token.")
            account = same_kind[0]
        if bool(account.get("isLive")) != want_live:  # fail closed: never trade a live account as demo
            actual = "live" if account.get("isLive") else "demo"
            raise BrokerError(f"cTrader account {account.get('traderLogin')} is {actual}, not {self.kind}.")
        return account

    async def _refresh(self) -> None:
        res = await self._req("refresh_token", {"refreshToken": self.refresh_token})
        self.access_token, self.refresh_token = res["accessToken"], res["refreshToken"]
        from app import secrets_store
        secrets_store.set_secret("ctrader_access_token", self.access_token)
        secrets_store.set_secret("ctrader_refresh_token", self.refresh_token)

    async def _send(self, payload_type: int, payload: dict, msg_id: str | None = None) -> None:
        if self._ws is None:
            raise BrokerError("cTrader is not connected", retryable=True)
        frame: dict = {"payloadType": payload_type, "payload": payload}
        if msg_id:
            frame["clientMsgId"] = msg_id
        async with self._send_lock:
            await self._ws.send(json.dumps(frame))

    async def _req(self, kind: str, payload: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
        msg_id = f"fxs-{next(self._ids)}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._send(PT[kind], payload, msg_id)
            pt, body = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:  # no answer: for an order this means "may or may not have executed"
            raise BrokerError(f"cTrader did not answer {kind} within {timeout:.0f}s", retryable=True) from exc
        finally:
            self._pending.pop(msg_id, None)
        if pt in (PT["error"], PT["order_error"]):
            raise BrokerError(f"{body.get('errorCode')}: {body.get('description') or ''}".strip(),
                              code=str(body.get("errorCode")))
        return body

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                await self._send(PT["heartbeat"], {})
            except Exception:
                return

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                msg = json.loads(raw)
                pt, body, mid = int(msg.get("payloadType", 0)), msg.get("payload") or {}, msg.get("clientMsgId")
                if pt == PT["spot"]:
                    old = self._quotes.get(int(body["symbolId"]), (0, 0, 0))
                    bid = body.get("bid", old[0] * PRICE_SCALE)
                    ask = body.get("ask", old[1] * PRICE_SCALE)
                    self._quotes[int(body["symbolId"])] = (float(bid) / PRICE_SCALE, float(ask) / PRICE_SCALE,
                                                           int(time.time()))
                    continue
                if pt in (PT["execution"], PT["order_error"]):
                    coid = ((body.get("order") or {}).get("clientOrderId")
                            or ((body.get("order") or {}).get("tradeData") or {}).get("label")
                            or ((body.get("position") or {}).get("tradeData") or {}).get("label"))
                    if coid and coid in self._order_waiters:
                        self._order_waiters[coid].put_nowait((pt, body))
                if mid and mid in self._pending and not self._pending[mid].done():
                    self._pending[mid].set_result((pt, body))
                elif pt in (PT["token_invalidated"], PT["client_disconnect"]):
                    raise BrokerError(f"cTrader session ended ({pt})")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BrokerError(f"cTrader connection lost: {exc}", retryable=True))
            self._ws = None

    # ------------------------------------------------------------ market data
    def _sid(self, symbol: str) -> int:
        sid = self._symbol_ids.get(symbol)
        if sid is None:
            raise BrokerError(f"{symbol} is not offered on this cTrader account.")
        return sid

    async def instrument(self, symbol: str) -> InstrumentSpec:
        if symbol in self._specs:
            return self._specs[symbol]
        sid = self._sid(symbol)
        res = await self._req("symbol_by_id", {"ctidTraderAccountId": self.account_id, "symbolId": [sid]})
        s = (res.get("symbol") or [{}])[0]
        digits = int(s.get("digits") or 5)
        q = await self.quote(symbol)
        sl = float(s.get("slDistance") or 0)
        distance_units = {"SYMBOL_DISTANCE_IN_POINTS": 1, "SYMBOL_DISTANCE_IN_PERCENTAGE": 2}
        in_pct = _enum(s.get("distanceSetIn"), distance_units) == 2
        min_stop = q.mid * sl / 100 if in_pct else sl / 10 ** digits
        light = next((n for n, i in self._symbol_ids.items() if i == sid), symbol)
        quote_ccy = self._quote_currency(light)
        if not s.get("minVolume"):
            raise BrokerError(f"cTrader returned no minimum volume for {symbol}; refusing to size.")
        spec = InstrumentSpec(
            symbol=symbol, broker_symbol=str(sid), currency=quote_ccy, min_size=float(s["minVolume"]) / 100,
            size_step=float(s.get("stepVolume") or s["minVolume"]) / 100, value_per_point=1.0,
            min_stop_distance=min_stop, decimals=digits,
            tradeable=_enum(s.get("tradingMode", 0), {"ENABLED": 0}) == 0, raw=s)
        self._specs[symbol] = spec
        return spec

    def _quote_currency(self, symbol: str) -> str:
        """Quote currency of our symbol (P&L currency). Indices on Deriv/most cTrader brokers quote in USD."""
        if symbol.startswith("frx") and len(symbol) == 9:
            return symbol[-3:]
        return "USD"

    async def quote(self, symbol: str) -> Quote:
        sid = self._sid(symbol)
        if sid not in self._subscribed:
            await self._req("sub_spots", {"ctidTraderAccountId": self.account_id, "symbolId": [sid]})
            self._subscribed.add(sid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            bid, ask, ts = self._quotes.get(sid, (0.0, 0.0, 0))
            if bid > 0 and ask > 0:
                return Quote(bid, ask, ts)
            await asyncio.sleep(0.05)
        raise BrokerError(f"No cTrader price for {symbol} (market closed?).")

    # ------------------------------------------------------------ orders
    async def place(self, req: OrderRequest) -> OrderAck:
        spec = await self.instrument(req.symbol)
        sid, rnd = self._sid(req.symbol), spec.decimals
        payload: dict = {"ctidTraderAccountId": self.account_id, "symbolId": sid,
                         "tradeSide": SIDE["BUY"] if req.side == "buy" else SIDE["SELL"],
                         "volume": int(round(req.size * 100)), "label": req.client_order_id[:50],
                         "clientOrderId": req.client_order_id[:50], "comment": "fx-scalper"}
        if req.order_type == "limit":
            if req.level is None:
                raise BrokerError("A limit order needs a level.")
            payload |= {"orderType": ORDER_TYPE["LIMIT"], "limitPrice": round(req.level, rnd),
                        "stopLoss": round(req.stop_level, rnd)}
            if req.tp_level is not None:
                payload["takeProfit"] = round(req.tp_level, rnd)
            if req.good_till:
                payload |= {"timeInForce": GOOD_TILL_DATE, "expirationTimestamp": int(req.good_till) * 1000}
        else:
            q = await self.quote(req.symbol)
            ref = q.ask if req.side == "buy" else q.bid
            payload |= {"orderType": ORDER_TYPE["MARKET"],
                        "relativeStopLoss": int(round(abs(ref - req.stop_level) * PRICE_SCALE))}
            if req.tp_level is not None:
                payload["relativeTakeProfit"] = int(round(abs(req.tp_level - ref) * PRICE_SCALE))
        waiter: asyncio.Queue = asyncio.Queue()
        self._order_waiters[req.client_order_id[:50]] = waiter
        try:
            try:
                body = await self._req("new_order", payload)
            except BrokerError as exc:
                if exc.code and exc.code != "None":   # the server answered with an error: definitely not placed
                    return OrderAck(req.client_order_id, "rejected", reason=str(exc), raw={"request": payload})
                # no answer / connection lost after sending: it may have executed; never resend
                return OrderAck(req.client_order_id, "unknown", reason=str(exc), raw={"request": payload})
            return await self._order_outcome(req, body, waiter, payload)
        finally:
            self._order_waiters.pop(req.client_order_id[:50], None)

    async def _order_outcome(self, req: OrderRequest, body: dict, waiter: asyncio.Queue, payload: dict) -> OrderAck:
        raw = {"request": payload, "events": [body]}
        deadline = time.monotonic() + FILL_WAIT_S
        while True:
            exec_type = _enum(body.get("executionType"), EXEC)
            order, position, deal = body.get("order") or {}, body.get("position") or {}, body.get("deal") or {}
            if exec_type == EXEC["ORDER_REJECTED"] or body.get("errorCode"):
                return OrderAck(req.client_order_id, "rejected", reason=str(body.get("errorCode") or "rejected"),
                                raw=raw)
            if req.order_type == "limit" and exec_type == EXEC["ORDER_ACCEPTED"]:
                return OrderAck(req.client_order_id, "working", deal_id=str(order.get("orderId")), raw=raw)
            if exec_type in (EXEC["ORDER_FILLED"], EXEC["ORDER_PARTIAL_FILL"]):
                price = deal.get("executionPrice") or position.get("price") or order.get("executionPrice")
                position_id = position.get("positionId") or deal.get("positionId")
                return OrderAck(req.client_order_id, "filled", deal_id=str(position_id),
                                fill_price=float(price) if price else None, raw=raw)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OrderAck(req.client_order_id, "unknown", reason="accepted but no fill seen yet", raw=raw)
            try:
                _, body = await asyncio.wait_for(waiter.get(), remaining)
                raw["events"].append(body)
            except asyncio.TimeoutError:
                return OrderAck(req.client_order_id, "unknown", reason="accepted but no fill seen yet", raw=raw)

    async def cancel_working(self, deal_id: str) -> None:
        await self._req("cancel_order", {"ctidTraderAccountId": self.account_id, "orderId": int(deal_id)})

    async def close_position(self, deal_id: str) -> OrderAck:
        pos = next((p for p in await self.positions() if p.deal_id == deal_id), None)
        if pos is None:
            return OrderAck(deal_id, "unknown", deal_id=deal_id, reason="position not found (already closed?)")
        try:
            body = await self._req("close_position", {"ctidTraderAccountId": self.account_id,
                                                      "positionId": int(deal_id), "volume": int(round(pos.size * 100))})
        except BrokerError as exc:
            if exc.code and exc.code != "None":
                raise
            return OrderAck(deal_id, "unknown", deal_id=deal_id, reason=str(exc))
        price = (body.get("deal") or {}).get("executionPrice")
        filled = _enum(body.get("executionType"), EXEC) == EXEC["ORDER_FILLED"]
        return OrderAck(deal_id, "filled" if filled else "unknown", deal_id=deal_id,
                        fill_price=float(price) if price else None, raw={"event": body})

    async def _reconcile(self) -> dict:
        return await self._req("reconcile", {"ctidTraderAccountId": self.account_id})

    async def positions(self) -> list[BrokerPosition]:
        out = []
        for p in (await self._reconcile()).get("position") or []:
            if _enum(p.get("positionStatus", 1), POSITION_STATUS) != 1:
                continue
            td = p.get("tradeData") or {}
            out.append(BrokerPosition(
                deal_id=str(p.get("positionId")), symbol=self._symbol_names.get(int(td.get("symbolId", 0)), "?"),
                side="buy" if _enum(td.get("tradeSide"), SIDE) == 1 else "sell",
                size=float(td.get("volume") or 0) / 100, open_price=float(p.get("price") or 0),
                stop_level=p.get("stopLoss"), tp_level=p.get("takeProfit"), opened_at=str(td.get("openTimestamp")),
                client_order_id=td.get("label")))
        return out

    async def working_orders(self) -> list[WorkingOrder]:
        out = []
        for o in (await self._reconcile()).get("order") or []:
            if _enum(o.get("orderType"), ORDER_TYPE) not in (2, 3) or \
                    _enum(o.get("orderStatus", 1), ORDER_STATUS) != 1:
                continue
            td = o.get("tradeData") or {}
            out.append(WorkingOrder(
                deal_id=str(o.get("orderId")), symbol=self._symbol_names.get(int(td.get("symbolId", 0)), "?"),
                side="buy" if _enum(td.get("tradeSide"), SIDE) == 1 else "sell",
                size=float(td.get("volume") or 0) / 100, level=float(o.get("limitPrice") or o.get("stopPrice") or 0),
                stop_level=o.get("stopLoss"), tp_level=o.get("takeProfit"),
                client_order_id=o.get("clientOrderId") or td.get("label")))
        return out

    async def activity(self, deal_id: str | None = None, last_seconds: int = 3600) -> list[dict]:
        """Closing deals of one position: the real exit price and P&L, from the broker."""
        if not deal_id:
            return []
        now_ms = int(time.time() * 1000)
        res = await self._req("deals_by_position", {"ctidTraderAccountId": self.account_id, "positionId": int(deal_id),
                                                    "fromTimestamp": now_ms - last_seconds * 1000,
                                                    "toTimestamp": now_ms})
        out = []
        for d in res.get("deal") or []:
            close = d.get("closePositionDetail")
            if not close:
                continue
            digits = int(close.get("moneyDigits") or d.get("moneyDigits") or 2)
            out.append({"price": d.get("executionPrice"), "gross": float(close.get("grossProfit") or 0) / 10 ** digits,
                        "commission": float(close.get("commission") or 0) / 10 ** digits,
                        "swap": float(close.get("swap") or 0) / 10 ** digits, "source": None})
        return out

    def info(self) -> dict:
        return {"broker": self.name, "account_kind": self.account_kind, "host": self.url.split("//")[-1],
                "account_id": str(self.account.get("traderLogin") or self.account_id or "") or None,
                "currency": self.currency, "balance": self.balance, "available": None,
                "broker_title": self.account.get("brokerTitleShort"), "connected": self._ws is not None}
