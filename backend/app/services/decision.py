"""Decision pipeline: every trade the system considers goes through here, and leaves a dossier.

    Proposal  (a signal source says: this setup, these levels)
      → policy (deterministic code: kill switch, master switch, expiry; agents plug in here later,
                and may only turn 'execute' into 'skip', never the reverse)
      → execution (sizing + risk.check_order + broker)
      → ledger (proposals · decisions · order_intents · trades)

A proposal is identified by its setup key, so the same setup can never be decided (or ordered) twice.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from app import db, events
from app.services import risk

EntryType = Literal["limit", "market"]


@dataclass
class Proposal:
    source: str                      # "ict_engine", "manual", later "tv:<alert>" and lab strategies
    symbol: str
    direction: Literal["long", "short"]
    stop: float
    setup_key: str                   # stable id of the setup (dedupe)
    entry: float | None = None       # None → market order
    target: float | None = None
    entry_type: EntryType = "limit"
    expires_at: int | None = None
    strategy_id: str | None = None
    strategy_version: int | None = None
    signal_id: str | None = None
    evidence: dict = field(default_factory=dict)   # the analysis behind it (rule results, confidence JSON…)
    id: str = field(default_factory=lambda: f"prop-{uuid.uuid4().hex[:10]}")


def policy(p: Proposal, *, auto: bool) -> tuple[str, list[str]]:
    """Deterministic pre-trade policy. Returns ('execute' | 'skip', reasons)."""
    reasons = []
    if risk.kill_switch_active():
        reasons.append("kill switch engaged")
    master = risk.autonomy()["master"]
    if auto and master != "on":
        reasons.append(f"master switch {master.upper()}")
    if p.expires_at is not None and p.expires_at <= time.time():
        reasons.append("setup expired before it could be placed")
    return ("skip" if reasons else "execute"), reasons


async def submit(p: Proposal, *, broker: str, account_kind: str, risk_amount: float, auto: bool = True,
                 reference_price: float | None = None) -> dict:
    """Record, decide and (if approved) execute one proposal. Idempotent per (source, symbol, setup_key)."""
    now = db.utc_now()
    try:
        with db.connect() as conn:
            conn.execute("INSERT INTO proposals(id, created_at, source, strategy_id, strategy_version, symbol, "
                         "direction, entry, stop, target, entry_type, expires_at, setup_key, signal_id, evidence_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (p.id, now, p.source, p.strategy_id, p.strategy_version, p.symbol, p.direction, p.entry,
                          p.stop, p.target, p.entry_type, p.expires_at, p.setup_key, p.signal_id,
                          db.dumps(p.evidence)))
    except sqlite3.IntegrityError:
        return {"status": "duplicate", "setup_key": p.setup_key}
    events.publish("proposal.new", {"id": p.id, "source": p.source, "symbol": p.symbol, "direction": p.direction},
                   message=f"Proposal {p.symbol} {p.direction} ({p.source})")
    verdict, reasons = policy(p, auto=auto)
    result: dict = {"verdict": "skipped", "reasons": reasons, "trade_id": None, "sizing": None}
    decision_id = f"dec-{uuid.uuid4().hex[:10]}"
    if verdict == "execute":
        from app.services import execution
        try:
            result = await execution.SERVICE.execute(decision_id=decision_id, proposal=asdict(p), broker_name=broker,
                                                     kind=account_kind, risk_amount=risk_amount, auto=auto,
                                                     reference_price=reference_price)
        except Exception as exc:  # the venue or network failed before anything was sent
            result = {"verdict": "error", "reasons": [f"{type(exc).__name__}: {exc}"], "trade_id": None}
    with db.connect() as conn:
        conn.execute("INSERT INTO decisions(id, proposal_id, decided_at, broker, account_kind, auto, verdict, "
                     "reasons_json, autonomy_json, sizing_json, trade_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (decision_id, p.id, db.utc_now(), broker, account_kind, int(auto), result["verdict"],
                      db.dumps(result.get("reasons") or []), db.dumps(risk.autonomy()), db.dumps(result.get("sizing")),
                      result.get("trade_id")))
    level = "info" if result["verdict"] in {"executed", "skipped"} else "warning"
    events.publish("decision.made", {"id": decision_id, "proposal_id": p.id, "symbol": p.symbol,
                                     "verdict": result["verdict"], "reasons": result.get("reasons"),
                                     "trade_id": result.get("trade_id")}, level=level,
                   message=f"{p.symbol} {p.direction}: {result['verdict']}"
                   + (f" ({'; '.join(result.get('reasons') or [])})" if result.get("reasons") else ""))
    return {"status": "decided", "decision_id": decision_id, "proposal_id": p.id} | result


def dossier(decision_id: str) -> dict | None:
    """Everything behind one decision: proposal + evidence, verdict, sizing, order intents, trade, signal."""
    with db.connect() as conn:
        d = db.row(conn, "SELECT * FROM decisions WHERE id = ?", (decision_id,))
        if d is None:
            return None
        p = db.row(conn, "SELECT * FROM proposals WHERE id = ?", (d["proposal_id"],))
        intents = db.rows(conn, "SELECT * FROM order_intents WHERE decision_id = ? ORDER BY created_at", (decision_id,))
        trade = db.row(conn, "SELECT * FROM trades WHERE id = ?", (d["trade_id"],)) if d["trade_id"] else None
        signal = db.row(conn, "SELECT id, created_at, status, decision_json FROM signals WHERE id = ?",
                        (p["signal_id"],)) if p and p["signal_id"] else None
    for row, keys in ((d, ("reasons_json", "autonomy_json", "sizing_json")), (p, ("evidence_json",))):
        for k in keys:
            if row is not None:
                row[k.removesuffix("_json")] = db.loads(row.pop(k), None)
    for i in intents:
        i["request"], i["response"] = db.loads(i.pop("request_json"), None), db.loads(i.pop("response_json"), None)
    if trade:
        trade["meta"] = db.loads(trade.pop("meta_json"), None)
    if signal:
        signal["decision"] = db.loads(signal.pop("decision_json"), None)
    return {"decision": d, "proposal": p, "intents": intents, "trade": trade, "signal": signal}


def recent(limit: int = 100) -> list[dict]:
    with db.connect() as conn:
        rows = db.rows(conn, """
            SELECT d.id, d.decided_at, d.verdict, d.reasons_json, d.broker, d.account_kind, d.auto, d.trade_id,
                   p.source, p.symbol, p.direction, p.entry, p.stop, p.target, p.entry_type, p.strategy_id,
                   p.strategy_version, t.status AS trade_status, t.r_multiple, t.pnl, t.size
            FROM decisions d JOIN proposals p ON p.id = d.proposal_id
            LEFT JOIN trades t ON t.id = d.trade_id ORDER BY d.decided_at DESC LIMIT ?""", (limit,))
    for r in rows:
        r["reasons"] = db.loads(r.pop("reasons_json"), [])
    return rows
