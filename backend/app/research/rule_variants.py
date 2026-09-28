"""What-if study: loosen the v3 filters one step at a time and score each variant out of sample.

    python -m app.research.rule_variants frxEURUSD frxGBPUSD frxXAUUSD

One JSON line per (symbol, variant): trades, win rate, expectancy with 95% range, profit factor,
drawdown, and the same for the last 30% of the year (out of sample). Results for 2026-09-28 are in
docs/research/2026-09-28-rule-variants.md.
"""
import asyncio
import json
import sys
import time

from app.services.backtest import engine
from app.services.deriv import history

VARIANTS = {
    "v3 as-is": None,
    "A fixed 2R target": {"execution": {"target": {"mode": "fixed_r", "r": 2.0}}},
    "B liquidity target, min 1.5R": {"nodes": {"min_rr": {"params": {"min_rr": 1.5}}},
                                     "execution": {"target": {"min_rr": 1.5}}},
    "C fixed 2R, no premium/discount": {"execution": {"target": {"mode": "fixed_r", "r": 2.0}},
                                        "nodes": {"premium_discount": {"params": {"max_depth": 1.0}}}},
    "D fixed 1.5R, no premium/discount": {"execution": {"target": {"mode": "fixed_r", "r": 1.5}},
                                          "nodes": {"premium_discount": {"params": {"max_depth": 1.0}},
                                                    "min_rr": {"params": {"min_rr": 1.5}}}},
}
SYMBOLS = sys.argv[1:] or ["frxEURUSD", "frxGBPUSD", "frxXAUUSD"]


async def main():
    end = int(time.time())
    start = end - 365 * 86400
    for sym in SYMBOLS:
        await history.backfill(sym, 60, start, end)
        for name, ov in VARIANTS.items():
            p = engine.BacktestParams(strategy_id="ict_2022_model", version=None, symbol=sym, start=start, end=end,
                                      mode="code", oos_fraction=0.3, start_balance=1000.0, cost=None,
                                      session_exit=True, extra={"overrides": ov} if ov else {})
            r = (await engine.run(p))["results"]["code"]
            s, v = r["summary"], r["validation"]
            oos = v.get("out_of_sample") or {}
            print(json.dumps({"symbol": sym, "variant": name, "trades": s.get("trades"),
                              "win_rate": s.get("win_rate"), "exp_r": s.get("expectancy_r"),
                              "ci": s.get("expectancy_ci95"), "total_r": s.get("total_r"),
                              "pf": s.get("profit_factor"), "max_dd_r": s.get("max_drawdown_r"),
                              "oos_trades": oos.get("trades"), "oos_exp_r": oos.get("expectancy_r"),
                              "oos_ci": oos.get("expectancy_ci95")}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
