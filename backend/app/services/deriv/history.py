"""Historical candles: Deriv backfill → SQLite, plus loading and resampling helpers.

Deriv returns at most 1000 candles per `ticks_history` call, and paging with
`end=` stalls at market-closed gaps (weekends). So backfill walks fixed
[start, end] windows of 1000 bars; empty windows simply mean the market was shut.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from app import db
from app.services.deriv.client import DerivClient, DerivError

GRANULARITIES = (60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400)
MAX_PER_REQUEST = 1000
CONCURRENCY = 4

Progress = Callable[[dict], Awaitable[None] | None]


def validate_granularity(granularity: int) -> int:
    if int(granularity) not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {GRANULARITIES}")
    return int(granularity)


def upsert(symbol: str, granularity: int, candles: list[dict], source: str = "deriv") -> int:
    if not candles:
        return 0
    params = [
        (symbol, granularity, int(c["epoch"]), float(c["open"]), float(c["high"]),
         float(c["low"]), float(c["close"]), source)
        for c in candles
    ]
    with db.connect() as conn, db.tx(conn):
        conn.executemany(
            "INSERT INTO candles(symbol, granularity, epoch, open, high, low, close, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, granularity, epoch) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close",
            params,
        )
    return len(params)  # rows upserted (inserted or refreshed)


def windows(start: int, end: int, granularity: int) -> list[tuple[int, int]]:
    span = granularity * MAX_PER_REQUEST
    out: list[tuple[int, int]] = []
    cursor = start - start % granularity
    while cursor <= end:
        out.append((cursor, min(cursor + span - granularity, end)))
        cursor += span
    return out


async def backfill(symbol: str, granularity: int, start: int, end: int | None = None, *,
                   client: Optional[DerivClient] = None, progress: Progress | None = None) -> dict:
    """Fetch [start, end] (epoch seconds) into the candles table. Public data: no token needed."""
    granularity = validate_granularity(granularity)
    end = int(end or time.time())
    if start >= end:
        raise ValueError("start must be before end")
    plan = windows(int(start), end, granularity)
    own_client = client is None
    client = client or DerivClient(token="")
    if own_client:
        await client.connect(authorize=False)
    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {"symbol": symbol, "granularity": granularity, "windows": len(plan), "done": 0,
             "fetched": 0, "written": 0, "errors": []}

    async def one(window: tuple[int, int]) -> None:
        async with sem:
            for attempt in range(3):
                try:
                    candles = await client.ticks_history_candles(
                        symbol, granularity, start=window[0], end=window[1], count=MAX_PER_REQUEST)
                    break
                except DerivError as exc:
                    if attempt == 2:
                        stats["errors"].append(f"{window[0]}-{window[1]}: {exc}")
                        candles = []
                    else:
                        await asyncio.sleep(1.5 * (attempt + 1))
            # adjust_start_time can shift into the previous window; keep only this window's bars
            candles = [c for c in candles if window[0] <= int(c["epoch"]) <= window[1]]
            stats["fetched"] += len(candles)
            stats["written"] += await asyncio.to_thread(upsert, symbol, granularity, candles)
            stats["done"] += 1
            if progress:
                result = progress(dict(stats, errors=len(stats["errors"])))
                if asyncio.iscoroutine(result):
                    await result

    try:
        await asyncio.gather(*(one(w) for w in plan))
    finally:
        if own_client:
            await client.close()
    return stats


def load(symbol: str, granularity: int, start: int | None = None, end: int | None = None) -> list[dict]:
    sql = "SELECT epoch, open, high, low, close FROM candles WHERE symbol = ? AND granularity = ?"
    params: list = [symbol, granularity]
    if start is not None:
        sql += " AND epoch >= ?"
        params.append(int(start))
    if end is not None:
        sql += " AND epoch <= ?"
        params.append(int(end))
    with db.connect() as conn:
        return db.rows(conn, sql + " ORDER BY epoch", tuple(params))


def resample(candles: list[dict], granularity: int) -> list[dict]:
    """Aggregate ascending candles into `granularity`-second buckets (aligned to epoch 0 / UTC)."""
    out: list[dict] = []
    current: dict | None = None
    for c in candles:
        bucket = int(c["epoch"]) - int(c["epoch"]) % granularity
        if current is None or current["epoch"] != bucket:
            if current is not None:
                out.append(current)
            current = {"epoch": bucket, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
        else:
            current["high"] = max(current["high"], c["high"])
            current["low"] = min(current["low"], c["low"])
            current["close"] = c["close"]
    if current is not None:
        out.append(current)
    return out


def coverage() -> list[dict]:
    """Per symbol/granularity: bar count, range and the largest gaps (weekends included; the UI labels them)."""
    with db.connect() as conn:
        series = db.rows(conn, "SELECT symbol, granularity, COUNT(*) AS bars, MIN(epoch) AS first, "
                               "MAX(epoch) AS last FROM candles GROUP BY symbol, granularity "
                               "ORDER BY symbol, granularity")
        for item in series:
            gaps = db.rows(conn, """
                SELECT prev AS from_epoch, epoch AS to_epoch, epoch - prev AS seconds FROM (
                  SELECT epoch, LAG(epoch) OVER (ORDER BY epoch) AS prev FROM candles
                  WHERE symbol = ? AND granularity = ?)
                WHERE prev IS NOT NULL AND epoch - prev > ? ORDER BY seconds DESC LIMIT 5""",
                           (item["symbol"], item["granularity"], item["granularity"]))
            item["largest_gaps"] = gaps
    return series
