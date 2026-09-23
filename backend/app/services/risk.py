"""Order-time risk gate. Every order to Deriv goes through `check_order()`, no exceptions.

The demo lock is fail-closed: an account counts as demo only when every signal
the client has agrees (server-issued OTP endpoint path and account metadata).
Real money additionally requires ALL of:
  1. COMMUNITY_ALLOW_REAL_TRADING=true, hand-edited into .env (the API cannot set it);
  2. a per-process arm via `arm_real()` (UI confirmation), cleared on restart;
  3. passing the same limits as demo.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from app import config, db, events

DEFAULTS = {
    "max_stake": 100.0,          # per order, account currency (multipliers need stake ≫ risk on tight stops)
    "max_risk_per_trade": 5.0,   # loss at the planned stop (1R), account currency
    "max_daily_loss": 20.0,      # realised loss today before new orders stop
    "max_concurrent": 3,         # open engine/live trades
    "max_orders_per_day": 30,
}

_state_lock = threading.Lock()
_kill_switch = False
_real_armed = False


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
    return _kill_switch


def set_kill_switch(active: bool) -> None:
    global _kill_switch
    with _state_lock:
        _kill_switch = bool(active)
    events.publish("risk.kill_switch", {"active": _kill_switch}, level="warning" if active else "info",
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


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def today_stats() -> dict:
    day = _today_utc()
    with db.connect() as conn:
        r = db.row(conn, """
            SELECT COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0) AS realised_loss,
                   COALESCE(SUM(pnl), 0) AS realised_pnl,
                   COUNT(*) AS orders,
                   SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_now
            FROM trades WHERE mode IN ('demo', 'real') AND substr(opened_at, 1, 10) = ?""", (day,))
        open_total = db.row(conn, "SELECT COUNT(*) AS n FROM trades WHERE mode IN ('demo','real') "
                                  "AND status = 'open'")["n"]
    return {"day": day, "realised_loss": round(r["realised_loss"], 2), "realised_pnl": round(r["realised_pnl"], 2),
            "orders": r["orders"], "open": open_total}


def check_order(intent: OrderIntent) -> None:
    """Raise RiskBlocked unless the order may be sent. Called immediately before `buy`."""
    def block(reason: str) -> None:
        events.publish("risk.blocked", {"symbol": intent.symbol, "stake": intent.stake, "reason": reason},
                       level="warning", message=f"Order blocked: {reason}")
        raise RiskBlocked(reason)

    if _kill_switch:
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
    if stats["open"] >= lim["max_concurrent"]:
        block(f"Max concurrent trades reached ({stats['open']}).")
    if stats["orders"] >= lim["max_orders_per_day"]:
        block(f"Max orders per day reached ({stats['orders']}).")
