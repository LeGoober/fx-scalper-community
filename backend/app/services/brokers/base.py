"""Broker protocol: what the execution service needs from any venue.

Deterministic code owns every order. A broker adapter only translates typed requests into one
venue's API and reports back; it never decides size or whether to trade.

Fail-closed rules every adapter follows:
  * `account_kind` is None until verified after connecting. The execution service refuses to
    trade on None, and real money additionally needs the 3-gate lock in `risk`.
  * `place` never retries a request that may have reached the venue. An ambiguous outcome is
    returned as status "unknown" and resolved later by reconciling against positions/orders.
  * Every order carries a broker-side stop (and target when the plan has one), so a crashed
    process never leaves exposure unprotected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

AccountKind = Literal["demo", "real"]
Side = Literal["buy", "sell"]


class BrokerError(RuntimeError):
    """The venue refused or failed a request; `retryable` only for requests that cannot have executed."""

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str                 # our symbol (the Deriv-style one the strategy uses)
    broker_symbol: str          # the venue's id (Capital.com "epic")
    currency: str               # currency P&L is paid in (quote currency)
    min_size: float
    size_step: float
    value_per_point: float      # P&L per 1.0 price move per 1 unit of size, in `currency`
    min_stop_distance: float    # price units; stops closer than this are refused by the venue
    decimals: int = 5
    tradeable: bool = True
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    ts: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    size: float
    order_type: Literal["market", "limit"]
    level: float | None          # limit price (None for market)
    stop_level: float
    tp_level: float | None
    good_till: int | None = None  # epoch seconds (limit orders)


@dataclass
class OrderAck:
    client_order_id: str
    status: Literal["working", "filled", "rejected", "unknown"]
    deal_id: str | None = None
    deal_reference: str | None = None
    fill_price: float | None = None
    reason: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class BrokerPosition:
    deal_id: str
    symbol: str
    side: Side
    size: float
    open_price: float
    stop_level: float | None
    tp_level: float | None
    opened_at: str | None = None
    upl: float | None = None


@dataclass
class WorkingOrder:
    deal_id: str
    symbol: str
    side: Side
    size: float
    level: float
    stop_level: float | None = None
    tp_level: float | None = None
    good_till: str | None = None


class Broker(Protocol):
    name: str
    account_kind: AccountKind | None

    async def connect(self) -> dict: ...
    async def close(self) -> None: ...
    async def instrument(self, symbol: str) -> InstrumentSpec: ...
    async def quote(self, symbol: str) -> Quote: ...
    async def place(self, req: OrderRequest) -> OrderAck: ...
    async def cancel_working(self, deal_id: str) -> None: ...
    async def close_position(self, deal_id: str) -> OrderAck: ...
    async def positions(self) -> list[BrokerPosition]: ...
    async def working_orders(self) -> list[WorkingOrder]: ...
    async def activity(self, deal_id: str | None = None, last_seconds: int = 3600) -> list[dict]: ...
    def info(self) -> dict: ...
