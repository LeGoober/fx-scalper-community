"""Does an agent earn its authority? Measure it on outcomes, not on how convincing it sounds.

For every proposal the agents reviewed, simulate what the trade WOULD have done (counterfactual R,
from the stored 1-minute candles, conservative: a bar touching both stop and target counts as a stop).
Then for each role compare the setups it would have vetoed with the ones it let through:

    veto value = mean R (let through) − mean R (would veto)

Positive = its vetoes remove worse-than-average trades. The bootstrap interval says whether that is
distinguishable from luck; with a handful of vetoes it won't be, and the role should stay at 'log'.
Costs are ignored here (they apply equally to both groups, so they cancel in the difference).
"""
from __future__ import annotations

import random
import time
from datetime import datetime
from statistics import mean

from app import db
from app.services.deriv import history

HOLD_S = 24 * 3600


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def counterfactual_r(p: dict) -> float | None:
    """R the proposal would have made, or None if it could not have filled / data is missing."""
    start = _epoch(p["created_at"])
    expires = int(p.get("expires_at") or start + 3600)
    end = min(int(time.time()), start + HOLD_S)
    bars = history.load(p["symbol"], 60, start, end)
    if not bars:
        return None
    long, stop, target, entry = p["direction"] == "long", float(p["stop"]), p.get("target"), p.get("entry")
    i, fill = 0, None
    if p.get("entry_type") == "market" or entry is None:
        fill = float(bars[0]["open"])
    else:
        entry = float(entry)
        for i, b in enumerate(bars):
            if b["epoch"] > expires:
                return None
            if target is not None and ((long and b["high"] >= target) or (not long and b["low"] <= target)) and \
                    not ((long and b["low"] <= entry) or (not long and b["high"] >= entry)):
                return None  # ran to target without filling
            if (long and b["low"] <= entry) or (not long and b["high"] >= entry):
                fill = min(entry, b["open"]) if long else max(entry, b["open"])
                break
        if fill is None:
            return None
    risk = abs(fill - stop)
    if risk <= 0:
        return None
    for b in bars[i:]:
        if (long and b["low"] <= stop) or (not long and b["high"] >= stop):
            exit_px = min(stop, b["open"]) if long else max(stop, b["open"])
            return ((exit_px - fill) if long else (fill - exit_px)) / risk
        if target is not None and ((long and b["high"] >= target) or (not long and b["low"] <= target)):
            return ((target - fill) if long else (fill - target)) / risk
    last = float(bars[-1]["close"])
    return ((last - fill) if long else (fill - last)) / risk


def _boot_diff(a: list[float], b: list[float], n: int = 2000, seed: int = 7) -> list[float] | None:
    if len(a) < 3 or len(b) < 3:
        return None
    rng = random.Random(seed)
    diffs = sorted(mean(rng.choices(a, k=len(a))) - mean(rng.choices(b, k=len(b))) for _ in range(n))
    return [round(diffs[int(0.025 * n)], 4), round(diffs[int(0.975 * n)], 4)]


def report(role: str | None = None, limit: int = 2000) -> dict:
    sql = ("SELECT a.role, a.authority, a.ok, a.would_veto, p.* FROM agent_outputs a "
           "JOIN proposals p ON p.id = a.proposal_id")
    params: tuple = ()
    if role:
        sql += " WHERE a.role = ?"
        params = (role,)
    with db.connect() as conn:
        rows = db.rows(conn, sql + " ORDER BY a.created_at DESC LIMIT ?", params + (limit,))
    by_role: dict[str, dict] = {}
    cache: dict[str, float | None] = {}
    for r in rows:
        stats = by_role.setdefault(r["role"], {"reviewed": 0, "failed": 0, "kept": [], "vetoed": [], "pending": 0})
        stats["reviewed"] += 1
        if not r["ok"]:
            stats["failed"] += 1
            continue
        if r["id"] not in cache:
            cache[r["id"]] = counterfactual_r(r)
        cf = cache[r["id"]]
        if cf is None:
            stats["pending"] += 1
            continue
        stats["vetoed" if r["would_veto"] else "kept"].append(cf)
    out = {}
    for name, s in by_role.items():
        kept, vetoed = s["kept"], s["vetoed"]
        out[name] = {"reviewed": s["reviewed"], "failed": s["failed"], "no_outcome": s["pending"],
                     "kept": len(kept), "would_veto": len(vetoed),
                     "kept_mean_r": round(mean(kept), 4) if kept else None,
                     "vetoed_mean_r": round(mean(vetoed), 4) if vetoed else None,
                     "veto_value_r": round(mean(kept) - mean(vetoed), 4) if kept and vetoed else None,
                     "veto_value_ci95": _boot_diff(kept, vetoed),
                     "verdict": _verdict(kept, vetoed)}
    return {"roles": out}


def _verdict(kept: list[float], vetoed: list[float]) -> str:
    if len(vetoed) < 10 or len(kept) < 10:
        return "not enough reviewed trades with outcomes yet (need ≥10 each side): keep at 'log'"
    ci = _boot_diff(kept, vetoed)
    if ci and ci[0] > 0:
        return "vetoes remove worse trades beyond chance: veto authority is supported"
    if ci and ci[1] < 0:
        return "vetoes remove BETTER trades: keep at 'log' or turn off"
    return "no measurable difference yet: keep at 'log'"
