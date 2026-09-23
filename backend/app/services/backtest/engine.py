"""Event-driven backtest of a strategy schema on stored Deriv candles.

Pipeline: candles → (resample) → Scanner (structural trigger) → Runtime (code
and/or Jev filter nodes) → trade simulation → metrics.

Fill model (conservative by design):
  - limit entry at the planned price, valid for `entry_window_bars` after the setup
    is armed; a gap through the price fills at the bar open (never better than the open);
  - the order is cancelled if the target trades before the fill;
  - on the fill bar only the stop is checked; if a later bar touches both stop and
    target, the stop wins (the worst case);
  - optional session exit (e.g. 16:00 NY) at that bar's close;
  - one position per symbol, `max_trades_per_day` per NY trading day;
  - round-trip cost per symbol (spread + commission estimate) deducted in R.
Jev answers are cached, so re-running a backtest is free and reproducible.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Literal

from app import db, events
from app.services.backtest import metrics
from app.services.deriv import history
from app.services.ict import features as F
from app.services.ict import strategy as S
from app.services.ict.scanner import Scanner

# Round-trip cost fallback in price units, measured from Deriv multiplier quotes (2 bps of notional,
# 2026-09-23). POST /api/market/calibrate refreshes the live profile, which takes precedence.
DEFAULT_COST = {"frxEURUSD": 0.00023, "frxGBPUSD": 0.00027, "frxUSDJPY": 0.03, "frxXAUUSD": 0.77,
                "OTC_NDX": 6.0, "OTC_SPC": 1.3, "OTC_DJI": 9.0}


def cost_for(symbol: str) -> float:
    from app.services.deriv import profile
    measured = profile.get(symbol).get("cost_price")
    return float(measured) if measured else DEFAULT_COST.get(symbol, 0.0)


JEV_CONCURRENCY = 8


@dataclass
class BacktestParams:
    strategy_id: str
    symbol: str
    start: int
    end: int
    version: int | None = None
    mode: Literal["code", "jev", "compare"] = "code"
    oos_fraction: float = 0.3
    start_balance: float = 1000.0
    cost: float | None = None
    session_exit: bool = True
    extra: dict = field(default_factory=dict)


def _news_lookup(start: int, end: int, currencies: list[str], min_importance: int) -> list[dict]:
    from app.services.data.openbb_feed import events_between
    return events_between(start, end, min_importance, currencies or None)


def prepare(schema: S.StrategySchema, symbol: str, start: int, end: int) -> tuple[F.Bars, Scanner, S.Context]:
    candles = history.load(symbol, 60, start, end)
    if not candles:
        raise ValueError(f"No stored 1-minute candles for {symbol} in range. "
                         "Backfill first (POST /api/market/backfill).")
    if schema.entry_granularity > 60:
        candles = history.resample(candles, schema.entry_granularity)
    bars = F.Bars.from_candles(candles, schema.entry_granularity)
    scanner = Scanner(bars, schema.scan_params())
    ctx = S.Context(symbol, bars, scanner.swings, scanner.levels, scanner.atr, _news_lookup)
    return bars, scanner, ctx


SESSION_LEVELS = ("pdh", "pdl", "asian_high", "asian_low")


def _dedupe(setups: list[F.Setup]) -> list[F.Setup]:
    """One setup per structure break. When several levels were raided into the same shift,
    keep the session level (previous day / Asian) over a plain swing, then the earliest armed."""
    best: dict[tuple[str, int], F.Setup] = {}
    for s in setups:
        key = (s.direction, s.mss_index)
        rank = (0 if s.sweep_level_name in SESSION_LEVELS else 1, s.armed_at, s.sweep_index)
        cur = best.get(key)
        if cur is None or rank < (0 if cur.sweep_level_name in SESSION_LEVELS else 1, cur.armed_at, cur.sweep_index):
            best[key] = s
    return sorted(best.values(), key=lambda x: (x.armed_at, x.sweep_index))


async def evaluate_all(runtime: S.Runtime, setups: list[F.Setup], ctx: S.Context,
                       progress: Callable[[dict], None] | None = None) -> list[S.Evaluation]:
    staged = [runtime.precheck(s, ctx) for s in setups]
    need = [(i, ev, pending) for i, (ev, pending) in enumerate(staged) if pending and ev.plan is not None]
    out = [ev for ev, _ in staged]
    if not need:
        return out
    sem = asyncio.Semaphore(JEV_CONCURRENCY)
    done = 0
    async with runtime.jev.async_sdk() as sdk:
        async def one(i: int, ev: S.Evaluation, pending: list[S.NodeSpec]) -> None:
            nonlocal done
            state = S.setup_facts(ev.setup, ctx, ev.plan, runtime.schema.entry_granularity)
            async with sem:
                try:
                    result = await runtime.jev.aask(state, runtime.jev_questions(pending), sdk=sdk)
                    answers = result.answers
                except Exception as exc:  # recorded per setup, not fatal to the run
                    answers = {}
                    ev.flags.append(f"jev_error:{type(exc).__name__}")
            out[i] = runtime.finalize(ev, pending, answers)
            done += 1
            if progress and done % 10 == 0:
                progress({"jev_evaluated": done, "jev_total": len(need)})
        await asyncio.gather(*(one(i, ev, p) for i, ev, p in need))
    return out


FOREX_ZONES = ["london", "ny_am", "ny_lunch", "ny_pm", "asian"]
INDEX_ZONES = ["ny_am_index", "london", "ny_lunch", "ny_pm", "asian"]


def simulate(evals: list[S.Evaluation], bars: F.Bars, schema: S.StrategySchema, cost: float,
             session_exit: bool, funnel: dict | None = None, symbol: str = "") -> list[dict]:
    ex = schema.execution
    trades: list[dict] = []
    busy_until = -1
    per_day: dict[str, int] = {}
    funnel = funnel if funnel is not None else {}
    for key in ("passed_filters", "skipped_position_open", "skipped_daily_cap", "not_filled", "missed_target_first",
                "filled"):
        funnel.setdefault(key, 0)
    exit_hhmm = tuple(int(x) for x in ex.session_exit_ny.split(":")) if (session_exit and ex.session_exit_ny) else None
    for ev in sorted((e for e in evals if e.passed), key=lambda e: e.setup.armed_at):
        s, plan = ev.setup, ev.plan
        funnel["passed_filters"] += 1
        if s.armed_at <= busy_until:
            funnel["skipped_position_open"] += 1
            continue
        day = F.ny_trading_day(bars.t[s.armed_at])
        if per_day.get(day, 0) >= ex.max_trades_per_day:
            funnel["skipped_daily_cap"] += 1
            continue
        long = s.direction == "long"
        if ex.contract.type == "rise_fall":
            trade = _rise_fall(s, plan, bars, ex, cost)
        else:
            trade = _multiplier(s, plan, bars, ex, cost, exit_hhmm)
        if trade is None or "missed" in trade:
            funnel["missed_target_first" if trade else "not_filled"] += 1
            continue
        funnel["filled"] += 1
        per_day[day] = per_day.get(day, 0) + 1
        busy_until = trade.pop("_exit_index")
        zone_names = INDEX_ZONES if symbol.startswith("OTC_") else FOREX_ZONES
        trade.update({"direction": s.direction, "killzone": F.killzone_of(bars.t[s.mss_index], zone_names) or "outside",
                      "liquidity": s.sweep_level_name, "rr_planned": plan.rr, "target_name": plan.target_name,
                      "flags": ev.flags, "nodes": {r.id: r.value for r in ev.results},
                      "side": "BUY" if long else "SELL"})
        trades.append(trade)
    return trades


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _multiplier(s: F.Setup, plan: S.Plan, bars: F.Bars, ex: S.ExecutionSpec, cost: float,
                exit_hhmm: tuple[int, int] | None) -> dict | None:
    long = s.direction == "long"
    n = len(bars)
    fill_i = fill_px = None
    for j in range(s.armed_at + 1, min(n, s.armed_at + 1 + ex.entry_window_bars)):
        if (long and bars.h[j] >= plan.target) or (not long and bars.l[j] <= plan.target):
            if not ((long and bars.l[j] <= plan.entry) or (not long and bars.h[j] >= plan.entry)):
                return {"missed": True}  # ran to target without us
        if long and bars.l[j] <= plan.entry:
            fill_i, fill_px = j, min(plan.entry, bars.o[j])
            break
        if not long and bars.h[j] >= plan.entry:
            fill_i, fill_px = j, max(plan.entry, bars.o[j])
            break
    if fill_i is None:
        return None
    exit_i, exit_px, reason = None, None, None
    exit_at = None
    if exit_hhmm:
        fill_dt = F.ny_time(bars.t[fill_i])
        exit_dt = fill_dt.replace(hour=exit_hhmm[0], minute=exit_hhmm[1], second=0, microsecond=0)
        if fill_dt >= exit_dt:
            exit_dt += timedelta(days=1)  # opened after the cut-off: close at the next session's cut-off
        exit_at = exit_dt.timestamp()
    for k in range(fill_i, n):
        hit_stop = bars.l[k] <= plan.stop if long else bars.h[k] >= plan.stop
        hit_target = (bars.h[k] >= plan.target if long else bars.l[k] <= plan.target) and k > fill_i
        if hit_stop:
            gap = bars.o[k] < plan.stop if long else bars.o[k] > plan.stop
            exit_i, exit_px, reason = k, (bars.o[k] if gap and k > fill_i else plan.stop), "stop"
            break
        if hit_target:
            exit_i, exit_px, reason = k, plan.target, "target"
            break
        if exit_at is not None and k > fill_i and bars.t[k] >= exit_at:
            exit_i, exit_px, reason = k, bars.c[k], "session_exit"
            break
    if exit_i is None:
        exit_i, exit_px, reason = n - 1, bars.c[n - 1], "end_of_data"
    risk = abs(fill_px - plan.stop)
    gross = (exit_px - fill_px) / risk if long else (fill_px - exit_px) / risk
    r = gross - cost / risk
    return {"entry_time": _iso(bars.t[fill_i]), "exit_time": _iso(bars.t[exit_i]), "entry": round(fill_px, 6),
            "exit": round(exit_px, 6), "stop": plan.stop, "target": plan.target, "r": round(r, 4),
            "gross_r": round(gross, 4), "cost_r": round(cost / risk, 4), "exit_reason": reason,
            "bars_held": exit_i - fill_i, "_exit_index": exit_i}


def _rise_fall(s: F.Setup, plan: S.Plan, bars: F.Bars, ex: S.ExecutionSpec, cost: float) -> dict | None:
    """Fixed-payout contract bought at the armed bar close, settled after N minutes (R = stake)."""
    i = s.armed_at
    settle_t = bars.t[i] + ex.contract.rise_fall_minutes * 60
    k = next((j for j in range(i + 1, len(bars)) if bars.t[j] >= settle_t), None)
    if k is None:
        return None
    won = bars.c[k] > bars.c[i] if s.direction == "long" else bars.c[k] < bars.c[i]
    r = ex.contract.rise_fall_payout if won else -1.0
    return {"entry_time": _iso(bars.t[i]), "exit_time": _iso(bars.t[k]), "entry": bars.c[i], "exit": bars.c[k],
            "stop": None, "target": None, "r": r, "gross_r": r, "cost_r": 0.0, "exit_reason": "expiry",
            "bars_held": k - i, "_exit_index": k}


def node_stats(evals: list[S.Evaluation], schema: S.StrategySchema) -> list[dict]:
    stats = {n.id: {"id": n.id, "label": n.label, "kind": n.kind, "evaluated": 0, "passed": 0} for n in schema.nodes}
    for ev in evals:
        for r in ev.results:
            nid = r.id.removeprefix("flag:")
            if nid in stats:
                stats[nid]["evaluated"] += 1
                stats[nid]["passed"] += int(r.passed)
    for s in stats.values():
        s["pass_rate"] = round(s["passed"] / s["evaluated"], 3) if s["evaluated"] else None
    return list(stats.values())


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if key == "nodes" and isinstance(value, dict):  # {"node_id": {"params": {...}}} patches nodes by id
            out["nodes"] = [_deep_merge(n, value.get(n["id"], {})) for n in base["nodes"]]
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            out[key] = _deep_merge(base[key], value)
        else:
            out[key] = value
    return out


def apply_overrides(schema: S.StrategySchema, overrides: dict | None) -> S.StrategySchema:
    """What-if variants without editing the schema file, e.g. {"execution": {"entry": "fvg_edge"}}."""
    if not overrides:
        return schema
    return S.StrategySchema.model_validate(_deep_merge(schema.model_dump(), overrides))


async def run(params: BacktestParams, progress: Callable[[dict], None] | None = None) -> dict:
    started = time.perf_counter()
    schema = apply_overrides(S.get(params.strategy_id, params.version), params.extra.get("overrides"))
    bars, scanner, ctx = await asyncio.to_thread(prepare, schema, params.symbol, params.start, params.end)
    setups = _dedupe(await asyncio.to_thread(scanner.run))
    if progress:
        progress({"stage": "scanned", "bars": len(bars), "setups": len(setups)})
    cost = params.cost if params.cost is not None else cost_for(params.symbol)
    from app.services.deriv import profile
    prof = profile.get(params.symbol)
    contract = schema.execution.contract.type
    warnings = []
    if prof and not (prof.get("executable") or {}).get(contract, True):
        warnings.append(f"Deriv does not offer {contract} contracts on {params.symbol} (options account API); "
                        "results are hypothetical. Use a rise_fall variant to test what is executable.")
    if contract == "rise_fall" and (prof.get("rise_fall") or {}).get("payout_r"):
        schema.execution.contract.rise_fall_payout = prof["rise_fall"]["payout_r"]
    modes = ["code", "jev"] if params.mode == "compare" else [params.mode]
    results = {}
    for mode in modes:
        runtime = S.Runtime(schema, mode, cost=cost)
        evals = await evaluate_all(runtime, setups, ctx, progress)
        funnel: dict = {"setups": len(setups)}
        trades = simulate(evals, bars, schema, cost, params.session_exit, funnel, params.symbol)
        rs = [t["r"] for t in trades]
        results[mode] = {
            "summary": metrics.summarise(rs),
            "execution_funnel": funnel,
            "validation": metrics.split(trades, params.oos_fraction),
            "by_killzone": metrics.breakdown(trades, "killzone"),
            "by_direction": metrics.breakdown(trades, "direction"),
            "by_exit": metrics.breakdown(trades, "exit_reason"),
            "nodes": node_stats(evals, schema),
            "setups_passed": sum(1 for e in evals if e.passed),
            "trades": trades,
            "equity": metrics.equity_curve(trades, params.start_balance, schema.execution.risk_per_trade_pct),
        }
    return {"strategy": {"id": schema.id, "version": schema.version, "name": schema.name,
                         "overrides": params.extra.get("overrides")},
            "symbol": params.symbol, "granularity": schema.entry_granularity, "bars": len(bars),
            "from": _iso(bars.t[0]), "to": _iso(bars.t[-1]), "setups": len(setups), "cost_price": cost,
            "contract": schema.execution.contract.type, "warnings": warnings, "results": results,
            "elapsed_s": round(time.perf_counter() - started, 2)}


async def run_and_store(params: BacktestParams, progress: Callable[[dict], None] | None = None) -> dict:
    run_id = f"bt-{uuid.uuid4().hex[:10]}"
    p = {k: v for k, v in params.__dict__.items() if k != "extra"}
    with db.connect() as conn:
        conn.execute("INSERT INTO backtest_runs(id, created_at, status, params_json) VALUES (?, ?, 'running', ?)",
                     (run_id, db.utc_now(), db.dumps(p)))
    try:
        result = await run(params, progress)
    except Exception as exc:
        with db.connect() as conn:
            conn.execute("UPDATE backtest_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                         (f"{type(exc).__name__}: {exc}", db.utc_now(), run_id))
        raise
    trades = {m: r.pop("trades") for m, r in result["results"].items()}
    equity = {m: r.pop("equity") for m, r in result["results"].items()}
    with db.connect() as conn:
        conn.execute("UPDATE backtest_runs SET status='done', summary_json=?, trades_json=?, equity_json=?, "
                     "finished_at=? WHERE id=?",
                     (db.dumps(result), db.dumps(trades), db.dumps(equity), db.utc_now(), run_id))
    headline = {m: r["summary"].get("expectancy_r") for m, r in result["results"].items()}
    events.publish("backtest.done", {"id": run_id, "symbol": params.symbol, "expectancy_r": headline},
                   message=f"Backtest {run_id} done")
    return {"id": run_id, **result}
