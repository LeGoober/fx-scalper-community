"""Market data: Deriv symbols and candle backfill/coverage, plus the OpenBB feed."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app import jobs
from app.services.deriv import history
from app.services.deriv.client import DerivClient, DerivError

router = APIRouter(prefix="/api/market", tags=["market data"])

# Instruments where ICT's institutional-liquidity premise applies. Deriv synthetic
# indices (Volatility, Boom/Crash, ...) are RNG-driven and deliberately excluded.
ICT_DEFAULT_SYMBOLS = ["frxEURUSD", "frxGBPUSD", "frxXAUUSD", "OTC_NDX", "OTC_SPC"]


class BackfillRequest(BaseModel):
    symbol: str = Field(examples=["frxEURUSD"])
    granularity: int = Field(60, description="Seconds per candle; one of " + ", ".join(map(str, history.GRANULARITIES)))
    days: float | None = Field(30, gt=0, le=3650, description="Look-back from `end` (ignored if `start` given)")
    start: int | None = Field(None, description="Epoch seconds")
    end: int | None = Field(None, description="Epoch seconds; default now")


@router.get("/symbols", summary="Deriv active symbols, flagged for ICT suitability")
async def symbols(market: str | None = None) -> dict:
    try:
        async with DerivClient(token="") as client:
            items = await client.active_symbols()
    except DerivError as exc:
        raise HTTPException(502, str(exc)) from exc
    out = []
    for s in items:
        if market and s.get("market") != market:
            continue
        synthetic = s.get("market") == "synthetic_index"
        out.append({"symbol": s.get("symbol"), "name": s.get("display_name"), "market": s.get("market"),
                    "submarket": s.get("submarket"), "open": bool(s.get("exchange_is_open")),
                    "pip_size": s.get("pip_size"), "ict_suitable": not synthetic,
                    "ict_default": s.get("symbol") in ICT_DEFAULT_SYMBOLS})
    return {"symbols": out, "ict_defaults": ICT_DEFAULT_SYMBOLS}


@router.get("/contracts/{symbol}", summary="Contract types available for a symbol (multipliers, rise/fall, ...)")
async def contracts(symbol: str) -> dict:
    try:
        async with DerivClient(token="") as client:
            data = await client.contracts_for(symbol)
    except DerivError as exc:
        raise HTTPException(502, str(exc)) from exc
    available = data.get("available", [])
    return {"symbol": symbol,
            "contract_types": sorted({a.get("contract_type") for a in available}),
            "multipliers": sorted({m for a in available if a.get("contract_category") == "multiplier"
                                   for m in a.get("multiplier_range") or []})}


@router.post("/backfill", summary="Start a background job pulling Deriv candles into the local DB")
def backfill(body: BackfillRequest) -> dict:
    try:
        history.validate_granularity(body.granularity)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    end = body.end or int(time.time())
    start = body.start or int(end - (body.days or 30) * 86400)

    async def run(job: jobs.Job) -> dict:
        return await history.backfill(body.symbol, body.granularity, start, end,
                                      progress=lambda p: job.update(**p))

    job = jobs.start("backfill", {"symbol": body.symbol, "granularity": body.granularity,
                                  "start": start, "end": end}, run)
    return job.to_dict()


@router.get("/coverage", summary="What candle history is stored locally (bars, range, largest gaps)")
async def coverage() -> dict:
    return {"series": await asyncio.to_thread(history.coverage)}


@router.get("/candles/{symbol}", summary="Stored candles (optionally resampled) for charts")
async def candles(symbol: str, granularity: int = 60, start: int | None = None, end: int | None = None,
                  resample: int | None = Query(None, description="Aggregate to this many seconds"),
                  limit: int = Query(5000, le=50000)) -> dict:
    rows = await asyncio.to_thread(history.load, symbol, granularity, start, end)
    if resample and resample > granularity:
        rows = history.resample(rows, resample)
    return {"symbol": symbol, "granularity": resample or granularity, "candles": rows[-limit:]}


# ------------------------------------------------------------------ OpenBB
@router.get("/openbb/status", summary="Is the OpenBB data layer installed and which providers are keyed")
async def openbb_status() -> dict:
    from app.services.data import openbb_feed
    return await asyncio.to_thread(openbb_feed.status)


@router.get("/openbb/htf/{pair}", summary="Daily/weekly candles for higher-timeframe bias (e.g. EURUSD)")
async def openbb_htf(pair: str, interval: str = "1d", days: int = 365, provider: str = "yfinance") -> dict:
    from app.services.data import openbb_feed
    try:
        return await asyncio.to_thread(openbb_feed.htf_candles, pair, interval, days, provider)
    except openbb_feed.OpenBBUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"OpenBB error: {exc}") from exc


@router.get("/openbb/calendar", summary="Economic calendar (high-impact news filter)")
async def openbb_calendar(days_ahead: int = 7, days_back: int = 1, provider: str | None = None,
                          min_importance: int = 3) -> dict:
    from app.services.data import openbb_feed
    try:
        return await asyncio.to_thread(openbb_feed.calendar, days_back, days_ahead, provider, min_importance)
    except openbb_feed.OpenBBUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"OpenBB error: {exc}") from exc


@router.get("/openbb/news", summary="Market news headlines")
async def openbb_news(query: str = "forex", limit: int = 20, provider: str | None = None) -> dict:
    from app.services.data import openbb_feed
    try:
        return await asyncio.to_thread(openbb_feed.news, query, limit, provider)
    except openbb_feed.OpenBBUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"OpenBB error: {exc}") from exc


@router.get("/openbb/crosscheck/{symbol}", summary="Compare Deriv daily closes against OpenBB (data quality)")
async def openbb_crosscheck(symbol: str, days: int = 60) -> dict:
    from app.services.data import openbb_feed
    try:
        return await asyncio.to_thread(openbb_feed.crosscheck, symbol, days)
    except openbb_feed.OpenBBUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
