"""Trade statistics in R. Same expectancy maths as the Bootcamp's code/expectancy.py, extended with
drawdown, profit factor and a bootstrap confidence interval (is the edge real or luck?)."""
from __future__ import annotations

import math
import random
from statistics import mean, pstdev


def summarise(r_values: list[float], *, bootstrap: int = 2000, seed: int = 7) -> dict:
    n = len(r_values)
    if n == 0:
        return {"trades": 0}
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r <= 0]
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0
    payoff = avg_win / avg_loss if avg_loss else math.inf
    expectancy = mean(r_values)
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    equity, peak, max_dd = 0.0, 0.0, 0.0
    streak = worst_streak = 0
    for r in r_values:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        streak = streak + 1 if r <= 0 else 0
        worst_streak = max(worst_streak, streak)
    sd = pstdev(r_values) if n > 1 else 0.0
    out = {
        "trades": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_win_r": round(avg_win, 3),
        "avg_loss_r": round(avg_loss, 3),
        "payoff": round(payoff, 3) if math.isfinite(payoff) else None,
        "expectancy_r": round(expectancy, 4),
        "breakeven_win_rate": round(1 / (1 + payoff), 4) if math.isfinite(payoff) and payoff > 0 else None,
        "total_r": round(sum(r_values), 3),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown_r": round(max_dd, 3),
        "max_consecutive_losses": worst_streak,
        "sharpe_per_trade": round(expectancy / sd, 3) if sd else None,
        "t_stat": round(expectancy / (sd / math.sqrt(n)), 3) if sd and n > 1 else None,
    }
    if bootstrap and n >= 5:
        rng = random.Random(seed)
        means = sorted(mean(rng.choices(r_values, k=n)) for _ in range(bootstrap))
        out["expectancy_ci95"] = [round(means[int(0.025 * bootstrap)], 4), round(means[int(0.975 * bootstrap)], 4)]
        out["p_expectancy_le_0"] = round(sum(1 for m in means if m <= 0) / bootstrap, 4)
    return out


def equity_curve(trades: list[dict], start_balance: float, risk_pct: float) -> list[dict]:
    """Cumulative R and a fixed-fractional balance (risk_pct of equity per trade)."""
    balance, cum_r = start_balance, 0.0
    points = [{"t": trades[0]["entry_time"] if trades else None, "r": 0.0, "balance": round(balance, 2)}]
    for t in trades:
        cum_r += t["r"]
        balance *= 1 + risk_pct / 100 * t["r"]
        points.append({"t": t["exit_time"], "r": round(cum_r, 3), "balance": round(balance, 2)})
    return points


def split(trades: list[dict], oos_fraction: float, folds: int = 4) -> dict:
    """In-sample vs out-of-sample (by time) and walk-forward folds. The strategy is not
    fitted per fold, so this measures stability: an edge that only exists in one slice is fragile."""
    if not trades:
        return {}
    cut = int(len(trades) * (1 - oos_fraction))
    result = {"in_sample": summarise([t["r"] for t in trades[:cut]], bootstrap=0),
              "out_of_sample": summarise([t["r"] for t in trades[cut:]], bootstrap=500),
              "oos_starts": trades[cut]["entry_time"] if cut < len(trades) else None, "folds": []}
    size = max(1, len(trades) // folds)
    for k in range(folds):
        chunk = trades[k * size:(k + 1) * size] if k < folds - 1 else trades[k * size:]
        if chunk:
            s = summarise([t["r"] for t in chunk], bootstrap=0)
            result["folds"].append({"fold": k + 1, "from": chunk[0]["entry_time"], "to": chunk[-1]["exit_time"],
                                    "trades": s["trades"], "expectancy_r": s.get("expectancy_r"),
                                    "win_rate": s.get("win_rate")})
    return result


def breakdown(trades: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[float]] = {}
    for t in trades:
        groups.setdefault(str(t.get(key)), []).append(t["r"])
    return [{key: k, **summarise(v, bootstrap=0)} for k, v in sorted(groups.items())]
