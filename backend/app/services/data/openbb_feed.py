"""OpenBB data layer: higher-timeframe candles, economic calendar, news, and a Deriv cross-check.

OpenBB is optional (backend/requirements-data.txt) and slow to import (~20 s on the
first run), so it is imported lazily and warmed in the background at startup.

Economic calendar: every OpenBB calendar provider (FMP, Trading Economics, FRED)
needs an API key, so the free ForexFactory weekly feed is the default for the live
news filter. Every fetched event is stored in `econ_events`, so backtests gain
history over time.
"""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app import db

FOREXFACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPORTANCE = {"low": 1, "medium": 2, "high": 3, "holiday": 0}
CREDENTIALS = {"fmp_api_key": "FMP_API_KEY", "fred_api_key": "FRED_API_KEY", "tiingo_token": "TIINGO_TOKEN"}

_obb: Any = None
_obb_lock = threading.Lock()
_import_error: str | None = None


class OpenBBUnavailable(RuntimeError):
    pass


def _load_obb() -> Any:
    global _obb, _import_error
    with _obb_lock:
        if _obb is None and _import_error is None:
            try:
                from openbb import obb  # noqa: WPS433 (deliberately lazy)
                obb.user.preferences.output_type = "OBBject"
                _obb = obb
            except Exception as exc:  # ImportError, or provider build errors
                _import_error = f"{type(exc).__name__}: {exc}"
    if _obb is None:
        raise OpenBBUnavailable(f"OpenBB is not available ({_import_error}). "
                                "Install with: pip install -r backend/requirements-data.txt")
    for cred, env_name in CREDENTIALS.items():
        value = os.getenv(env_name, "").strip()
        if value:
            setattr(_obb.user.credentials, cred, value)
    return _obb


def warm() -> None:
    """Import OpenBB in a background thread so the first request is fast."""
    threading.Thread(target=lambda: _safe(_load_obb), daemon=True, name="openbb-warm").start()


def _safe(fn):
    try:
        return fn()
    except OpenBBUnavailable:
        return None


def status() -> dict:
    loaded = _obb is not None
    keyed = {cred: bool(os.getenv(env_name, "").strip()) for cred, env_name in CREDENTIALS.items()}
    try:
        import importlib.metadata as md
        version = md.version("openbb")
    except Exception:
        version = None
    return {"installed": version is not None, "version": version, "loaded": loaded, "import_error": _import_error,
            "keys": keyed, "calendar_provider": _calendar_provider(None),
            "free_sources": ["yfinance (FX/indices candles)", "forexfactory (weekly calendar)"]}


def self_test() -> dict:
    data = htf_candles("EURUSD", "1d", 10, "yfinance")
    return {"ok": bool(data["candles"]), "detail": {"bars": len(data["candles"]), "source": data["source"]}}


# ------------------------------------------------------------------ candles
def to_yf_pair(symbol: str) -> str:
    """frxEURUSD / EURUSD → EURUSD (OpenBB currency symbols have no prefix)."""
    return symbol.removeprefix("frx").upper()


def htf_candles(pair: str, interval: str = "1d", days: int = 365, provider: str = "yfinance") -> dict:
    obb = _load_obb()
    start = (date.today() - timedelta(days=max(1, days))).isoformat()
    result = obb.currency.price.historical(to_yf_pair(pair), provider=provider, interval=interval, start_date=start)
    frame = result.to_dataframe()
    candles = []
    for idx, row in frame.iterrows():
        ts = idx if isinstance(idx, datetime) else datetime.combine(idx, datetime.min.time())
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        candles.append({"epoch": int(ts.timestamp()), "open": float(row["open"]), "high": float(row["high"]),
                        "low": float(row["low"]), "close": float(row["close"])})
    return {"pair": to_yf_pair(pair), "interval": interval, "source": f"openbb:{provider}", "candles": candles}


def crosscheck(symbol: str, days: int = 60) -> dict:
    """Deriv daily closes (local DB) vs OpenBB/yfinance. Large deviations flag bad data."""
    from app.services.deriv import history
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    deriv = {c["epoch"] // 86400: c["close"] for c in history.load(symbol, 86400, since)}
    if not deriv:
        return {"symbol": symbol, "compared": 0, "note": "No Deriv daily candles stored; backfill granularity 86400."}
    ref = {c["epoch"] // 86400: c["close"] for c in htf_candles(symbol, "1d", days)["candles"]}
    diffs = [abs(deriv[d] - ref[d]) / ref[d] * 10_000 for d in deriv if d in ref and ref[d]]
    return {"symbol": symbol, "compared": len(diffs),
            "mean_abs_diff_bps": round(sum(diffs) / len(diffs), 2) if diffs else None,
            "max_abs_diff_bps": round(max(diffs), 2) if diffs else None}


# ----------------------------------------------------------------- calendar
def _calendar_provider(requested: str | None) -> str:
    if requested:
        return requested
    if os.getenv("FMP_API_KEY"):
        return "fmp"
    return "forexfactory"


def _store_events(items: list[dict]) -> None:
    if not items:
        return
    with db.connect() as conn, db.tx(conn):
        conn.executemany(
            "INSERT INTO econ_events(ts, currency, title, importance, source, actual, forecast, previous) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ts, currency, title) DO UPDATE SET "
            "importance=excluded.importance, actual=COALESCE(excluded.actual, econ_events.actual), "
            "forecast=excluded.forecast, previous=excluded.previous",
            [(e["ts"], e["currency"], e["title"], e["importance"], e["source"], e.get("actual"), e.get("forecast"),
              e.get("previous")) for e in items])


def _forexfactory() -> list[dict]:
    response = httpx.get(FOREXFACTORY_URL, timeout=15, headers={"User-Agent": "fx-scalper-community/1.0"})
    response.raise_for_status()
    items = []
    for e in response.json():
        try:
            ts = int(datetime.fromisoformat(e["date"]).timestamp())
        except (KeyError, ValueError):
            continue
        items.append({"ts": ts, "currency": str(e.get("country") or "").upper(), "title": e.get("title") or "",
                      "importance": IMPORTANCE.get(str(e.get("impact") or "").lower(), 1),
                      "source": "forexfactory", "forecast": e.get("forecast") or None,
                      "previous": e.get("previous") or None})
    return items


def _openbb_calendar(provider: str, start: date, end: date) -> list[dict]:
    obb = _load_obb()
    frame = obb.economy.calendar(provider=provider, start_date=start.isoformat(),
                                 end_date=end.isoformat()).to_dataframe()
    items = []
    for _, row in frame.reset_index().iterrows():
        when = row.get("date")
        if when is None:
            continue
        when = when if isinstance(when, datetime) else datetime.fromisoformat(str(when))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        raw_importance = row.get("importance")
        importance = IMPORTANCE.get(str(raw_importance).lower()) if isinstance(raw_importance, str) else raw_importance
        items.append({"ts": int(when.timestamp()), "currency": str(row.get("currency") or row.get("country") or ""),
                      "title": str(row.get("event") or ""), "importance": int(importance or 1),
                      "source": f"openbb:{provider}", "actual": _s(row.get("actual")),
                      "forecast": _s(row.get("consensus", row.get("forecast"))), "previous": _s(row.get("previous"))})
    return items


def _s(value: Any) -> str | None:
    return None if value is None or value != value else str(value)  # value != value catches NaN


def calendar(days_back: int = 1, days_ahead: int = 7, provider: str | None = None, min_importance: int = 3) -> dict:
    chosen = _calendar_provider(provider)
    today = date.today()
    fallback_reason = None
    if chosen == "forexfactory":
        fetched = _forexfactory()
    else:
        try:
            fetched = _openbb_calendar(chosen, today - timedelta(days=days_back), today + timedelta(days=days_ahead))
        except Exception as exc:  # e.g. FMP free tier: 402 "Restricted Endpoint"
            if provider:  # explicitly requested: surface the error
                raise
            fallback_reason = f"{chosen} unavailable ({str(exc).strip().splitlines()[-1][:160]}); used forexfactory"
            chosen, fetched = "forexfactory", _forexfactory()
    _store_events(fetched)
    lo = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())
    hi = int((datetime.now(timezone.utc) + timedelta(days=days_ahead)).timestamp())
    return {"provider": chosen, "fallback_reason": fallback_reason, "fetched": len(fetched),
            "events": events_between(lo, hi, min_importance)}


def events_between(start_ts: int, end_ts: int, min_importance: int = 3,
                   currencies: list[str] | None = None) -> list[dict]:
    sql = "SELECT * FROM econ_events WHERE ts BETWEEN ? AND ? AND importance >= ?"
    params: list = [start_ts, end_ts, min_importance]
    if currencies:
        sql += f" AND currency IN ({','.join('?' * len(currencies))})"
        params.extend(currencies)
    with db.connect() as conn:
        return db.rows(conn, sql + " ORDER BY ts", tuple(params))


# --------------------------------------------------------------------- news
def news(query: str = "forex", limit: int = 20, provider: str | None = None) -> dict:
    obb = _load_obb()
    if provider is None:
        provider = "fmp" if os.getenv("FMP_API_KEY") else "tiingo" if os.getenv("TIINGO_TOKEN") else "yfinance"
    if provider == "yfinance":
        # Free: company/ticker news. FX pairs map to Yahoo tickers like EURUSD=X.
        ticker = query if "=" in query or query.isupper() else "EURUSD=X"
        frame = obb.news.company(ticker, provider="yfinance", limit=limit).to_dataframe()
    else:
        frame = obb.news.world(provider=provider, limit=limit).to_dataframe()
    items = []
    for idx, row in frame.reset_index().iterrows():
        items.append({"date": str(row.get("date") or idx), "title": row.get("title"), "url": row.get("url"),
                      "source": row.get("source") or provider})
    return {"provider": provider, "items": items[:limit]}
