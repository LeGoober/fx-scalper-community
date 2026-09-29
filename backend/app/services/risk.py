"""Order-time risk gate. Every order to Deriv goes through `check_order()`, no exceptions.

The demo lock is fail-closed: an account counts as demo only when every signal
the client has agrees (server-issued OTP endpoint path and account metadata).
Real money additionally requires ALL of:
  1. COMMUNITY_ALLOW_REAL_TRADING=true, hand-edited into .env (the API cannot set it);
  2. a per-process arm via `arm_real()` (UI confirmation), cleared on restart;
  3. passing the same limits as demo.

The kill switch is stored in the database, so a restart never silently releases it (fail-closed).
The daily loss limit counts realised losses plus the risk still open at the stops, plus the new
order's own risk, on the ICT/New York trading day (rolls at 17:00 New York).
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from app import config, db, events
from app.services.ict import features as F

DEFAULTS = {
    "max_stake": 100.0,          # per order, account currency (multipliers need stake ≫ risk on tight stops)
    "max_risk_per_trade": 5.0,   # loss at the planned stop (1R), account currency
    "max_daily_loss": 20.0,      # realised loss today before new orders stop
    "max_concurrent": 3,         # open engine/live trades
    "max_orders_per_day": 30,
}

_state_lock = threading.Lock()
_real_armed = False  # deliberately per-process: arming real money must be repeated after every restart
KILL_KEY = "risk_kill_switch"


class RiskBlocked(PermissionError):
    pass


@dataclass
class OrderIntent:
    symbol: str
    stake: float
    contract_type: str
    is_virtual: bool | None  # from DerivClient.is_virtual after connect
    risk_amount: float | None = None  # loss at the stop; None for fixed-stake options (risk = stake)


def limits() -> dict:
    stored = db.kv_get("risk_limits") or {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def set_limits(values: dict) -> dict:
    current = limits()
    for key, value in values.items():
        if key not in DEFAULTS or value is None:
            continue
        number = float(value)
        if number <= 0:
            raise ValueError(f"{key} must be positive")
        current[key] = int(number) if isinstance(DEFAULTS[key], int) else number
    db.kv_set("risk_limits", current)
    events.publish("risk.limits", current, message="Risk limits updated")
    return current


def kill_switch_active() -> bool:
    return bool((db.kv_get(KILL_KEY) or {}).get("active"))


def set_kill_switch(active: bool) -> None:
    with _state_lock:
        db.kv_set(KILL_KEY, {"active": bool(active), "at": db.utc_now()})
    events.publish("risk.kill_switch", {"active": bool(active)}, level="warning" if active else "info",
                   message="Kill switch ENGAGED" if active else "Kill switch released")


def real_trading_status() -> dict:
    return {"env_allows": config.allow_real_trading(), "armed": _real_armed,
            "effective": config.allow_real_trading() and _real_armed}


def arm_real(confirm_phrase: str) -> dict:
    global _real_armed
    if not config.allow_real_trading():
        raise RiskBlocked("Real trading is disabled. It can only be enabled by editing COMMUNITY_ALLOW_REAL_TRADING "
                          "in .env by hand.")
    if confirm_phrase.strip() != "I ACCEPT REAL MONEY RISK":
        raise RiskBlocked("Confirmation phrase does not match.")
    with _state_lock:
        _real_armed = True
    events.publish("risk.real_armed", real_trading_status(), level="warning", message="Real trading ARMED")
    return real_trading_status()


def disarm_real() -> dict:
    global _real_armed
    with _state_lock:
        _real_armed = False
    events.publish("risk.real_disarmed", real_trading_status(), message="Real trading disarmed")
    return real_trading_status()


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def trading_day(epoch: float | None = None) -> str:
    """The ICT/New York trading day (17:00 New York rollover), the day every limit resets on."""
    return F.ny_trading_day(int(epoch if epoch is not None else datetime.now(timezone.utc).timestamp()))


def today_stats() -> dict:
    day = trading_day()
    since = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 2 * 86400, timezone.utc)
    with db.connect() as conn:
        recent = db.rows(conn, "SELECT pnl, opened_at FROM trades WHERE mode IN ('demo', 'real') AND opened_at >= ?",
                         (since.isoformat(timespec="seconds").replace("+00:00", "Z"),))
        open_rows = db.rows(conn, "SELECT stake, risk_amount FROM trades WHERE mode IN ('demo', 'real') "
                                  "AND status = 'open'")
    todays = [r for r in recent if trading_day(_epoch(r["opened_at"])) == day]
    loss = sum(-r["pnl"] for r in todays if r["pnl"] is not None and r["pnl"] < 0)
    pnl = sum(r["pnl"] for r in todays if r["pnl"] is not None)
    # An open trade can still lose its full risk; without a recorded risk, assume the whole stake.
    open_risk = sum(r["risk_amount"] if r["risk_amount"] is not None else (r["stake"] or 0.0) for r in open_rows)
    return {"day": day, "realised_loss": round(loss, 2), "realised_pnl": round(pnl, 2), "orders": len(todays),
            "open": len(open_rows), "open_risk": round(open_risk, 2)}


def size_for_risk(risk_budget: float, entry: float, stop: float, value_per_point: float, size_step: float,
                  min_size: float, tolerance: float = 1.05) -> tuple[float, float]:
    """Position size so that a stop-out loses at most `risk_budget` (account currency).

    Returns (size, risk at the stop). Rounds DOWN to the broker's size step, and refuses when even the
    minimum size would lose more than `tolerance` x the budget, instead of silently risking more."""
    distance = abs(entry - stop)
    if distance <= 0 or value_per_point <= 0 or size_step <= 0:
        raise RiskBlocked("Cannot size: stop distance, point value and size step must be positive.")
    size = math.floor(risk_budget / (distance * value_per_point) / size_step + 1e-9) * size_step
    if size < min_size:
        min_risk = min_size * distance * value_per_point
        if min_risk > risk_budget * tolerance:
            raise RiskBlocked(f"Minimum size {min_size} would risk {min_risk:.2f} at the stop, above the "
                              f"{risk_budget:.2f} budget. Raise the budget or skip this trade.")
        size = min_size
    size = round(size, 10)
    return size, round(size * distance * value_per_point, 4)


def check_order(intent: OrderIntent) -> None:
    """Raise RiskBlocked unless the order may be sent. Called immediately before `buy`."""
    def block(reason: str) -> None:
        events.publish("risk.blocked", {"symbol": intent.symbol, "stake": intent.stake, "reason": reason},
                       level="warning", message=f"Order blocked: {reason}")
        raise RiskBlocked(reason)

    if kill_switch_active():
        block("Kill switch is engaged.")
    if intent.is_virtual is not True:
        status = real_trading_status()
        if intent.is_virtual is None:
            block("Could not verify the account is a demo account; refusing (fail-closed).")
        if not status["effective"]:
            block("Account is REAL and real trading is not enabled+armed. Demo-only lock is active.")
    lim = limits()
    if intent.stake <= 0 or intent.stake > lim["max_stake"]:
        block(f"Stake {intent.stake} exceeds max_stake {lim['max_stake']}.")
    at_risk = intent.risk_amount if intent.risk_amount is not None else intent.stake
    if at_risk > lim["max_risk_per_trade"]:
        block(f"Risk {at_risk} exceeds max_risk_per_trade {lim['max_risk_per_trade']}.")
    stats = today_stats()
    if stats["realised_loss"] >= lim["max_daily_loss"]:
        block(f"Daily loss limit reached ({stats['realised_loss']} ≥ {lim['max_daily_loss']}).")
    worst_case = stats["realised_loss"] + stats["open_risk"] + at_risk
    if worst_case > lim["max_daily_loss"] + 1e-9:
        block(f"If every open trade and this one stopped out, today's loss would be {worst_case:.2f}, above "
              f"max_daily_loss {lim['max_daily_loss']}.")
    if stats["open"] >= lim["max_concurrent"]:
        block(f"Max concurrent trades reached ({stats['open']}).")
    if stats["orders"] >= lim["max_orders_per_day"]:
        block(f"Max orders per day reached ({stats['orders']}).")
