"""Replay one trading day: which setups the system saw, which it would have traded, and how they ended.

    python -m app.research.day_replay                       # today (New York trading day), forex + gold
    python -m app.research.day_replay --date 2026-09-25 --symbols frxEURUSD frxGBPUSD --mode jev

Causal like the live engine: each decision uses only bars closed at that moment. Earlier days are
loaded for context (previous-day and session levels, swings, ATR). For every setup it reports the
plan, every rule that failed, the trade the engine would actually have taken (one position at a
time, daily cap), and what each setup would have made had it been taken anyway, so a filter that
blocks winners is visible. Money uses the risk per trade given (1R).
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import sys
import time
from datetime import datetime, timedelta

from app.services.backtest import engine
from app.services.deriv import history
from app.services.ict import features as F
from app.services.ict import strategy as S

DEFAULT_SYMBOLS = ["frxEURUSD", "frxGBPUSD", "frxXAUUSD"]
SAST = timedelta(hours=2)  # South Africa: UTC+2 all year


def _clock(epoch: int) -> str:
    ny = F.ny_time(epoch).strftime("%H:%M")
    return f"{time.strftime('%H:%M', time.gmtime(epoch + SAST.total_seconds()))} SAST ({ny} NY)"


def _today_ny() -> str:
    return F.ny_trading_day(int(time.time()))


async def replay(day: str, symbols: list[str], mode: str, risk: float, context_days: int, version: int | None) -> dict:
    day_start = int(datetime.strptime(day, "%Y-%m-%d").timestamp()) - 86400  # generous: NY day straddles UTC days
    end = min(int(time.time()), day_start + 3 * 86400)
    start = day_start - context_days * 86400
    schema = S.get("ict_2022_model", version)
    report = {"day": day, "mode": mode, "risk_per_trade": risk, "strategy": f"{schema.id} v{schema.version}",
              "symbols": {}}
    for symbol in symbols:
        await history.backfill(symbol, 60, start, end)
        bars, scanner, ctx = engine.prepare(schema, symbol, start, end)
        today = [s for s in engine._dedupe(scanner.run()) if F.ny_trading_day(bars.t[s.armed_at]) == day]
        cost = engine.cost_for(symbol)
        runtime = S.Runtime(schema, mode, cost=cost)
        evals = await engine.evaluate_all(runtime, today, ctx)
        funnel: dict = {}
        taken = engine.simulate(evals, bars, schema, cost, session_exit=True, funnel=funnel, symbol=symbol)
        taken_at = {t["entry_time"] for t in taken}
        rows = []
        for ev in evals:
            s, plan = ev.setup, ev.plan
            row = {"armed": _clock(bars.t[s.armed_at]), "direction": s.direction, "raided": s.sweep_level_name,
                   "passed": ev.passed, "failed_rules": [r.id for r in ev.results if not r.passed]}
            if plan:
                row.update(entry=plan.entry, stop=plan.stop, target=plan.target, rr=round(plan.rr, 2),
                           target_name=plan.target_name)
                anyway = engine._multiplier(s, plan, bars, schema.execution, cost, None)
                if anyway is None:
                    row["if_taken"] = "entry never reached"
                elif "missed" in anyway:
                    row["if_taken"] = "ran to target without filling"
                else:
                    row["if_taken"] = {"r": anyway["r"], "exit": anyway["exit_reason"],
                                       "filled": _clock(calendar.timegm(time.strptime(anyway["entry_time"],
                                                                                      "%Y-%m-%dT%H:%M:%SZ"))),
                                       "money": round(anyway["r"] * risk, 2)}
                    row["traded"] = anyway["entry_time"] in taken_at and ev.passed
            rows.append(row)
        total_r = round(sum(t["r"] for t in taken), 3)
        report["symbols"][symbol] = {
            "setups": len(evals), "funnel": funnel, "cost_price": cost,
            "trades": [{k: t[k] for k in ("side", "entry_time", "exit_time", "entry", "exit", "stop", "target", "r",
                                          "exit_reason")} | {"money": round(t["r"] * risk, 2)} for t in taken],
            "total_r": total_r, "total_money": round(total_r * risk, 2), "rows": rows}
    all_r = [t["r"] for v in report["symbols"].values() for t in v["trades"]]
    report["total"] = {"trades": len(all_r), "r": round(sum(all_r), 3), "money": round(sum(all_r) * risk, 2),
                       "wins": sum(1 for r in all_r if r > 0)}
    return report


def to_markdown(rep: dict) -> str:
    out = [f"# Day replay {rep['day']} ({rep['strategy']}, {rep['mode']} judges, 1R = {rep['risk_per_trade']})", ""]
    t = rep["total"]
    out += [f"**Result:** {t['trades']} trade(s), {t['wins']} win(s), {t['r']:+.2f}R = {t['money']:+.2f}", "",
            "Rules are checked in order and a setup stops at the first one it fails (\"rejected at\"). "
            "\"If taken anyway\" ignores every rule, to show what the filters kept you out of.", ""]
    for sym, v in rep["symbols"].items():
        out += [f"## {sym}: {v['setups']} setup(s), {len(v['trades'])} traded, {v['total_r']:+.2f}R", ""]
        if not v["rows"]:
            out += ["No setups formed.", ""]
            continue
        out += ["| Armed | Dir | Raided | Plan (entry / stop / target, R:R) | Verdict | If taken anyway |",
                "|---|---|---|---|---|---|"]
        for r in v["rows"]:
            plan = (f"{r['entry']:.5f} / {r['stop']:.5f} / {r['target']:.5f}, {r['rr']}R" if "entry" in r
                    else "no valid plan")
            verdict = ("TRADED" if r.get("traded") else "passed (not filled / position open)" if r["passed"]
                       else "rejected at " + ", ".join(r["failed_rules"]))
            anyway = r.get("if_taken", "")
            if isinstance(anyway, dict):
                anyway = f"{anyway['r']:+.2f}R ({anyway['exit']}, filled {anyway['filled']})"
            out.append(f"| {r['armed']} | {r['direction']} | {r['raided']} | {plan} | {verdict} | {anyway} |")
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=None, help="New York trading day YYYY-MM-DD (default: today)")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--mode", default="code", choices=["code", "jev", "laya", "ensemble"])
    ap.add_argument("--risk", type=float, default=1.0, help="money per 1R (default 1.0)")
    ap.add_argument("--context-days", type=int, default=30)
    ap.add_argument("--version", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    a = ap.parse_args()
    rep = asyncio.run(replay(a.date or _today_ny(), a.symbols, a.mode, a.risk, a.context_days, a.version))
    sys.stdout.write((json.dumps(rep, indent=1, default=str) if a.json else to_markdown(rep)) + "\n")


if __name__ == "__main__":
    main()
