"""Structural trigger scanner for the ICT 2022 model, run bar by bar (causal).

Sequence (long side; short is the mirror):
  1. liquidity raid: price trades below sell-side liquidity (a confirmed, unswept
     swing low, the previous day low, or the completed Asian-session low);
  2. market structure shift: within `mss_window` bars, a close above the most
     recent confirmed swing high that preceded the raid;
  3. displacement: the leg from the raid contains a candle whose body is at least
     `displacement_min_body_atr` × ATR;
  4. fair value gap: a bullish FVG formed inside that leg (at most `fvg_grace`
     bars after the MSS). The setup is armed at the FVG's close.

Everything else (killzones, premium/discount, bias, news, R:R, Jev judgments)
lives in the strategy schema's filter nodes, not here.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.ict.features import (FVG, Bars, SessionLevels, Setup, Swing, atr, body_ratio, fvg_at,
                                       session_levels, swings)


@dataclass
class ScanParams:
    swing_k: int = 2
    liquidity_lookback: int = 240       # bars a swing stays a liquidity pool
    mss_window: int = 30                # bars allowed between raid and MSS
    displacement_min_body_atr: float = 1.2
    fvg_min_atr: float = 0.05           # ignore hairline gaps
    fvg_grace: int = 2                  # bars after MSS in which the FVG may still form
    use_session_levels: bool = True
    atr_period: int = 14


@dataclass
class _Raid:
    direction: str
    index: int
    level: float
    level_name: str
    extreme: float
    ref: Swing           # swing to break for the MSS
    mss_index: int | None = None


class Scanner:
    def __init__(self, bars: Bars, params: ScanParams | None = None) -> None:
        self.bars = bars
        self.p = params or ScanParams()
        self.atr = atr(bars, self.p.atr_period)
        self.swings = swings(bars, self.p.swing_k)
        self.levels: list[SessionLevels] = session_levels(bars) if self.p.use_session_levels else []

    def run(self) -> list[Setup]:
        b, p = self.bars, self.p
        setups: list[Setup] = []
        known_highs: list[Swing] = []
        known_lows: list[Swing] = []
        swept: set[tuple[str, float]] = set()
        raids: list[_Raid] = []
        sw_ptr = 0
        for i in range(len(b)):
            while sw_ptr < len(self.swings) and self.swings[sw_ptr].confirmed_at <= i:
                s = self.swings[sw_ptr]
                (known_highs if s.kind == "high" else known_lows).append(s)
                sw_ptr += 1
            known_highs = [s for s in known_highs if i - s.index <= p.liquidity_lookback]
            known_lows = [s for s in known_lows if i - s.index <= p.liquidity_lookback]

            for raid in list(raids):
                done = self._advance(raid, i, setups)
                if done or i - raid.index > p.mss_window + p.fvg_grace:
                    raids.remove(raid)

            for direction, pools, ref_pool in (("long", self._sell_side(i, known_lows), known_highs),
                                               ("short", self._buy_side(i, known_highs), known_lows)):
                for name, level in pools:
                    key = (name if name.startswith(("pd", "asian")) else f"{direction}-swing", level)
                    if key in swept:
                        continue
                    hit = b.l[i] < level if direction == "long" else b.h[i] > level
                    if not hit:
                        continue
                    swept.add(key)
                    ref = next((s for s in reversed(ref_pool) if s.index < i), None)
                    if ref is None:
                        continue
                    raids.append(_Raid(direction, i, level, name, b.l[i] if direction == "long" else b.h[i], ref))
        return setups

    # ------------------------------------------------------------ helpers
    def _sell_side(self, i: int, lows: list[Swing]) -> list[tuple[str, float]]:
        pools = [("swing_low", s.price) for s in lows]
        if self.levels:
            lv = self.levels[i]
            pools += [(n, v) for n, v in (("pdl", lv.pdl), ("asian_low", lv.asian_low)) if v is not None]
        return pools

    def _buy_side(self, i: int, highs: list[Swing]) -> list[tuple[str, float]]:
        pools = [("swing_high", s.price) for s in highs]
        if self.levels:
            lv = self.levels[i]
            pools += [(n, v) for n, v in (("pdh", lv.pdh), ("asian_high", lv.asian_high)) if v is not None]
        return pools

    def _advance(self, raid: _Raid, i: int, setups: list[Setup]) -> bool:
        b, p = self.bars, self.p
        long = raid.direction == "long"
        if raid.mss_index is None:
            raid.extreme = min(raid.extreme, b.l[i]) if long else max(raid.extreme, b.h[i])
            if i - raid.index > p.mss_window:
                return True
            broke = b.c[i] > raid.ref.price if long else b.c[i] < raid.ref.price
            if not broke:
                return False
            raid.mss_index = i
        if i - raid.mss_index > p.fvg_grace:
            return True
        fvg = self._find_fvg(raid, i)
        if fvg is None:
            return False
        leg = range(raid.index, i + 1)
        bodies = [body_ratio(b, j, self.atr) for j in leg]
        strongest = max(bodies) if bodies else 0.0
        if strongest < p.displacement_min_body_atr:
            return True  # shift happened without displacement: not the model
        leg_high = max(b.h[j] for j in leg)
        leg_low = min(b.l[j] for j in leg)
        setups.append(Setup(
            direction=raid.direction, sweep_index=raid.index, sweep_level=raid.level, sweep_level_name=raid.level_name,
            sweep_extreme=raid.extreme, mss_index=raid.mss_index, mss_level=raid.ref.price, fvg=fvg,
            leg_high=leg_high, leg_low=leg_low, displacement_body_atr=round(strongest, 3),
            displacement_bars=sum(1 for x in bodies if x >= 0.8), armed_at=i,
            facts={"atr": self.atr[i], "levels": self.levels[i] if self.levels else None}))
        return True

    def _find_fvg(self, raid: _Raid, i: int) -> FVG | None:
        """First gap created by the displacement that breaks structure (third candle closed, index ≤ i).

        ICT (2022 Mentorship Ep. 16 @22:37): "the first fair value gap here… because we have
        this short term low broken with the run lower". The gap's middle candle must be at or
        after the first bar that traded through the broken swing; gaps left at the base of the
        move, before the break, are not the model's entry."""
        b = self.bars
        want = "bullish" if raid.direction == "long" else "bearish"
        min_size = self.p.fvg_min_atr * self.atr[i]
        long = raid.direction == "long"
        break_bar = next((j for j in range(raid.index + 1, i + 1)
                          if (b.h[j] > raid.ref.price if long else b.l[j] < raid.ref.price)), raid.mss_index)
        for m in range(max(raid.index + 2, break_bar + 1), i + 1):
            gap = fvg_at(self.bars, m, min_size)
            if gap and gap.direction == want:
                return gap
        return None
