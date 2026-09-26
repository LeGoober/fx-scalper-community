"""Backtests: run a strategy schema on stored candles (code, Jev, or both compared)."""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Body, HTTPException

from app import db, jobs
from app.routers.legacy import legacy_backtest
from app.services.backtest import engine
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/backtests", tags=["backtests"])


class BacktestRequest(BaseModel):
    strategy_id: str = "ict_2022_model"
    version: int | None = None
    symbol: str = "frxEURUSD"
    days: float = Field(90, gt=0, le=3650)
    start: int | None = None
    end: int | None = None
    mode: Literal["code", "jev", "laya", "ensemble", "compare", "compare_all"] = "code"
    oos_fraction: float = Field(0.3, ge=0.0, le=0.9)
    start_balance: float = Field(1000.0, gt=0)
    cost: float | None = Field(None, ge=0, description="Round-trip cost in price units; default per symbol")
    session_exit: bool = True
    overrides: dict | None = Field(
        None, description='What-if patch, e.g. {"execution": {"entry": "fvg_edge"}} or '
                          '{"nodes": {"killzone": {"params": {"zones": ["london", "ny_am"]}}}}')


@router.post("/run", summary="Start a backtest (background job). An empty body runs the legacy 3-scenario check.")
def run(body: dict | None = Body(default=None)):
    if not body:
        return legacy_backtest()  # the original Vue dashboard's button
    try:
        req = BacktestRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if req.mode in {"jev", "compare", "compare_all", "ensemble"}:
        from app.services.jev.client import JevClient
        if not JevClient().available and req.mode != "ensemble":
            raise HTTPException(412, "Jev modes need TYPESAFE_API_KEY. Use mode='code' or add the key.")
    if req.mode in {"laya", "compare_all"}:
        from app.services.laya_client import installed
        if not installed():
            raise HTTPException(412, "Laya is not installed (pip install laya).")
    end = req.end or int(time.time())
    start = req.start or int(end - req.days * 86400)
    params = engine.BacktestParams(strategy_id=req.strategy_id, version=req.version, symbol=req.symbol, start=start,
                                   end=end, mode=req.mode, oos_fraction=req.oos_fraction,
                                   start_balance=req.start_balance, cost=req.cost, session_exit=req.session_exit,
                                   extra={"overrides": req.overrides} if req.overrides else {})

    async def job_fn(job: jobs.Job) -> dict:
        result = await engine.run_and_store(params, progress=lambda p: job.update(**p))
        return {"id": result["id"], "headline": {m: r["summary"] for m, r in result["results"].items()}}
    return jobs.start("backtest", req.model_dump() | {"start": start, "end": end}, job_fn).to_dict()


@router.get("", summary="Stored backtest runs (newest first)")
def list_runs(limit: int = 50) -> dict:
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT id, created_at, finished_at, status, params_json, summary_json, error "
                             "FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        summary = db.loads(r.pop("summary_json"), {}) or {}
        r["params"] = db.loads(r.pop("params_json"), {})
        r["headline"] = {m: {k: res["summary"].get(k) for k in ("trades", "win_rate", "expectancy_r", "profit_factor",
                                                                "max_drawdown_r")}
                         for m, res in (summary.get("results") or {}).items()}
        out.append(r)
    return {"runs": out}


@router.get("/{run_id}", summary="Full result: summary, validation, breakdowns, node pass rates, trades, equity")
def get_run(run_id: str, include_trades: bool = True) -> dict:
    with db.connect() as conn:
        r = db.row(conn, "SELECT * FROM backtest_runs WHERE id = ?", (run_id,))
    if r is None:
        raise HTTPException(404, "Unknown backtest run.")
    result = {"id": r["id"], "status": r["status"], "created_at": r["created_at"], "finished_at": r["finished_at"],
              "error": r["error"], "params": db.loads(r["params_json"], {}), **(db.loads(r["summary_json"], {}) or {})}
    result["equity"] = db.loads(r["equity_json"], {})
    if include_trades:
        result["trades"] = db.loads(r["trades_json"], {})
    return result
