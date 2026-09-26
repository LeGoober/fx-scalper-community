"""Do the judges carry information? A calibration study, not a strategy.

For every structurally valid setup inside the killzone (raid → shift → displacement → FVG,
plan tradeable), simulate the outcome with the strategy's execution rules while IGNORING
all other filters. Then score the same code-computed facts with:
  - each judge (Jev, Laya), averaged over every question phrasing (question_variants);
  - the ensemble confidence JSON (weighted mean × agreement, execute/skip);
  - the code fallback rules.
Finally compare expectancy for setups each judgment liked against setups it did not.

    python -m app.research.jev_calibration --symbols frxEURUSD frxGBPUSD OTC_NDX --days 365
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from statistics import mean

from app.services.backtest import engine, metrics
from app.services.ict import ensemble
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


async def study(symbol: str, days: int, version: int, overrides: dict | None, judges: list[str]) -> dict:
    end = int(time.time())
    schema = engine.apply_overrides(S.get("ict_2022_model", version), overrides)
    bars, scanner, ctx = engine.prepare(schema, symbol, end - days * 86400, end)
    setups = engine._dedupe(scanner.run())
    cost = engine.cost_for(symbol)
    rt = S.Runtime(schema, "ensemble", cost=cost)
    clients = {"jev": rt.jev if "jev" in judges else None, "laya": rt.laya if "laya" in judges else None}
    jev_nodes = [n for n in schema.nodes if n.kind == "jev"]
    kz_node = next(n for n in schema.nodes if n.id == "killzone")
    cfg = schema.ensemble or {}
    candidates = []
    for s in setups:
        plan = S.build_plan(s, ctx, schema.execution)
        if plan is None:
            continue
        close = bars.c[s.armed_at]
        if not (plan.target > close if s.direction == "long" else plan.target < close):
            continue
        if not S.DETECTORS["killzone"](s, ctx, plan, kz_node.params).passed:
            continue
        r = _outcome(S.Evaluation(s, plan), bars, schema, cost)
        if r is None:
            continue
        row = {"r": r, "direction": s.direction}
        for n in jev_nodes:
            if n.fallback:
                res = S.DETECTORS[n.fallback](s, ctx, plan, n.params | n.gate | {"_cost": cost})
                row[f"code:{n.id}"] = 1.0 if res.passed else 0.0
        candidates.append((row, S.setup_facts(s, ctx, plan, schema.entry_granularity)))

    if clients["laya"] is not None and candidates:
        # Pre-score every phrasing with Laya in batches (CPU-bound; one batched pass per schema),
        # so the ensemble below reads Laya answers from the cache instead of 1-by-1 inference.
        for qs in ensemble.schemas_for(jev_nodes):
            await asyncio.to_thread(clients["laya"].ask_batch, [f for _, f in candidates], qs)
    sem = asyncio.Semaphore(8)

    async def score(row: dict, facts: dict) -> None:
        async with sem:
            rep = await ensemble.validate(facts, jev_nodes, row["direction"], clients,
                                          weights=cfg.get("weights"), threshold=float(cfg.get("threshold", 0.6)))
        for nid, nr in rep["nodes"].items():
            for judge, v in (nr.get("by_judge") or {}).items():
                row[f"{judge}:{nid}"] = v
            row[f"ensemble:{nid}"] = nr.get("mean")
        row["ensemble:confidence"] = rep["confidence"]
        row["ensemble:execute"] = 1.0 if rep["decision"] == "execute" else 0.0

    await asyncio.gather(*(score(r, f) for r, f in candidates))
    rows = [r for r, _ in candidates]
    out = {"symbol": symbol, "contract": schema.execution.contract.type, "setups_scored": len(rows),
           "judges": [j for j, c in clients.items() if c is not None],
           "baseline": metrics.summarise([r["r"] for r in rows], bootstrap=1000), "judgments": {}}
    for n in jev_nodes:
        gate = ensemble.gate_of(n)
        res = {who: _split(rows, f"{who}:{n.id}", gate) for who in (*out["judges"], "ensemble")}
        if n.fallback:
            res["code_fallback"] = _split(rows, f"code:{n.id}", 0.5)
        out["judgments"][n.id] = res
    out["judgments"]["ENSEMBLE_DECISION"] = {"ensemble": _split(rows, "ensemble:execute", 0.5)}
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=["frxEURUSD", "frxGBPUSD", "OTC_NDX"])
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--version", type=int, default=3)
    p.add_argument("--judges", nargs="+", default=["jev", "laya"], choices=["jev", "laya"])
    a = p.parse_args()
    for sym in a.symbols:
        ov = RF if sym.startswith("OTC_") else None
        print(json.dumps(asyncio.run(study(sym, a.days, a.version, ov, a.judges)), default=str), flush=True)


if __name__ == "__main__":
    main()
