# Rule variants and today's replay (2026-09-28)

**Question:** would the system have been profitable, today and over the past year, on the markets it
can actually trade on Deriv (EURUSD, GBPUSD, gold)?

**Short answer:** no version tested shows a proven edge. The v3 rules almost never trade; loosening
them produces enough trades to measure, and those lose money after Deriv's costs.

## Today (Monday 2026-09-28, New York window 13:00 to 16:00 SAST)

`python -m app.research.day_replay` (code judging) and `--mode jev`:

| Market | Setups | Traded (code) | Traded (Jev) | In-window setups taken regardless of rules |
|---|---|---|---|---|
| EURUSD | 7 | 0 | 0 | +0.47R (1 setup) |
| GBPUSD | 7 | 0 | 0 | +0.30R (1 filled, 1 ran to target unfilled) |
| Gold | 6 | 0 | 1: long 14:05, stopped, **−1.08R** | −4.12R (5 setups, 1 winner) |

With code judging the rules kept the account flat on a day when gold's setups mostly failed; with Jev
judging, one gold trade lost 1.08R. One day is anecdote, not evidence.

## One year, five rule variants (`python -m app.research.rule_variants`)

Code judging, real Deriv costs, one trade a day maximum. "OOS" is the final 30% of trades, which no
variant was tuned on. Expectancy is per trade in R (1R = the amount risked).

| Market | Variant | Trades | Win rate | Expectancy (95% range) | Total | OOS trades / expectancy |
|---|---|---|---|---|---|---|
| EURUSD | v3 as-is | 0 | | | | |
| EURUSD | A: fixed 2R target | 3 | 67% | +0.83R (too few to measure) | +2.5R | 1 / +1.80R |
| EURUSD | B: liquidity target, min 1.5R | 0 | | | | |
| EURUSD | C: fixed 2R, no premium/discount | 24 | 33% | −0.26R (−0.65 to +0.18) | −6.2R | 8 / +0.04R |
| EURUSD | D: fixed 1.5R, no premium/discount | 24 | 33% | −0.29R (−0.67 to +0.09) | −7.1R | 8 / −0.03R |
| GBPUSD | v3 as-is | 0 | | | | |
| GBPUSD | A | 2 | 50% | +0.28R (too few) | +0.6R | 1 / −1.23R |
| GBPUSD | B | 0 | | | | |
| GBPUSD | C | 28 | 25% | −0.36R (−0.71 to +0.05) | −10.2R | 9 / +0.18R |
| GBPUSD | D | 27 | 30% | −0.31R (−0.67 to +0.06) | −8.4R | 9 / +0.08R |
| Gold | v3 as-is | 1 | 0% | −1.04R | −1.0R | 1 / −1.04R |
| Gold | A | 6 | 33% | −0.08R (−1.07 to +0.96) | −0.5R | 2 / +0.46R |
| Gold | B | 4 | 50% | +0.31R (too few) | +1.3R | 2 / +0.36R |
| Gold | C | 36 | 36% | −0.09R (−0.49 to +0.38) | −3.2R | 11 / −0.43R |
| Gold | D | 34 | 41% | −0.06R (−0.43 to +0.32) | −2.0R | 11 / −0.46R |

## Reading it

1. **The strict variants (v3, A, B) trade 0 to 6 times a year.** Their small positive numbers come
   from a handful of trades and mean nothing statistically.
2. **The loose variants (C, D) trade 24 to 36 times a year and lose** 0.06R to 0.36R per trade over
   the year. Every 95% range includes zero, so they are not proven losers either, but none is close
   to proven profitable, and the out-of-sample slices disagree with each other.
3. **Premium/discount is the rule doing the most work:** removing it multiplies the trade count by
   about ten, and the extra trades are net losers. The ICT idea of entering in discount may be right,
   but combined with a 2R target it leaves almost nothing to trade on these markets.
4. **Testing many variants and picking the best one is itself a trap.** With five variants × three
   markets, one of them looking good by chance is expected. Any candidate has to be re-confirmed on
   fresh data (forward paper/demo trading) before it counts.

## Implications

- Nothing here supports trading real money with this strategy yet.
- The fastest honest source of new evidence is **forward testing**: run the engine every weekday on
  paper or demo and replay each day. 50+ forward trades per variant are needed to say anything.
- A different class of edge (for example ATJ Research's cross-venue arbitrage) rests on price
  differences rather than chart patterns. It needs accounts on several venues, fast execution, and
  enough capital for fees to be a small fraction, so it is not something a R100 Deriv account can run.
