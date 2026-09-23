"""Metrics: one aggregated read per dashboard cluster (performance, execution, strategy, data, risk)."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict

from fastapi import APIRouter

from app import db
from app.services import engine, risk
from app.services.backtest import metrics as M
from app.services.deriv import history

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


def _closed_trades(mode: str | None) -> list[dict]:
    sql = "SELECT * FROM trades WHERE status = 'closed'"
    params: tuple = ()
    if mode:
        sql += " AND mode = ?"
        params = (mode,)
    with db.connect() as conn:
        rows = db.rows(conn, sql + " ORDER BY closed_at", params)
    for r in rows:
        r["meta"] = db.loads(r.pop("meta_json"), {})
    return rows


@router.get("/performance", summary="Expectancy, win rate, profit factor, drawdown and equity per mode")
def performance(mode: str | None = None, start_balance: float = 1000.0, risk_pct: float = 1.0) -> dict:
    out = {}
    modes = [mode] if mode else ["paper", "demo", "real"]
    for m in modes:
        rows = _closed_trades(m)
        trades = [{"r": float(r["r_multiple"] or 0), "entry_time": r["opened_at"], "exit_time": r["closed_at"]}
                  for r in rows]
        out[m] = {"summary": M.summarise([t["r"] for t in trades]),
                  "equity": M.equity_curve(trades, start_balance, risk_pct),
                  "pnl": round(sum(float(r["pnl"] or 0) for r in rows), 2)}
    return {"modes": out}


@router.get("/execution", summary="Deriv round-trip, Jev latency, entry slippage vs plan")
def execution() -> dict:
    status = engine.status()
    with db.connect() as conn:
        jev = db.row(conn, "SELECT COUNT(*) AS calls, AVG(NULLIF(latency_ms, 0)) AS avg_ms, "
                           "MAX(latency_ms) AS max_ms FROM jev_cache")
        rows = db.rows(conn, "SELECT entry_price, stop_price, meta_json FROM trades WHERE entry_price IS NOT NULL")
    slips = []
    for r in rows:
        meta = db.loads(r["meta_json"], {})
        planned, stop = meta.get("planned_entry"), r["stop_price"]
        if planned and stop and planned != stop:
            slips.append(abs(r["entry_price"] - planned) / abs(planned - stop))
    return {"deriv_feed_rtt_ms": status.get("feed_rtt_ms"),
            "broker": status.get("broker"),
            "jev": {"calls": jev["calls"], "avg_latency_ms": round(jev["avg_ms"], 1) if jev["avg_ms"] else None,
                    "max_latency_ms": jev["max_ms"]},
            "entry_slippage_r": {"fills": len(slips), "avg": round(sum(slips) / len(slips), 4) if slips else None}}


@router.get("/strategy", summary="Live signal funnel: node pass rates, killzones, accept/reject counts")
def strategy(limit: int = 1000) -> dict:
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT status, decision_json FROM signals ORDER BY created_at DESC LIMIT ?", (limit,))
    nodes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    killzones: Counter = Counter()
    statuses: Counter = Counter()
    for r in rows:
        d = db.loads(r["decision_json"], {})
        statuses[r["status"]] += 1
        killzones[(d.get("setup") or {}).get("killzone") or "outside"] += 1
        for n in d.get("nodes") or []:
            nodes[n["id"]][0] += 1
            nodes[n["id"]][1] += int(bool(n.get("passed")))
    return {"signals": dict(statuses), "killzones": dict(killzones),
            "nodes": [{"id": k, "evaluated": v[0], "passed": v[1], "pass_rate": round(v[1] / v[0], 3) if v[0] else None}
                      for k, v in nodes.items()]}


@router.get("/data", summary="Data health: candle coverage, transcripts, Jev tags, calendar events")
async def data_health() -> dict:
    series = await asyncio.to_thread(history.coverage)
    with db.connect() as conn:
        tr = db.row(conn, "SELECT COUNT(*) AS videos, SUM(status='ok') AS ok, SUM(status IN ('failed','blocked')) AS "
                          "problems, COALESCE(SUM(segment_count),0) AS segments FROM videos")
        tags = db.row(conn, "SELECT COUNT(*) AS windows, COUNT(DISTINCT video_id) AS videos FROM segment_tags")
        econ = db.row(conn, "SELECT COUNT(*) AS events, MIN(ts) AS first, MAX(ts) AS last FROM econ_events")
    return {"candles": [{k: s[k] for k in ("symbol", "granularity", "bars", "first", "last")} for s in series],
            "transcripts": tr, "jev_tags": tags, "calendar": econ}


@router.get("/risk", summary="Today's realised P&L and usage against limits")
def risk_metrics() -> dict:
    return {"today": risk.today_stats(), "limits": risk.limits(), "kill_switch": risk.kill_switch_active(),
            "real_trading": risk.real_trading_status()}


@router.get("/overview", summary="Everything the Metrics page needs in one call")
async def overview() -> dict:
    with db.connect() as conn:
        last_bt = db.row(conn, "SELECT id, created_at, summary_json FROM backtest_runs WHERE status='done' "
                               "ORDER BY created_at DESC LIMIT 1")
    headline = None
    if last_bt:
        s = db.loads(last_bt["summary_json"], {})
        headline = {"id": last_bt["id"], "created_at": last_bt["created_at"], "symbol": s.get("symbol"),
                    "results": {m: {k: r["summary"].get(k) for k in ("trades", "expectancy_r", "win_rate")}
                                for m, r in (s.get("results") or {}).items()}}
    return {"performance": performance(), "execution": execution(), "strategy": strategy(),
            "data": await data_health(), "risk": risk_metrics(), "last_backtest": headline,
            "engine": {"running": engine.status()["running"]}}
