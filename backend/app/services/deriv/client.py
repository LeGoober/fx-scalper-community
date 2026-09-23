"""Async Deriv WebSocket client.

Replaces the old synchronous `DerivCommunityClient` (one send, one recv).
- `req_id` multiplexing, so many requests can be in flight on one socket;
- subscriptions (ticks, proposal_open_contract) delivered through async iterators;
- app-level ping every 30 s (Deriv drops idle sockets) with round-trip timing;
- both auth flows ported from the original client: PAT tokens (REST account
  lookup, then an OTP WebSocket URL) and legacy tokens (`authorize` over WS).

Public market data (`ticks_history`, `active_symbols`) needs no token:
`await DerivClient(token="").connect()`.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import websockets

from app import config

log = logging.getLogger("fxs.deriv")


class DerivError(RuntimeError):
    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def normalize_token(value: object) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    token = token.replace("​", "").replace("﻿", "")
    return "".join(token.split())


class Subscription:
    """Async iterator over one Deriv subscription stream."""

    def __init__(self, client: "DerivClient", req_id: int, queue: asyncio.Queue) -> None:
        self._client = client
        self.req_id = req_id
        self._queue = queue
        self.id: Optional[str] = None

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> dict:
        message = await self._queue.get()
        if message is None:
            raise StopAsyncIteration
        if "error" in message:
            err = message["error"]
            raise DerivError(err.get("message", "Deriv subscription error"), err.get("code"))
        sub = message.get("subscription") or {}
        if sub.get("id"):
            self.id = sub["id"]
        return message

    async def close(self) -> None:
        self._client._streams.pop(self.req_id, None)
        if self.id and self._client.connected:
            try:
                await self._client.request({"forget": self.id}, timeout=5)
            except DerivError:
                pass


class DerivClient:
    def __init__(
        self,
        *,
        app_id: str | None = None,
        token: str | None = None,
        ws_url: str | None = None,
        rest_url: str | None = None,
        account_mode: str = "demo",
        account_id: str = "",
    ) -> None:
        raw_app_id = str(app_id if app_id is not None else config.deriv_app_id()).strip()
        self.app_id = raw_app_id if raw_app_id.isdigit() else config.DEFAULT_DERIV_APP_ID
        self.token = normalize_token(config.deriv_token() if token is None else token)
        self.ws_url = ws_url or config.deriv_ws_url()
        self.rest_url = (rest_url or config.deriv_rest_url()).rstrip("/")
        self.account_mode = account_mode if account_mode in {"demo", "real"} else "demo"
        self.account_id = account_id
        self.account: dict = {}
        self.endpoint_kind: str = "public"  # public | demo | real | legacy (from the connected URL path)
        self.last_rtt_ms: float | None = None
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._streams: dict[int, asyncio.Queue] = {}
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ state
    @property
    def token_mode(self) -> str:
        if not self.token:
            return "none"
        return "pat" if self.token.startswith("pat_") else "legacy"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def is_virtual(self) -> Optional[bool]:
        """True = demo, False = real, None = unknown. Callers must treat None as NOT demo.

        Every available signal must agree. The server-issued OTP URL path (/ws/demo vs
        /ws/real) is authoritative when present; account metadata must not contradict it.
        """
        if not self.account:
            return None
        signals: list[bool] = []
        if self.endpoint_kind in {"demo", "real"}:
            signals.append(self.endpoint_kind == "demo")
        meta = self._account_is_virtual()
        if meta is not None:
            signals.append(meta)
        if not signals or len(set(signals)) > 1:
            return None
        return signals[0]

    def _account_is_virtual(self) -> Optional[bool]:
        if "is_virtual" in self.account:
            return bool(int(self.account["is_virtual"]))
        account_type = str(self.account.get("account_type") or "").lower()
        if account_type in {"demo", "real"}:
            return account_type == "demo"
        loginid = str(self.account.get("loginid") or "")
        if loginid.startswith("VRT"):
            return True
        return None

    def account_info(self) -> dict:
        a = self.account or {}
        return {
            "connected": self.connected,
            "authorized": bool(a),
            "token_mode": self.token_mode,
            "app_id": self.app_id,
            "loginid": a.get("loginid") or a.get("account_id") or a.get("id"),
            "is_virtual": self.is_virtual,
            "endpoint": self.endpoint_kind,
            "account_type": a.get("account_type") or ("demo" if self.is_virtual else "real" if self.is_virtual is False
                                                       else None),
            "currency": a.get("currency"),
            "balance": a.get("balance"),
            "rtt_ms": self.last_rtt_ms,
        }

    # -------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "DerivClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self, *, authorize: bool | None = None) -> dict:
        await self.close()
        authorize = bool(self.token) if authorize is None else authorize
        if authorize and not self.token:
            raise DerivError("Missing Deriv API token.")
        url = self._ws_url_with_app_id()
        self.endpoint_kind = "legacy" if (authorize and self.token_mode == "legacy") else "public"
        if authorize and self.token_mode == "pat":
            account = await self._select_options_account()
            account_id = self._field(account, "account_id", "accountId", "id")
            if not account_id:
                raise DerivError("Unable to resolve a Deriv options account ID.")
            url = await self._otp_ws_url(account_id)
            path = urlparse(url).path.rstrip("/")
            self.endpoint_kind = "demo" if path.endswith("/demo") else "real" if path.endswith("/real") else "unknown"
            self.account = {**account, "account_id": account_id}
        try:
            self._ws = await websockets.connect(url, open_timeout=15, ping_interval=20, ping_timeout=20,
                                                max_size=2**24, close_timeout=5)
        except Exception as exc:
            raise DerivError(f"Deriv connection failed: {exc}") from exc
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._ping_loop())]
        if authorize and self.token_mode == "legacy":
            try:
                auth = await self.request({"authorize": self.token})
            except DerivError:
                await self.close()
                raise
            self.account = auth.get("authorize") or {}
        if authorize:
            try:
                bal = (await self.request({"balance": 1})).get("balance") or {}
                self.account.update({k: bal[k] for k in ("balance", "currency", "loginid") if bal.get(k) is not None})
            except DerivError:
                pass
        return self.account_info()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self._fail_all(DerivError("Deriv connection closed."))

    def _fail_all(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        for queue in self._streams.values():
            queue.put_nowait(None)
        self._streams.clear()

    async def _read_loop(self) -> None:
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                req_id = message.get("req_id")
                if req_id in self._streams:
                    self._streams[req_id].put_nowait(message)
                    continue
                fut = self._pending.get(req_id)
                if fut is not None and not fut.done():
                    fut.set_result(message)
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("deriv read loop crashed")
        finally:
            if self._ws is ws:
                self._ws = None
                self._fail_all(DerivError("Deriv connection lost."))

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.ping()
            except DerivError:
                return

    # ------------------------------------------------------------- messaging
    async def request(self, payload: dict, timeout: float = 15) -> dict:
        if self._ws is None:
            raise DerivError("Deriv WebSocket is not connected.")
        req_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps({**payload, "req_id": req_id}))
            message = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise DerivError(f"Deriv request timed out: {next(iter(payload))}") from exc
        except websockets.ConnectionClosed as exc:
            raise DerivError("Deriv connection closed.") from exc
        finally:
            self._pending.pop(req_id, None)
        if "error" in message:
            err = message["error"]
            raise DerivError(err.get("message", "Deriv returned an error."), err.get("code"))
        return message

    async def subscribe(self, payload: dict) -> Subscription:
        if self._ws is None:
            raise DerivError("Deriv WebSocket is not connected.")
        req_id = next(self._ids)
        queue: asyncio.Queue = asyncio.Queue()
        self._streams[req_id] = queue
        await self._ws.send(json.dumps({**payload, "subscribe": 1, "req_id": req_id}))
        return Subscription(self, req_id, queue)

    # ----------------------------------------------------------------- calls
    async def ping(self) -> float:
        started = time.perf_counter()
        await self.request({"ping": 1}, timeout=10)
        self.last_rtt_ms = round((time.perf_counter() - started) * 1000, 1)
        return self.last_rtt_ms

    async def ticks_history_candles(self, symbol: str, granularity: int, *, end: int | str = "latest",
                                    start: int | None = None, count: int = 5000) -> list[dict]:
        payload: dict = {"ticks_history": symbol, "style": "candles", "granularity": int(granularity),
                         "end": end, "count": int(count)}
        if start is not None:
            payload["start"] = int(start)
            payload["adjust_start_time"] = 1
        return (await self.request(payload, timeout=30)).get("candles") or []

    async def active_symbols(self) -> list[dict]:
        """Normalised to include `symbol` and `display_name` (the new API uses underlying_symbol*)."""
        items = (await self.request({"active_symbols": "brief"})).get("active_symbols") or []
        for item in items:
            item.setdefault("symbol", item.get("underlying_symbol"))
            item.setdefault("display_name", item.get("underlying_symbol_name"))
        return items

    async def contracts_for(self, symbol: str) -> dict:
        return (await self.request({"contracts_for": symbol})).get("contracts_for") or {}

    async def proposal(self, **fields: Any) -> dict:
        payload = {"proposal": 1, **fields}
        try:
            return (await self.request(payload)).get("proposal") or {}
        except DerivError as exc:
            # Some API versions want `symbol`, others `underlying_symbol` (as handled by the original client).
            if "underlying_symbol" in payload and "underlying_symbol" in str(exc):
                payload["symbol"] = payload.pop("underlying_symbol")
                return (await self.request(payload)).get("proposal") or {}
            raise

    async def buy(self, proposal_id: str, price: float) -> dict:
        return (await self.request({"buy": proposal_id, "price": float(price)})).get("buy") or {}

    async def sell(self, contract_id: int | str, price: float = 0) -> dict:
        return (await self.request({"sell": int(contract_id), "price": float(price)})).get("sell") or {}

    # ------------------------------------------------------------ PAT (REST)
    def _ws_url_with_app_id(self) -> str:
        parsed = urlparse(self.ws_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["app_id"] = self.app_id
        return urlunparse(parsed._replace(query=urlencode(query)))

    @staticmethod
    def _field(obj: object, *keys: str) -> str:
        if isinstance(obj, dict):
            for key in keys:
                if obj.get(key):
                    return str(obj[key])
        return ""

    async def _rest(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Deriv-App-ID": self.app_id, "Authorization": f"Bearer {self.token}",
                   "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                response = await http.request(method, f"{self.rest_url}/{path.lstrip('/')}",
                                              headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise DerivError(f"Deriv REST request failed: {exc.__class__.__name__}") from exc
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        if response.status_code >= 400:
            errors = data.get("errors") if isinstance(data, dict) else None
            message = errors[0].get("message") if isinstance(errors, list) and errors else None
            raise DerivError(message or f"Deriv REST error {response.status_code} for {path}")
        return data if isinstance(data, dict) else {}

    async def _options_accounts(self) -> list[dict]:
        data = (await self._rest("GET", "/trading/v1/options/accounts")).get("data")
        if isinstance(data, list):
            return [a for a in data if isinstance(a, dict)]
        if isinstance(data, dict):
            nested = data.get("accounts") or data.get("items")
            return [a for a in nested if isinstance(a, dict)] if isinstance(nested, list) else [data]
        return []

    async def _select_options_account(self) -> dict:
        accounts = await self._options_accounts()
        if self.account_id:
            for account in accounts:
                if self._field(account, "account_id", "accountId", "id") == self.account_id:
                    return account
        for account in accounts:
            if self._field(account, "account_type", "accountType", "type").lower() == self.account_mode:
                return account
        if self.account_mode == "demo":
            # Never silently fall back to another (possibly real) account in demo mode.
            created = (await self._rest("POST", "/trading/v1/options/accounts",
                                        {"currency": "USD", "group": "row", "account_type": "demo"})).get("data")
            if isinstance(created, list) and created:
                created = created[0]
            if isinstance(created, dict):
                return created
            raise DerivError("Deriv did not return a demo options account.")
        raise DerivError("No real Deriv options account is available for this token.")

    async def _otp_ws_url(self, account_id: str) -> str:
        data = (await self._rest("POST", f"/trading/v1/options/accounts/{account_id}/otp")).get("data")
        url = data.get("url") if isinstance(data, dict) else None
        if not url:
            raise DerivError("Deriv OTP response did not include a WebSocket URL.")
        return str(url)
