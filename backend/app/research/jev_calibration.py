"""Does Jev's judgment carry information? A calibration study, not a strategy.

For every structurally valid setup inside the killzone (raid → shift → displacement → FVG,
plan tradeable), simulate the outcome with the strategy's execution rules while IGNORING
all other filters, and ask Jev the strategy's Jev questions on the same code-computed facts.
Then compare expectancy for setups Jev liked against setups it did not. The code
fallback rules are scored the same way, so Jev and code are compared on equal footing.

    python -m app.research.jev_calibration --symbols frxEURUSD frxGBPUSD OTC_NDX --days 365
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from statistics import mean

from app.services.backtest import engine, metrics
from app.services.ict import strategy as S

RF = {"execution": {"contract": {"type": "rise_fall", "rise_fall_minutes": 15}}}


def _outcome(ev: S.Evaluation, bars, schema: S.StrategySchema, cost: float) -> float | None:
    s, plan = ev.setup, ev.plan
    if schema.execution.contract.type == "rise_fall":
        t = engine._rise_fall(s, plan, bars, schema.execution, cost)
    else:
        t = engine._multiplier(s, plan, bars, schema.execution, cost, None)
    return None if not t or "missed" in t else t["r"]


def _split(rows: list[dict], key: str, threshold: float) -> dict:
    liked = [r["r"] for r in rows if r.get(key) is not None and r[key] >= threshold]
    other = [r["r"] for r in rows if r.get(key) is not None and r[key] < threshold]
    return {"liked": metrics.summarise(liked, bootstrap=1000), "not_liked": metrics.summarise(other, bootstrap=1000),
            "edge_r": round(mean(liked) - mean(other), 4) if liked and other else None}


async def study(symbol: str, days: int, version: int, overrides: dict | None) -> dict:
    end = int(time.time())
    schema = engine.apply_overrides(S.get("ict_2022_model", version), overrides)
    bars, scanner, ctx = engine.prepare(schema, symbol, end - days * 86400, end)
    setups = engine._dedupe(scanner.run())
    cost = engine.cost_for(symbol)
    code_rt = S.Runtime(schema, "code", cost=cost)
    jev_rt = S.Runtime(schema, "jev", cost=cost)
    jev_nodes = [n for n in schema.nodes if n.kind == "jev"]
    candidates = []
    for s in setups:
        plan = S.build_plan(s, ctx, schema.execution)
        if plan is None:
            continue
        close = bars.c[s.armed_at]
        if not (plan.target > close if s.direction == "long" else plan.target < close):
            continue
        kz = S.DETECTORS["killzone"](s, ctx, plan, next(n for n in schema.nodes if n.id == "killzone").params)
        if not kz.passed:
            continue
        ev = S.Evaluation(s, plan)
        r = _outcome(ev, bars, schema, cost)
        if r is None:
            continue
        row = {"r": r, "direction": s.direction}
        for n in jev_nodes:  # code fallback scores on the same setups
            if n.fallback:
                res = S.DETECTORS[n.fallback](s, ctx, plan, n.params | n.gate | {"_cost": cost})
                row[f"code:{n.id}"] = 1.0 if res.passed else 0.0
        candidates.append((row, S.setup_facts(s, ctx, plan, schema.entry_granularity)))
    sem = asyncio.Semaphore(8)
    questions = code_rt.jev_questions(jev_nodes)
    async with jev_rt.jev.async_sdk() as sdk:
        async def ask(row: dict, facts: dict) -> None:
            async with sem:
                res = await jev_rt.jev.aask(facts, questions, sdk=sdk)
            for n in jev_nodes:
                a = res.answers[n.id]
                if a["type"] == "noul":
                    row[f"jev:{n.id}"] = a["noul"]
                elif a["type"] == "score":
                    row[f"jev:{n.id}"] = a["score_norm"]
                else:
                    want = n.gate.get("expect", {}).get(row["direction"])
                    row[f"jev:{n.id}"] = a["probabilities"].get(want, 0.0) if want else a["confidence"]
        await asyncio.gather(*(ask(r, f) for r, f in candidates))
    rows = [r for r, _ in candidates]
    out = {"symbol": symbol, "contract": schema.execution.contract.type, "setups_scored": len(rows),
           "baseline": metrics.summarise([r["r"] for r in rows], bootstrap=1000), "judgments": {}}
    for n in jev_nodes:
        gate = n.gate.get("min", n.gate.get("min_probability", 0.5))
        out["judgments"][n.id] = {"jev": _split(rows, f"jev:{n.id}", gate)}
        if n.fallback:
            out["judgments"][n.id]["code_fallback"] = _split(rows, f"code:{n.id}", 0.5)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=["frxEURUSD", "frxGBPUSD", "OTC_NDX"])
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--version", type=int, default=3)
    a = p.parse_args()
    for sym in a.symbols:
        ov = RF if sym.startswith("OTC_") else None
        res = asyncio.run(study(sym, a.days, a.version, ov))
        print(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
