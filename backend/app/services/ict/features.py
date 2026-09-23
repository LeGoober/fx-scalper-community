"""Deterministic ICT detectors. Code computes every number; Jev never does arithmetic.

All detectors are causal. At bar i they use only bars ≤ i. A swing point at bar j
is confirmed only at bar j + k (its right-hand bars must have closed), and it
becomes visible from that bar on, never earlier. The no-look-ahead test in
tests/test_ict.py mutates future bars and asserts that past outputs are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


@dataclass
class Bars:
    t: list[int]
    o: list[float]
    h: list[float]
    l: list[float]  # noqa: E741  (OHLC naming)
    c: list[float]
    granularity: int = 60

    @classmethod
    def from_candles(cls, candles: list[dict], granularity: int = 60) -> "Bars":
        return cls([int(x["epoch"]) for x in candles], [float(x["open"]) for x in candles],
                   [float(x["high"]) for x in candles], [float(x["low"]) for x in candles],
                   [float(x["close"]) for x in candles], granularity)

    def __len__(self) -> int:
        return len(self.t)


# --------------------------------------------------------------------- time
KILLZONES_NY: dict[str, tuple[dtime, dtime]] = {
    "asian": (dtime(20, 0), dtime(0, 0)),
    "london": (dtime(2, 0), dtime(5, 0)),
    "ny_am": (dtime(7, 0), dtime(10, 0)),          # forex (Ep. 17 @10:22)
    "ny_am_index": (dtime(8, 30), dtime(11, 0)),   # index futures (Ep. 17 @24:23)
    "ny_lunch": (dtime(12, 0), dtime(13, 0)),
    "ny_pm": (dtime(13, 30), dtime(16, 0)),
    # Silver Bullet one-hour windows
    "sb_london": (dtime(3, 0), dtime(4, 0)),
    "sb_ny_am": (dtime(10, 0), dtime(11, 0)),
    "sb_ny_pm": (dtime(14, 0), dtime(15, 0)),
}


def ny_time(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(NY)


def in_window(epoch: int, window: tuple[dtime, dtime]) -> bool:
    now = ny_time(epoch).time()
    start, end = window
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # wraps midnight (Asian session)


def killzone_of(epoch: int, names: list[str] | None = None) -> str | None:
    for name, window in KILLZONES_NY.items():
        if (names is None or name in names) and in_window(epoch, window):
            return name
    return None


def ny_trading_day(epoch: int) -> str:
    """ICT trading day starts at 17:00 NY (FX rollover), so the Asian session belongs to the next day."""
    dt = ny_time(epoch)
    if dt.time() >= dtime(17, 0):
        dt = datetime.fromordinal(dt.toordinal() + 1).replace(tzinfo=NY)
    return dt.strftime("%Y-%m-%d")


# ------------------------------------------------------------------ volatility
def atr(bars: Bars, period: int = 14) -> list[float]:
    """Wilder ATR; value at i uses bars ≤ i."""
    out: list[float] = []
    prev = None
    for i in range(len(bars)):
        tr = bars.h[i] - bars.l[i] if i == 0 else max(bars.h[i] - bars.l[i], abs(bars.h[i] - bars.c[i - 1]),
                                                      abs(bars.l[i] - bars.c[i - 1]))
        prev = tr if prev is None else (prev * (period - 1) + tr) / period if i >= period else (
            (prev * i + tr) / (i + 1))
        out.append(prev)
    return out


# ------------------------------------------------------------------- swings
@dataclass(frozen=True)
class Swing:
    index: int         # bar of the swing extreme
    confirmed_at: int  # first bar at which the swing is known (index + k)
    price: float
    kind: str          # "high" | "low"


def swings(bars: Bars, k: int = 2) -> list[Swing]:
    """Fractal swings: an extreme strictly above/below k bars left and ≥/≤ k bars right."""
    out: list[Swing] = []
    n = len(bars)
    for j in range(k, n - k):
        hi, lo = bars.h[j], bars.l[j]
        if all(hi > bars.h[j - m] for m in range(1, k + 1)) and all(hi >= bars.h[j + m] for m in range(1, k + 1)):
            out.append(Swing(j, j + k, hi, "high"))
        if all(lo < bars.l[j - m] for m in range(1, k + 1)) and all(lo <= bars.l[j + m] for m in range(1, k + 1)):
            out.append(Swing(j, j + k, lo, "low"))
    return sorted(out, key=lambda s: (s.confirmed_at, s.index))


# ---------------------------------------------------------------------- FVG
@dataclass(frozen=True)
class FVG:
    index: int      # third candle; the gap is known at its close
    direction: str  # "bullish" | "bearish"
    top: float
    bottom: float

    @property
    def ce(self) -> float:  # consequent encroachment (50%)
        return (self.top + self.bottom) / 2


def fvg_at(bars: Bars, i: int, min_size: float = 0.0) -> FVG | None:
    """Three-candle gap ending at bar i (candles i-2, i-1, i)."""
    if i < 2:
        return None
    if bars.l[i] > bars.h[i - 2] and bars.l[i] - bars.h[i - 2] > min_size:
        return FVG(i, "bullish", top=bars.l[i], bottom=bars.h[i - 2])
    if bars.h[i] < bars.l[i - 2] and bars.l[i - 2] - bars.h[i] > min_size:
        return FVG(i, "bearish", top=bars.l[i - 2], bottom=bars.h[i])
    return None


def body_ratio(bars: Bars, i: int, atr_values: list[float]) -> float:
    a = atr_values[i - 1] if i > 0 else atr_values[i]
    return abs(bars.c[i] - bars.o[i]) / a if a else 0.0


# -------------------------------------------------------------- session levels
@dataclass
class SessionLevels:
    """Liquidity levels known at a given bar: previous day and Asian-session extremes."""
    pdh: float | None = None
    pdl: float | None = None
    pd_close: float | None = None
    pd_open: float | None = None
    asian_high: float | None = None
    asian_low: float | None = None
    day: str | None = None
    midnight_open: float | None = None  # NY 00:00 opening price, known from midnight until the next 17:00 roll


PIP_SIZE = {"frxUSDJPY": 0.01, "frxEURJPY": 0.01, "frxGBPJPY": 0.01, "frxAUDJPY": 0.01, "frxNZDJPY": 0.01,
            "frxXAUUSD": 0.1}


def pip_size(symbol: str) -> float:
    """Forex pip (0.0001, or 0.01 for JPY pairs); gold 0.1; OTC indices 1 point."""
    if symbol in PIP_SIZE:
        return PIP_SIZE[symbol]
    return 1.0 if symbol.startswith("OTC_") else 0.0001


def session_levels(bars: Bars) -> list[SessionLevels]:
    """Per bar, the levels that are complete by that bar (previous NY trading day; Asian range once it ends)."""
    out: list[SessionLevels] = []
    day = None
    cur = {"h": None, "l": None, "o": None, "c": None}
    prev = {"h": None, "l": None, "o": None, "c": None}
    asian = {"h": None, "l": None}
    asian_done = {"h": None, "l": None}
    asian_window = KILLZONES_NY["asian"]
    midnight_open: float | None = None
    for i in range(len(bars)):
        d = ny_trading_day(bars.t[i])
        if d != day:
            if day is not None:
                prev = dict(cur)
            day = d
            cur = {"h": bars.h[i], "l": bars.l[i], "o": bars.o[i], "c": bars.c[i]}
            asian = {"h": None, "l": None}
            asian_done = {"h": None, "l": None}
            midnight_open = None  # the new trading day starts at 17:00; its midnight has not happened yet
        if midnight_open is None and ny_time(bars.t[i]).hour < 17 and ny_time(bars.t[i]).strftime("%Y-%m-%d") == d:
            midnight_open = bars.o[i]  # first bar at/after 00:00 NY of this trading day
        else:
            cur["h"], cur["l"], cur["c"] = max(cur["h"], bars.h[i]), min(cur["l"], bars.l[i]), bars.c[i]
        if in_window(bars.t[i], asian_window):
            asian["h"] = bars.h[i] if asian["h"] is None else max(asian["h"], bars.h[i])
            asian["l"] = bars.l[i] if asian["l"] is None else min(asian["l"], bars.l[i])
        elif asian["h"] is not None and asian_done["h"] is None:
            asian_done = dict(asian)  # Asian range becomes a known level only after the session closes
        out.append(SessionLevels(prev["h"], prev["l"], prev["c"], prev["o"], asian_done["h"], asian_done["l"], d,
                                 midnight_open))
    return out


# ------------------------------------------------------------------- HTF bias
def today_extremes(bars: Bars, i: int) -> tuple[float, float]:
    """High and low of the current NY trading day up to and including bar i (causal)."""
    day = ny_trading_day(bars.t[i])
    hi, lo = bars.h[i], bars.l[i]
    j = i - 1
    while j >= 0 and ny_trading_day(bars.t[j]) == day:
        hi, lo = max(hi, bars.h[j]), min(lo, bars.l[j])
        j -= 1
    return hi, lo


def daily_bias_facts(levels: SessionLevels, price: float, today: tuple[float, float] | None = None) -> dict:
    """Descriptive (non-numeric) facts for the HTF-bias judgment; also used by the code-mode fallback."""
    if levels.pdh is None or levels.pdl is None:
        return {"known": False}
    mid = (levels.pdh + levels.pdl) / 2
    prev_bullish = levels.pd_close is not None and levels.pd_open is not None and levels.pd_close > levels.pd_open
    range_ = levels.pdh - levels.pdl or 1e-12
    taken = {}
    if today:
        taken = {"previous_day_high_already_traded_today": "yes" if today[0] > levels.pdh else "no",
                 "previous_day_low_already_traded_today": "yes" if today[1] < levels.pdl else "no"}
    return {
        **taken,
        "known": True,
        "previous_day_closed": "bullish (close above open)" if prev_bullish else "bearish (close below open)",
        "price_vs_previous_day_range": ("above the previous day high" if price > levels.pdh else
                                        "below the previous day low" if price < levels.pdl else
                                        "in the upper half (premium) of the previous day range" if price > mid else
                                        "in the lower half (discount) of the previous day range"),
        "closer_to": "previous day high" if levels.pdh - price < price - levels.pdl else "previous day low",
        "distance_to_pdh_in_ranges": round((levels.pdh - price) / range_, 2),
        "distance_to_pdl_in_ranges": round((price - levels.pdl) / range_, 2),
    }


@dataclass
class Setup:
    """A candidate produced by the structural trigger sequence (sweep → MSS/displacement → FVG)."""
    direction: str            # "long" | "short"
    sweep_index: int
    sweep_level: float
    sweep_level_name: str
    sweep_extreme: float      # lowest low (long) / highest high (short) of the raid
    mss_index: int
    mss_level: float          # swing broken to confirm the shift
    fvg: FVG
    leg_high: float
    leg_low: float
    displacement_body_atr: float
    displacement_bars: int
    armed_at: int             # bar at which the setup is known (FVG close)
    facts: dict = field(default_factory=dict)
