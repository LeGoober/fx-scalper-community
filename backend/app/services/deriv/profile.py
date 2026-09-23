"""Per-symbol Deriv contract profile, calibrated from live public quotes (no token needed).

Why: what Deriv advertises (`contracts_for`) and what it accepts (`proposal`) differ.
For example, frxEURUSD lists x800 but accepts only x100, and the OTC indices offer
no multipliers at all (Rise/Fall options only). Backtests use the measured
commission as their cost, and the engine picks only multipliers Deriv accepts.
"""
from __future__ import annotations

import re

from app import db
from app.services.deriv.client import DerivClient, DerivError

KEY = "deriv_contract_profile"
QUOTE_STAKE = 10.0


async def _multiplier_quote(client: DerivClient, symbol: str, multiplier: int) -> tuple[dict | None, list[int]]:
    try:
        p = await client.proposal(amount=QUOTE_STAKE, basis="stake", contract_type="MULTUP", currency="USD",
                                  multiplier=multiplier, underlying_symbol=symbol)
        return p, []
    except DerivError as exc:
        found = re.search(r"Accepts ([\d,\s]+)", str(exc))
        if found:
            return None, [int(x) for x in found.group(1).replace(" ", "").split(",") if x]
        raise


async def calibrate(symbols: list[str], rise_fall_minutes: int = 15) -> dict:
    profiles = db.kv_get(KEY) or {}
    async with DerivClient(token="") as client:
        for symbol in symbols:
            prof: dict = {"symbol": symbol, "calibrated_at": db.utc_now()}
            try:
                cf = await client.contracts_for(symbol)
            except DerivError as exc:
                profiles[symbol] = {**prof, "error": str(exc)}
                continue
            avail = cf.get("available", [])
            prof["categories"] = sorted({a.get("contract_category") for a in avail})
            advertised = sorted({m for a in avail if a.get("contract_type") == "MULTUP"
                                 for m in a.get("multiplier_range") or []})
            prof["multipliers_advertised"] = advertised
            if advertised:
                accepted: list[int] = []
                quote, hint = await _multiplier_quote(client, symbol, advertised[0])
                if quote is None and hint:
                    accepted = hint
                    quote, _ = await _multiplier_quote(client, symbol, accepted[0])
                else:
                    accepted = [advertised[0]]
                    for m in advertised[1:]:
                        q, hint = await _multiplier_quote(client, symbol, m)
                        if q is not None:
                            accepted.append(m)
                        elif hint:
                            accepted = sorted(set(accepted) | set(hint))
                            break
                # Commission has a floor, so quote a realistic notional (x100 when accepted), not the smallest.
                ref = 100 if 100 in accepted else accepted[len(accepted) // 2] if accepted else None
                if ref is not None:
                    quote, _ = await _multiplier_quote(client, symbol, ref)
                if quote:
                    spot, commission = float(quote["spot"]), float(quote["commission"])
                    notional = QUOTE_STAKE * ref
                    prof.update({"multipliers": accepted, "quoted_multiplier": ref,
                                 "commission_rate": round(commission / notional, 8),
                                 "cost_price": round(commission / notional * spot, 8), "spot": spot})
            try:
                rf = await client.proposal(amount=QUOTE_STAKE, basis="stake", contract_type="CALL", currency="USD",
                                           duration=rise_fall_minutes, duration_unit="m", underlying_symbol=symbol)
                prof["rise_fall"] = {"minutes": rise_fall_minutes,
                                     "payout_r": round(float(rf["payout"]) / float(rf["ask_price"]) - 1, 4)}
                prof["rise_fall"]["breakeven_win_rate"] = round(1 / (1 + prof["rise_fall"]["payout_r"]), 4)
            except DerivError as exc:
                prof["rise_fall"] = {"error": str(exc)}
            prof["executable"] = {"multiplier": bool(prof.get("multipliers")),
                                  "rise_fall": "payout_r" in prof.get("rise_fall", {})}
            profiles[symbol] = prof
    db.kv_set(KEY, profiles)
    return profiles


def get(symbol: str | None = None) -> dict:
    profiles = db.kv_get(KEY) or {}
    return profiles.get(symbol, {}) if symbol else profiles
