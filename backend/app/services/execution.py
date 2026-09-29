"""Execution service: turns an approved decision into a broker order, and keeps the ledger true.

Order lifecycle (trades.status): submitting → working (limit resting at the broker) → open → closed,
or rejected / cancelled / expired / unknown / failed.

Idempotency, the rule that prevents duplicate orders:
  1. An order_intents row (state 'submitting') and a trades row are written BEFORE the HTTP call,
     so a crash mid-request still leaves a record that reconcile() resolves.
  2. An ambiguous outcome (timeout after sending) becomes 'unknown' and is never resent.
     reconcile() matches it against the broker's positions and working orders by symbol, side and
     size; after two misses it is marked 'failed' (no order exists).
  3. One decision per proposal (UNIQUE setup key) and one intent per decision.

Sizing is deterministic: risk.size_for_risk() on the broker's dealing rules, with the spread added
to the money at risk, then risk.check_order() (limits, kill switch, master switch, real-money lock).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Callable

from app import db, events
from app.services import risk
from app.services.brokers.base import AccountKind, Broker, BrokerError, OrderRequest

log = logging.getLogger("fxs.execution")

POLL_S = 3.0
SETTLE_AFTER_S = 30  # never judge an order still in flight: requests + confirmation can take ~15 s
UNKNOWN_MISSES_TO_FAIL = 2
BrokerFactory = Callable[[str, AccountKind], Broker]


def _default_factory(name: str, kind: AccountKind) -> Broker:
    if name == "capital":
        from app.services.brokers.capital import CapitalBroker
        return CapitalBroker(kind)
    raise BrokerError(f"No execution adapter for broker '{name}'. Deriv orders still run inside the engine.")


def _age_s(t: dict) -> float:
    from datetime import datetime
    try:
        return time.time() - datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        return 0.0


class ExecutionService:
    def __init__(self, factory: BrokerFactory | None = None) -> None:
        self.factory = factory or _default_factory
        self._brokers: dict[tuple[str, str], Broker] = {}
        self._lock = asyncio.Lock()
        self._poller: asyncio.Task | None = None
        self._misses: dict[str, int] = {}

    # ------------------------------------------------------------ brokers
    async def broker(self, name: str, kind: AccountKind) -> Broker:
        key = (name, kind)
        async with self._lock:
            b = self._brokers.get(key)
            if b is None:
                b = self.factory(name, kind)
                await b.connect()
                if b.account_kind != kind:  # fail closed: the venue must confirm the account kind we asked for
                    await b.close()
                    raise risk.RiskBlocked(f"{name} account is not verified as {kind}.")
                self._brokers[key] = b
            return b

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()
            self._poller = None
        for b in self._brokers.values():
            await b.close()
        self._brokers.clear()

    def status(self) -> dict:
        return {"brokers": [b.info() for b in self._brokers.values()],
                "polling": bool(self._poller and not self._poller.done())}

    # ------------------------------------------------------------ execute
    async def execute(self, *, decision_id: str, proposal: dict, broker_name: str, kind: AccountKind,
                      risk_amount: float, auto: bool, reference_price: float | None = None) -> dict:
        """Size, check and place one order. Returns {'verdict', 'reasons', 'trade_id', 'sizing'}."""
        b = await self.broker(broker_name, kind)
        symbol, long = proposal["symbol"], proposal["direction"] == "long"
        spec = await b.instrument(symbol)
        if not spec.tradeable:
            return {"verdict": "blocked", "reasons": [f"{spec.broker_symbol} is not tradeable right now"]}
        account_ccy = (b.info().get("currency") or "").upper()
        if spec.currency and account_ccy and spec.currency.upper() != account_ccy:
            return {"verdict": "blocked", "reasons": [
                f"{spec.broker_symbol} pays P&L in {spec.currency}, the account is {account_ccy}: currency "
                "conversion is not supported yet, so the risk could not be sized exactly"]}
        quote = await b.quote(symbol)
        entry, stop, target = proposal.get("entry"), float(proposal["stop"]), proposal.get("target")
        order_type = "limit" if proposal.get("entry_type") == "limit" and entry is not None else "market"
        basis = 0.0
        if reference_price is not None:
            # The signal was computed on another feed (Deriv). Shift the whole plan by the venue gap, and
            # refuse when the gap is a meaningful fraction of the risk (it would silently change the trade).
            basis = quote.mid - reference_price
            one_r = abs((entry if entry is not None else reference_price) - stop)
            if one_r > 0 and abs(basis) > risk.limits()["max_basis_r"] * one_r:
                return {"verdict": "blocked", "reasons": [
                    f"Price gap between feeds is {basis:+.5f}, {abs(basis) / one_r:.2f}R: above max_basis_r"]}
            stop += basis
            entry = entry + basis if entry is not None else None
            target = target + basis if target is not None else None
        fill_ref = entry if order_type == "limit" else (quote.ask if long else quote.bid)
        if (long and stop >= fill_ref) or (not long and stop <= fill_ref):
            return {"verdict": "blocked", "reasons": ["stop is on the wrong side of the entry"]}
        beyond = target is not None and ((long and target <= fill_ref) or (not long and target >= fill_ref))
        if order_type == "market" and beyond:
            return {"verdict": "blocked", "reasons": ["price is already beyond the target"]}
        if abs(fill_ref - stop) < spec.min_stop_distance:
            return {"verdict": "blocked", "reasons": [
                f"stop {abs(fill_ref - stop):.5f} away is closer than the venue minimum "
                f"{spec.min_stop_distance:.5f}"]}
        try:
            size, at_risk = risk.size_for_risk(risk_amount, fill_ref, stop, spec.value_per_point, spec.size_step,
                                               spec.min_size)
            at_risk = round(at_risk + size * quote.spread * spec.value_per_point, 4)  # exits pay the spread
            notional = size * fill_ref * spec.value_per_point
            risk.check_order(risk.OrderIntent(
                symbol=symbol, stake=at_risk, contract_type=f"CFD_{'BUY' if long else 'SELL'}",
                is_virtual=(b.account_kind == "demo"), risk_amount=at_risk, auto=auto, notional=notional))
        except risk.RiskBlocked as exc:
            return {"verdict": "blocked", "reasons": [str(exc)]}
        sizing = {"size": size, "at_risk": at_risk, "notional": round(notional, 2), "spread": quote.spread,
                  "basis": round(basis, 6), "entry": entry, "stop": stop, "target": target,
                  "order_type": order_type, "min_size": spec.min_size, "size_step": spec.size_step,
                  "value_per_point": spec.value_per_point}
        trade_id, coid = f"t-{uuid.uuid4().hex[:10]}", f"co-{uuid.uuid4().hex[:12]}"
        req = OrderRequest(coid, symbol, "buy" if long else "sell", size, order_type,
                           entry if order_type == "limit" else None, stop, target,
                           proposal.get("expires_at") if order_type == "limit" else None)
        now = db.utc_now()
        with db.connect() as conn, db.tx(conn):  # write-ahead: the record exists before the venue sees anything
            conn.execute("INSERT INTO order_intents(client_order_id, decision_id, trade_id, broker, account_kind, "
                         "symbol, broker_symbol, side, size, order_type, level, stop_level, tp_level, state, "
                         "request_json, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'submitting',?,?,?)",
                         (coid, decision_id, trade_id, broker_name, b.account_kind, symbol, spec.broker_symbol,
                          req.side, size, order_type, req.level, stop, target, db.dumps(sizing), now, now))
            conn.execute("INSERT INTO trades(id, mode, symbol, direction, contract_type, stake, entry_price, "
                         "stop_price, target_price, status, opened_at, signal_id, strategy_id, meta_json, risk_amount, "
                         "broker, "
                         "decision_id, auto, size) VALUES (?,?,?,?,?,?,?,?,?, 'submitting', ?,?,?,?,?,?,?,?,?)",
                         (trade_id, b.account_kind, symbol, proposal["direction"], f"cfd_{order_type}", at_risk,
                          entry, stop, target, now, proposal.get("signal_id"), proposal.get("strategy_id"),
                          db.dumps({"sizing": sizing, "client_order_id": coid}), at_risk, broker_name, decision_id,
                          int(auto), size))
        events.publish("order.submitted", {"trade_id": trade_id, "symbol": symbol, "side": req.side, "size": size,
                                           "type": order_type, "broker": broker_name},
                       message=f"{broker_name} {req.side.upper()} {size} {spec.broker_symbol} ({order_type})")
        ack = await b.place(req)
        state = {"working": "working", "filled": "open", "rejected": "rejected", "unknown": "unknown"}[ack.status]
        with db.connect() as conn, db.tx(conn):
            conn.execute("UPDATE order_intents SET state=?, deal_id=?, deal_reference=?, reason=?, response_json=?, "
                         "updated_at=? WHERE client_order_id=?",
                         (ack.status, ack.deal_id, ack.deal_reference, ack.reason, db.dumps(ack.raw), db.utc_now(),
                          coid))
            conn.execute("UPDATE trades SET status=?, deal_id=?, entry_price=COALESCE(?, entry_price), "
                         "closed_at=CASE WHEN ? = 'rejected' THEN ? ELSE closed_at END WHERE id=?",
                         (state, ack.deal_id, ack.fill_price, state, db.utc_now(), trade_id))
        level = "warning" if ack.status in {"rejected", "unknown"} else "info"
        payload = {"trade_id": trade_id, "symbol": symbol, "deal_id": ack.deal_id, "reason": ack.reason}
        events.publish(f"order.{ack.status}", payload, level=level,
                       message=f"Order {ack.status}: {spec.broker_symbol}" + (f" ({ack.reason})" if ack.reason else ""))
        if state == "open":
            events.publish("trade.opened", {"id": trade_id, "mode": b.account_kind, "symbol": symbol,
                                            "direction": proposal["direction"], "entry": ack.fill_price, "stop": stop,
                                            "target": target}, message=f"{b.account_kind.upper()} {symbol} opened")
        self.ensure_polling()
        verdict = {"working": "executed", "filled": "executed", "rejected": "rejected",
                   "unknown": "unknown"}[ack.status]
        return {"verdict": verdict, "reasons": [ack.reason] if ack.reason else [], "trade_id": trade_id,
                "sizing": sizing}

    # ------------------------------------------------------------ OFF controls
    async def close_trade(self, trade_id: str, reason: str = "manual") -> dict:
        with db.connect() as conn:
            t = db.row(conn, "SELECT * FROM trades WHERE id = ?", (trade_id,))
        if t is None:
            raise KeyError(trade_id)
        if t["status"] not in risk.ACTIVE_STATUSES or not t.get("broker"):
            return {"trade_id": trade_id, "status": t["status"], "note": "nothing to close"}
        b = await self.broker(t["broker"], t["mode"])
        if t["status"] == "working" and t["deal_id"]:
            await b.cancel_working(t["deal_id"])
            self._settle(t, "cancelled", None, reason)
            return {"trade_id": trade_id, "status": "cancelled"}
        if t["status"] == "open" and t["deal_id"]:
            ack = await b.close_position(t["deal_id"])
            if ack.status == "filled":
                self._settle(t, "closed", ack.fill_price, reason)
                return {"trade_id": trade_id, "status": "closed", "exit": ack.fill_price}
            return {"trade_id": trade_id, "status": t["status"], "note": f"close request {ack.status}; reconciling"}
        return {"trade_id": trade_id, "status": t["status"], "note": "state unknown; reconcile resolves it"}

    async def flatten(self, reason: str) -> dict:
        """Cancel every working order and close every position the system placed automatically."""
        with db.connect() as conn:
            ids = [r["id"] for r in db.rows(conn, "SELECT id FROM trades WHERE auto = 1 AND broker IS NOT NULL AND "
                                                  "status IN ('working', 'open')")]
        results = []
        for trade_id in ids:
            try:
                results.append(await self.close_trade(trade_id, reason))
            except (BrokerError, risk.RiskBlocked) as exc:
                results.append({"trade_id": trade_id, "error": str(exc)})
        events.publish("autonomy.flattened", {"reason": reason, "results": results}, level="warning",
                       message=f"Flattened {len(ids)} automated trade(s): {reason}")
        return {"reason": reason, "results": results}

    # ------------------------------------------------------------ reconcile
    def _settle(self, t: dict, status: str, exit_px: float | None, reason: str, *, estimated: bool = False) -> None:
        r = pnl = None
        entry, stop = t.get("entry_price"), t.get("stop_price")
        if exit_px is not None and entry is not None and stop is not None and entry != stop and t.get("size"):
            sign = 1 if t["direction"] == "long" else -1
            gross = sign * (exit_px - entry)
            r = gross / abs(entry - stop)
            vpp = float((db.loads(t.get("meta_json"), {}) or {}).get("sizing", {}).get("value_per_point") or 1.0)
            pnl = round(gross * t["size"] * vpp, 2)
        with db.connect() as conn:
            conn.execute("UPDATE trades SET status=?, exit_price=?, r_multiple=?, pnl=?, closed_at=?, meta_json="
                         "json_set(json_set(COALESCE(meta_json,'{}'), '$.exit_reason', ?), '$.pnl_estimated', ?) "
                         "WHERE id=?", (status, exit_px, round(r, 4) if r is not None else None, pnl, db.utc_now(),
                                        reason, 1 if estimated else 0, t["id"]))
        if status == "closed":
            events.publish("trade.closed", {"id": t["id"], "mode": t["mode"], "symbol": t["symbol"],
                                            "r": round(r, 3) if r is not None else None, "pnl": pnl, "reason": reason},
                           message=f"{t['mode'].upper()} {t['symbol']} closed ({reason})")

    async def reconcile(self) -> dict:
        """Bring every active broker trade in line with what the venue actually holds."""
        with db.connect() as conn:
            active = db.rows(conn, "SELECT * FROM trades WHERE broker IS NOT NULL AND status IN "
                                   "('submitting', 'working', 'open', 'unknown')")
        summary = {"checked": len(active), "filled": 0, "closed": 0, "expired": 0, "linked": 0, "failed": 0}
        by_venue: dict[tuple[str, str], list[dict]] = {}
        for t in active:
            by_venue.setdefault((t["broker"], t["mode"]), []).append(t)
        for (name, kind), trades in by_venue.items():
            b = await self.broker(name, kind)  # type: ignore[arg-type]
            positions, working = await b.positions(), await b.working_orders()
            owned = {t["deal_id"] for t in trades if t["deal_id"]}
            free_pos = [p for p in positions if p.deal_id not in owned]
            working_ids = {w.deal_id for w in working}
            pos_by_id = {p.deal_id: p for p in positions}
            for t in trades:
                side = "buy" if t["direction"] == "long" else "sell"
                match = next((p for p in free_pos if p.symbol == t["symbol"] and p.side == side
                              and abs(p.size - (t["size"] or 0)) < 1e-9), None)
                if t["status"] == "working" and t["deal_id"] not in working_ids:
                    if match:  # the limit filled: the position carries a new deal id
                        free_pos.remove(match)
                        self._mark_open(t, match.deal_id, match.open_price)
                        summary["filled"] += 1
                    else:
                        self._settle(t, "expired", None, "limit order no longer working (expired or cancelled)")
                        summary["expired"] += 1
                elif t["status"] == "open" and t["deal_id"] not in pos_by_id:
                    exit_px, why = await self._exit_estimate(b, t)
                    self._settle(t, "closed", exit_px, why, estimated=True)
                    summary["closed"] += 1
                elif t["status"] in {"unknown", "submitting"} and _age_s(t) >= SETTLE_AFTER_S:
                    linked = next((w for w in working if w.symbol == t["symbol"] and w.side == side
                                   and abs(w.size - (t["size"] or 0)) < 1e-9 and w.deal_id not in owned), None)
                    if linked:
                        self._link(t, "working", linked.deal_id)
                        summary["linked"] += 1
                    elif match:
                        free_pos.remove(match)
                        self._mark_open(t, match.deal_id, match.open_price)
                        summary["linked"] += 1
                    else:
                        self._misses[t["id"]] = self._misses.get(t["id"], 0) + 1
                        if self._misses[t["id"]] >= UNKNOWN_MISSES_TO_FAIL:
                            self._settle(t, "failed", None, "no order found at the broker")
                            summary["failed"] += 1
        if any(v for k, v in summary.items() if k != "checked"):
            events.publish("order.reconciled", summary, message=f"Reconciled: {summary}")
        return summary

    def _link(self, t: dict, status: str, deal_id: str) -> None:
        with db.connect() as conn:
            conn.execute("UPDATE trades SET status=?, deal_id=? WHERE id=?", (status, deal_id, t["id"]))
            conn.execute("UPDATE order_intents SET state=?, deal_id=?, updated_at=? WHERE trade_id=?",
                         (status, deal_id, db.utc_now(), t["id"]))

    def _mark_open(self, t: dict, deal_id: str, price: float) -> None:
        with db.connect() as conn:
            conn.execute("UPDATE trades SET status='open', deal_id=?, entry_price=? WHERE id=?",
                         (deal_id, price, t["id"]))
        events.publish("trade.opened", {"id": t["id"], "mode": t["mode"], "symbol": t["symbol"],
                                        "direction": t["direction"], "entry": price, "stop": t["stop_price"],
                                        "target": t["target_price"]},
                       message=f"{t['mode'].upper()} {t['symbol']} filled")

    @staticmethod
    async def _exit_estimate(b: Broker, t: dict) -> tuple[float | None, str]:
        """Which exit closed the position: the broker's activity log says SL/TP when it can."""
        try:
            acts = await b.activity(t["deal_id"], last_seconds=86400)
        except BrokerError:
            acts = []
        sources = {str(a.get("source")).upper() for a in acts}
        if "TP" in sources:
            return t["target_price"], "target (broker take-profit)"
        if "SL" in sources:
            return t["stop_price"], "stop (broker stop-loss)"
        return None, "closed at the broker (" + (", ".join(sorted(sources)) or "source unknown") + ")"

    # ------------------------------------------------------------ polling
    def ensure_polling(self) -> None:
        if self._poller and not self._poller.done():
            return
        try:
            self._poller = asyncio.get_running_loop().create_task(self._poll(), name="execution:reconcile")
        except RuntimeError:
            pass

    async def _poll(self) -> None:
        idle_since = time.monotonic()
        while True:
            await asyncio.sleep(POLL_S)
            with db.connect() as conn:
                active = conn.execute("SELECT COUNT(*) FROM trades WHERE broker IS NOT NULL AND status IN "
                                      "('submitting', 'working', 'open', 'unknown')").fetchone()[0]
            if not active:
                if time.monotonic() - idle_since > 60:
                    return  # nothing at the venue: stop polling until the next order
                continue
            idle_since = time.monotonic()
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep polling; surface the problem
                events.publish("order.reconcile_error", {"error": str(exc)}, level="error",
                               message=f"Reconcile failed: {exc}")


SERVICE = ExecutionService()
