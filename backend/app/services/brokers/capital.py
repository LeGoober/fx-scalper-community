"""Capital.com REST adapter (open-api.capital.com, /api/v1).

Why Capital.com: it has a public retail API *and* is a TradingView trading-panel broker, so an order
placed here from Python appears on your TradingView chart once you connect the same account in
TradingView. TradingView itself has no order API.

Session: POST /session with the X-CAP-API-KEY header returns CST and X-SECURITY-TOKEN headers. The
session expires after 10 minutes idle, so an idle client pings before reuse and logs in again on 401.
Rate limits (documented): 10 requests/s overall, 1 order request per 0.1 s, 1 POST /session per s,
1,000 order requests per hour on demo.

Order safety:
  * A 401 or a refused connection means the venue never processed the request, so it is safe to
    log in again and resend once.
  * A timeout or dropped connection AFTER sending an order is ambiguous (it may have executed):
    the order is reported as status "unknown" and never resent; the execution service reconciles.
  * Live (real-money) host only when the 3-gate real-money lock in `risk` is effective.

Field names follow the published docs. Dealing-rule units the docs don't spell out are parsed
defensively; when a value can't be read, sizing refuses to trade instead of guessing.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from app import config
from app.services.brokers.base import (AccountKind, BrokerError, BrokerPosition, InstrumentSpec, OrderAck,
                                       OrderRequest, Quote, WorkingOrder)

# Our (Deriv-style) symbols → Capital.com epics. Verified against GET /markets/{epic} before use.
EPIC_MAP = {"frxEURUSD": "EURUSD", "frxGBPUSD": "GBPUSD", "frxUSDJPY": "USDJPY", "frxXAUUSD": "GOLD",
            "frxAUDUSD": "AUDUSD", "frxUSDCAD": "USDCAD", "frxEURGBP": "EURGBP",
            "OTC_NDX": "US100", "OTC_SPC": "US500", "OTC_DJI": "US30"}
SESSION_IDLE_S = 540          # ping before reuse once idle this long (the venue drops sessions at 600 s)
CONFIRM_ATTEMPTS = 6
SPEC_TTL_S = 3600


class AmbiguousOrder(BrokerError):
    """The order request may or may not have executed; never resend it."""


class _Limiter:
    """Minimum spacing between calls (a 1-slot token bucket), shared by concurrent callers."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.min_interval


def epic_for(symbol: str) -> str:
    return EPIC_MAP.get(symbol, symbol)


def symbol_for(epic: str) -> str:
    return next((s for s, e in EPIC_MAP.items() if e == epic), epic)


def _value(rule: object) -> tuple[float | None, str | None]:
    """Dealing rules come as {"unit": ..., "value": ...} (or a bare number)."""
    if isinstance(rule, dict):
        v = rule.get("value")
        return (float(v) if v is not None else None), rule.get("unit")
    if isinstance(rule, (int, float)):
        return float(rule), None
    return None, None


class CapitalBroker:
    name = "capital"

    def __init__(self, kind: AccountKind = "demo", *, api_key: str | None = None, identifier: str | None = None,
                 password: str | None = None, base_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0) -> None:
        if kind == "real":
            from app.services import risk
            if not risk.real_trading_status()["effective"]:
                raise BrokerError("The live Capital.com host needs the real-money lock (Settings → Risk).")
        self.kind: AccountKind = kind
        self.base_url = (base_url or config.capital_url(kind)).rstrip("/")
        self.api_key = api_key if api_key is not None else config.capital_api_key()
        self.identifier = identifier if identifier is not None else config.capital_identifier()
        self.password = password if password is not None else config.capital_api_password()
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)
        self._cst: str | None = None
        self._security: str | None = None
        self._last_used = 0.0
        self._login_lock = asyncio.Lock()
        self._any = _Limiter(0.11)      # ≤ ~9 requests/s overall
        self._orders = _Limiter(0.12)   # ≤ 1 order request per 0.1 s
        self._session_calls = _Limiter(1.05)
        self._specs: dict[str, tuple[float, InstrumentSpec]] = {}
        self.account: dict = {}
        self.account_kind: AccountKind | None = None

    # ------------------------------------------------------------ session
    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.identifier and self.password)

    async def connect(self) -> dict:
        if not self.configured:
            raise BrokerError("Capital.com needs an API key, login identifier and API password (Settings → API keys).")
        await self._login()
        accounts = (await self._request("GET", "/api/v1/accounts")).get("accounts") or []
        self.account = next((a for a in accounts if a.get("preferred")), accounts[0] if accounts else {})
        # The host decides the account kind: the demo host only ever holds demo money.
        self.account_kind = self.kind
        return self.info()

    async def _login(self) -> None:
        async with self._login_lock:
            await self._session_calls.wait()
            try:
                r = await self._http.post("/api/v1/session", headers={"X-CAP-API-KEY": self.api_key},
                                          json={"identifier": self.identifier, "password": self.password,
                                                "encryptedPassword": False})
            except httpx.HTTPError as exc:
                raise BrokerError(f"Capital.com login failed: {type(exc).__name__}", retryable=True) from exc
            if r.status_code >= 400:
                raise BrokerError(f"Capital.com login refused: {self._error(r)}", code=str(r.status_code))
            self._cst, self._security = r.headers.get("CST"), r.headers.get("X-SECURITY-TOKEN")
            if not (self._cst and self._security):
                raise BrokerError("Capital.com login returned no session tokens.")
            self._last_used = time.monotonic()

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _error(r: httpx.Response) -> str:
        try:
            body = r.json()
        except ValueError:
            return r.text[:200] or str(r.status_code)
        return str(body.get("errorCode") or body.get("message") or body)[:200]

    async def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                       order: bool = False) -> dict:
        if self._cst is None:
            await self._login()
        elif time.monotonic() - self._last_used > SESSION_IDLE_S:
            await self._ping_or_login()
        for attempt in (1, 2):
            await self._any.wait()
            if order:
                await self._orders.wait()
            try:
                r = await self._http.request(method, path, json=json, params=params,
                                             headers={"CST": self._cst or "", "X-SECURITY-TOKEN": self._security or ""})
            except httpx.ConnectError as exc:  # never reached the venue: safe to report as retryable
                raise BrokerError(f"Capital.com unreachable: {exc}", retryable=True) from exc
            except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if order:
                    raise AmbiguousOrder(f"No answer to {method} {path}: the order may have executed") from exc
                raise BrokerError(f"Capital.com timed out on {method} {path}", retryable=True) from exc
            self._last_used = time.monotonic()
            if r.status_code == 401 and attempt == 1:  # session expired: the request was not processed
                await self._login()
                continue
            if r.status_code == 429:
                raise BrokerError("Capital.com rate limit hit", code="429", retryable=True)
            if r.status_code >= 400:
                raise BrokerError(f"{method} {path}: {self._error(r)}", code=str(r.status_code))
            return r.json() if r.content else {}
        raise BrokerError("Capital.com session could not be renewed")

    async def _ping_or_login(self) -> None:
        try:
            await self._any.wait()
            r = await self._http.get("/api/v1/ping", headers={"CST": self._cst or "",
                                                              "X-SECURITY-TOKEN": self._security or ""})
            if r.status_code == 200:
                self._last_used = time.monotonic()
                return
        except httpx.HTTPError:
            pass
        await self._login()

    # ------------------------------------------------------------ market data
    async def _market(self, symbol: str) -> dict:
        return await self._request("GET", f"/api/v1/markets/{epic_for(symbol)}")

    async def instrument(self, symbol: str) -> InstrumentSpec:
        cached = self._specs.get(symbol)
        if cached and time.monotonic() - cached[0] < SPEC_TTL_S:
            return cached[1]
        m = await self._market(symbol)
        inst, rules, snap = m.get("instrument") or {}, m.get("dealingRules") or {}, m.get("snapshot") or {}
        min_size, _ = _value(rules.get("minDealSize"))
        step, _ = _value(rules.get("minSizeIncrement"))
        stop_v, stop_unit = _value(rules.get("minStopOrProfitDistance"))
        price = float(snap.get("offer") or snap.get("bid") or 0) or None
        if stop_v is None:
            min_stop = 0.0
        elif (stop_unit or "").upper() == "PERCENTAGE":
            min_stop = (price or 0) * stop_v / 100
        else:
            min_stop = stop_v
        if not min_size:
            raise BrokerError(f"Capital.com returned no minimum deal size for {epic_for(symbol)}; refusing to size.")
        spec = InstrumentSpec(
            symbol=symbol, broker_symbol=epic_for(symbol), currency=str(inst.get("currency") or ""),
            min_size=min_size, size_step=step or min_size,
            value_per_point=float(inst.get("lotSize") or 1.0), min_stop_distance=min_stop,
            decimals=int(snap.get("decimalPlacesFactor") or 5),
            tradeable=str(snap.get("marketStatus") or "").upper() == "TRADEABLE", raw=m)
        self._specs[symbol] = (time.monotonic(), spec)
        return spec

    async def quote(self, symbol: str) -> Quote:
        snap = (await self._market(symbol)).get("snapshot") or {}
        bid, ask = snap.get("bid"), snap.get("offer")
        if bid is None or ask is None:
            raise BrokerError(f"No Capital.com price for {epic_for(symbol)} (market {snap.get('marketStatus')}).")
        return Quote(float(bid), float(ask), int(time.time()))

    # ------------------------------------------------------------ orders
    async def place(self, req: OrderRequest) -> OrderAck:
        spec = await self.instrument(req.symbol)
        rnd = spec.decimals
        body: dict = {"epic": spec.broker_symbol, "direction": "BUY" if req.side == "buy" else "SELL",
                      "size": req.size, "guaranteedStop": False, "stopLevel": round(req.stop_level, rnd)}
        if req.tp_level is not None:
            body["profitLevel"] = round(req.tp_level, rnd)
        if req.order_type == "limit":
            if req.level is None:
                raise BrokerError("A limit order needs a level.")
            body |= {"level": round(req.level, rnd), "type": "LIMIT"}
            if req.good_till:
                body["goodTillDate"] = datetime.fromtimestamp(req.good_till, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            path = "/api/v1/workingorders"
        else:
            path = "/api/v1/positions"
        try:
            ref = (await self._request("POST", path, json=body, order=True)).get("dealReference")
        except AmbiguousOrder as exc:
            return OrderAck(req.client_order_id, "unknown", reason=str(exc), raw={"request": body})
        except BrokerError as exc:
            return OrderAck(req.client_order_id, "rejected", reason=str(exc), raw={"request": body})
        if not ref:
            return OrderAck(req.client_order_id, "unknown", reason="no dealReference returned", raw={"request": body})
        confirm = await self._confirm(ref)
        if confirm is None:
            return OrderAck(req.client_order_id, "unknown", deal_reference=ref, reason="no confirmation yet",
                            raw={"request": body})
        if str(confirm.get("dealStatus")).upper() != "ACCEPTED":
            return OrderAck(req.client_order_id, "rejected", deal_reference=ref,
                            reason=str(confirm.get("reason") or confirm.get("rejectReason") or confirm.get("status")),
                            raw={"request": body, "confirm": confirm})
        deal_id = confirm.get("dealId") or next((d.get("dealId") for d in confirm.get("affectedDeals") or []), None)
        status = "working" if req.order_type == "limit" else "filled"
        return OrderAck(req.client_order_id, status, deal_id=deal_id, deal_reference=ref,
                        fill_price=float(confirm["level"]) if status == "filled" and confirm.get("level") else None,
                        raw={"request": body, "confirm": confirm})

    async def _confirm(self, ref: str) -> dict | None:
        for attempt in range(CONFIRM_ATTEMPTS):
            try:
                return await self._request("GET", f"/api/v1/confirms/{ref}")
            except BrokerError as exc:
                if exc.code not in {"404", "400"} and not exc.retryable:
                    raise
            await asyncio.sleep(0.25 * (attempt + 1))
        return None

    async def cancel_working(self, deal_id: str) -> None:
        await self._request("DELETE", f"/api/v1/workingorders/{deal_id}", order=True)

    async def close_position(self, deal_id: str) -> OrderAck:
        try:
            ref = (await self._request("DELETE", f"/api/v1/positions/{deal_id}", order=True)).get("dealReference")
        except AmbiguousOrder as exc:
            return OrderAck(deal_id, "unknown", deal_id=deal_id, reason=str(exc))
        confirm = await self._confirm(ref) if ref else None
        ok = confirm is not None and str(confirm.get("dealStatus")).upper() == "ACCEPTED"
        return OrderAck(deal_id, "filled" if ok else "unknown", deal_id=deal_id, deal_reference=ref,
                        fill_price=float(confirm["level"]) if ok and confirm.get("level") else None,
                        raw={"confirm": confirm})

    async def positions(self) -> list[BrokerPosition]:
        out = []
        for item in (await self._request("GET", "/api/v1/positions")).get("positions") or []:
            p, m = item.get("position") or {}, item.get("market") or {}
            out.append(BrokerPosition(
                deal_id=str(p.get("dealId")), symbol=symbol_for(str(m.get("epic"))),
                side="buy" if str(p.get("direction")).upper() == "BUY" else "sell", size=float(p.get("size") or 0),
                open_price=float(p.get("level") or 0), stop_level=p.get("stopLevel"), tp_level=p.get("profitLevel"),
                opened_at=p.get("createdDateUTC"), upl=p.get("upl")))
        return out

    async def working_orders(self) -> list[WorkingOrder]:
        out = []
        for item in (await self._request("GET", "/api/v1/workingorders")).get("workingOrders") or []:
            w, m = item.get("workingOrderData") or {}, item.get("marketData") or {}
            out.append(WorkingOrder(
                deal_id=str(w.get("dealId")), symbol=symbol_for(str(w.get("epic") or m.get("epic"))),
                side="buy" if str(w.get("direction")).upper() == "BUY" else "sell",
                size=float(w.get("orderSize") or 0), level=float(w.get("orderLevel") or 0),
                stop_level=w.get("stopLevel"), tp_level=w.get("profitLevel"), good_till=w.get("goodTillDateUTC")))
        return out

    async def activity(self, deal_id: str | None = None, last_seconds: int = 3600) -> list[dict]:
        params: dict = {"lastPeriod": min(max(last_seconds, 60), 86400), "detailed": "true"}
        if deal_id:
            params["dealId"] = deal_id
        return (await self._request("GET", "/api/v1/history/activity", params=params)).get("activities") or []

    def info(self) -> dict:
        balance = self.account.get("balance") or {}
        return {"broker": self.name, "account_kind": self.account_kind, "host": self.base_url.split("//")[-1],
                "account_id": self.account.get("accountId"), "currency": self.account.get("currency"),
                "balance": balance.get("balance"), "available": balance.get("available"),
                "connected": self._cst is not None}
