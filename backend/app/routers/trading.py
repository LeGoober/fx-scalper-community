"""Trading: live engine control (paper / demo), signals, positions, trade journal and CSV export."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import db
from app.services import engine, risk
from app.services.deriv import profile

router = APIRouter(prefix="/api/trading", tags=["trading"])


@router.get("/engine", summary="Engine status: config, per-symbol feed, pending entries, open position")
def engine_status() -> dict:
    return engine.status()


@router.post("/engine/start", summary="Start the engine (paper = simulated fills; demo = Deriv demo orders)")
async def engine_start(cfg: engine.EngineConfig) -> dict:
    try:
        return await engine.start(cfg)
    except (RuntimeError, risk.RiskBlocked, KeyError) as exc:
        raise HTTPException(409 if isinstance(exc, RuntimeError) else 403, str(exc)) from exc


@router.post("/engine/stop",
             summary="Stop the engine and cancel pending entries (open Deriv contracts keep their SL/TP)")
async def engine_stop() -> dict:
    return await engine.stop("stopped by user")


class CalibrateRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["frxEURUSD", "frxGBPUSD", "frxXAUUSD", "OTC_NDX", "OTC_SPC"])
    rise_fall_minutes: int = 15


@router.post("/calibrate", summary="Measure Deriv's real commission, accepted multipliers and Rise/Fall payout")
async def calibrate(body: CalibrateRequest) -> dict:
    return {"profiles": await profile.calibrate(body.symbols, body.rise_fall_minutes)}


@router.get("/profiles", summary="Calibrated per-symbol contract profiles")
def profiles() -> dict:
    return {"profiles": profile.get()}


@router.get("/signals", summary="Evaluated setups (accepted and rejected) with node-by-node reasons")
def signals(limit: int = 100, status: str | None = None) -> dict:
    sql, params = "SELECT * FROM signals", []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    with db.connect() as conn:
        rows = db.rows(conn, sql + " ORDER BY created_at DESC LIMIT ?", (*params, limit))
    for r in rows:
        r["decision"] = db.loads(r.pop("decision_json"), {})
    return {"signals": rows}


@router.get("/trades", summary="Trade journal (paper/demo/real), newest first")
def trades(limit: int = 200, mode: str | None = None, status: str | None = None) -> dict:
    sql, params, where = "SELECT * FROM trades", [], []
    if mode:
        where.append("mode = ?")
        params.append(mode)
    if status:
        where.append("status = ?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with db.connect() as conn:
        rows = db.rows(conn, sql + " ORDER BY opened_at DESC LIMIT ?", (*params, limit))
    for r in rows:
        r["meta"] = db.loads(r.pop("meta_json"), {})
    return {"trades": rows}


@router.get("/trades.csv", summary="Trade journal as CSV")
def trades_csv(mode: str | None = None) -> StreamingResponse:
    rows = trades(limit=100000, mode=mode)["trades"]
    cols = ["id", "mode", "symbol", "direction", "contract_type", "stake", "entry_price", "exit_price", "stop_price",
            "target_price", "r_multiple", "pnl", "status", "opened_at", "closed_at", "contract_id", "signal_id",
            "strategy_id"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=trade_journal.csv"})
