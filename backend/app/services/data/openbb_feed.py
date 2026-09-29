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
import time
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
    """Upsert calendar rows. `first_seen_at` is set once and never changed.

    Point-in-time note: the news filter uses only the scheduled time and importance, which are
    published days ahead, so reading this table in a backtest does not leak the future. `actual`
    (the released number) IS later information and must never feed a historical decision."""
    if not items:
        return
    now = int(time.time())
    with db.connect() as conn, db.tx(conn):
        conn.executemany(
            "INSERT INTO econ_events(ts, currency, title, importance, source, actual, forecast, previous, "
            "first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ts, currency, title) DO UPDATE SET "
            "importance=excluded.importance, actual=COALESCE(excluded.actual, econ_events.actual), "
            "forecast=excluded.forecast, previous=excluded.previous, "
            "first_seen_at=COALESCE(econ_events.first_seen_at, excluded.first_seen_at)",
            [(e["ts"], e["currency"], e["title"], e["importance"], e["source"], e.get("actual"), e.get("forecast"),
              e.get("previous"), now) for e in items])


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
    from app.services.data import observations
    for e in fetched:  # every version of every event, stamped with when we first had it
        observations.record(f"calendar:{e['source']}", "calendar", e, key=f"{e['ts']}|{e['currency']}|{e['title']}",
                            currencies=[e["currency"]], title=e["title"])
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
RSS_FEEDS = ("https://www.investing.com/rss/news_1.rss",   # forex news
             "https://www.fxstreet.com/rss/news")


def _rss_items(limit: int) -> list[dict]:
    """Free forex headlines from public RSS feeds (headline, link, time only)."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    feeds = [u for u in os.getenv("COMMUNITY_NEWS_RSS", "").split(",") if u.strip()] or list(RSS_FEEDS)
    items: list[dict] = []
    for url in feeds:
        try:
            r = httpx.get(url.strip(), timeout=15, follow_redirects=True,
                          headers={"User-Agent": "fx-scalper-community/1.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except (httpx.HTTPError, ET.ParseError):
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            if not title:
                continue
            try:
                when = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                when = ""
            items.append({"date": when, "title": title, "url": (it.findtext("link") or "").strip(),
                          "source": httpx.URL(url.strip()).host})
    items.sort(key=lambda i: i["date"], reverse=True)
    if not items:
        raise RuntimeError("no RSS feed answered")
    return items[:limit]


def _news_frame(obb: Any, provider: str, query: str, limit: int) -> Any:
    if provider == "yfinance":
        # Free: company/ticker news. FX pairs map to Yahoo tickers like EURUSD=X.
        ticker = query if "=" in query or query.isupper() else "EURUSD=X"
        return obb.news.company(ticker, provider="yfinance", limit=limit).to_dataframe()
    return obb.news.world(provider=provider, limit=limit).to_dataframe()


def news(query: str = "forex", limit: int = 20, provider: str | None = None) -> dict:
    """Headlines. With no provider given, tries keyed providers first and falls back to free yfinance
    (the FMP free tier answers 402 for news); an explicitly requested provider re-raises instead."""
    order = [provider] if provider else [p for p, key in (("fmp", "FMP_API_KEY"), ("tiingo", "TIINGO_TOKEN"))
                                         if os.getenv(key)] + ["rss", "yfinance"]
    frame, rss, fallback_reason = None, None, None
    for candidate in order:
        try:
            if candidate == "rss":
                rss, provider = _rss_items(limit), candidate
            else:
                frame, provider = _news_frame(_load_obb(), candidate, query, limit), candidate
            break
        except Exception as exc:
            if len(order) == 1:
                raise
            fallback_reason = f"{candidate} unavailable ({str(exc).strip().splitlines()[-1][:120]})"
    if frame is None and rss is None:
        raise RuntimeError(f"No news provider worked: {fallback_reason}")
    items = list(rss or [])
    for idx, row in (frame.reset_index().iterrows() if frame is not None else []):
        items.append({"date": str(row.get("date") or idx), "title": row.get("title"), "url": row.get("url"),
                      "source": row.get("source") or provider})
    from app.services.data import observations
    new = 0
    for item in items[:limit]:
        published = _epoch_or_none(item["date"])
        new += observations.record(f"news:{provider}", "news", item, published_at=published,
                                   key=item.get("url") or item.get("title"), title=item.get("title"),
                                   currencies=_currencies_in(item.get("title") or "")) is not None
    return {"provider": provider, "items": items[:limit], "new": new, "fallback_reason": fallback_reason}


FX_CODES = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY", "XAU")
# Longest phrases first, so "australian dollar" is AUD and only a bare "dollar" means USD.
CURRENCY_PHRASES = (("australian dollar", "AUD"), ("new zealand dollar", "NZD"), ("canadian dollar", "CAD"),
                    ("swiss franc", "CHF"), ("us dollar", "USD"), ("u.s. dollar", "USD"), ("aussie", "AUD"),
                    ("kiwi", "NZD"), ("loonie", "CAD"), ("franc", "CHF"), ("dollar", "USD"), ("greenback", "USD"),
                    ("fed", "USD"), ("fomc", "USD"), ("euro", "EUR"), ("ecb", "EUR"), ("sterling", "GBP"),
                    ("pound", "GBP"), ("boe", "GBP"), ("yen", "JPY"), ("boj", "JPY"), ("rba", "AUD"),
                    ("rbnz", "NZD"), ("boc", "CAD"), ("snb", "CHF"), ("yuan", "CNY"), ("pboc", "CNY"),
                    ("gold", "XAU"))


def _currencies_in(text: str) -> list[str]:
    """Rough tag of which currencies a headline is about, for filtering (never for decisions)."""
    import re
    tags = {c for c in FX_CODES if re.search(rf"\b{c}\b|\b{c}(?=[A-Z]{{3}}\b)|(?<=\b[A-Z]{{3}}){c}\b", text.upper())}
    lower = f" {re.sub(r'[^a-z. ]', ' ', text.lower())} "
    for phrase, code in CURRENCY_PHRASES:
        if f" {phrase} " in lower:
            tags.add(code)
            lower = lower.replace(f" {phrase} ", " ")  # consumed: 'australian dollar' must not also match 'dollar'
    return sorted(tags)


def _epoch_or_none(value: str) -> int | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
