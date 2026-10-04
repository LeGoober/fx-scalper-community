"""TradingView alert receiver.

An alert never places a trade by itself. The receiver:
  1. checks the shared secret (constant-time), then drops duplicate alert ids;
  2. maps the TradingView ticker to a Deriv symbol;
  3. records it as an event (an untrusted observation). The engine's own scan on closed bars,
     the strategy nodes and the risk gate decide; an alert cannot trigger or bypass them.
So a forged, replayed or stale alert cannot open a position.

TradingView can only reach a public URL (a tunnel such as cloudflared is needed for
localhost), and webhooks require a paid TradingView plan.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import config, db, events
from app.services import engine
from app.services.data import observations

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

TICKER_MAP = {"EURUSD": "frxEURUSD", "GBPUSD": "frxGBPUSD", "USDJPY": "frxUSDJPY", "XAUUSD": "frxXAUUSD",
              "GOLD": "frxXAUUSD", "NAS100": "OTC_NDX", "NDX": "OTC_NDX", "US100": "OTC_NDX", "NQ1!": "OTC_NDX",
              "SPX500": "OTC_SPC", "SPX": "OTC_SPC", "US500": "OTC_SPC", "ES1!": "OTC_SPC", "US30": "OTC_DJI"}
SEEN_KEY = "tv_seen_alert_ids"
SOURCES_KEY = "tv_signal_sources"


class SignalSource(BaseModel):
    """An alert name you allow to propose trades. Unregistered alerts are only recorded."""
    enabled: bool = True
    broker: str = "ctrader"
    account_kind: str = Field("demo", pattern="^demo$", description="TradingView signals trade demo only for now")
    risk_amount: float = Field(1.0, gt=0)
    expires_in_minutes: int = Field(30, ge=1, le=1440)


class TradingViewAlert(BaseModel):
    secret: str
    ticker: str = Field(examples=["FX:EURUSD", "EURUSD", "NAS100"])
    direction: str | None = Field(None, examples=["long", "short"])
    event: str = "ict_setup"
    id: str | None = Field(None, description="Idempotency key, e.g. '{{ticker}}-{{timenow}}'")
    price: float | None = None
    time: str | None = None
    note: str | None = None
    entry: float | None = Field(None, description="Limit price; omit for a market order")
    stop: float | None = Field(None, description="Required for the alert to become a trade proposal")
    target: float | None = None


def map_ticker(ticker: str) -> str | None:
    raw = ticker.split(":")[-1].upper().replace("/", "")
    if raw.startswith(("FRX", "OTC_")):
        return ticker.split(":")[-1]
    return TICKER_MAP.get(raw)


@router.post("/tradingview", summary="Receive a TradingView alert (JSON body; see tradingview/*.pine)")
async def tradingview(alert: TradingViewAlert, request: Request) -> dict:
    expected = config.tradingview_webhook_secret()
    if not expected or not hmac.compare_digest(alert.secret.encode(), expected.encode()):
        events.publish("webhook.rejected", {"reason": "bad secret", "client": request.client.host if request.client
                                            else None}, level="warning", message="TradingView alert rejected")
        raise HTTPException(401, "Invalid webhook secret.")
    seen: list = db.kv_get(SEEN_KEY) or []
    if alert.id and alert.id in seen:
        return {"status": "duplicate", "id": alert.id}
    if alert.id:
        db.kv_set(SEEN_KEY, (seen + [alert.id])[-500:])
    symbol = map_ticker(alert.ticker)
    payload = alert.model_dump(exclude={"secret"}) | {"symbol": symbol}
    status = engine.status()
    watching = bool(symbol) and status["running"] and any(s["symbol"] == symbol for s in status["symbols"])
    events.publish("webhook.tradingview", payload | {"engine_watching": watching},
                   message=f"TradingView {alert.event} {alert.ticker} → {symbol or 'unmapped'}")
    obs_id = observations.record("tradingview", "tv_alert", payload, symbol=symbol, key=alert.id,
                                 title=f"{alert.event} {alert.ticker}")
    proposal = await _maybe_propose(alert, symbol)
    return {"status": "accepted", "symbol": symbol, "engine_watching": watching, "observation": obs_id,
            "proposal": proposal}


async def _maybe_propose(alert: TradingViewAlert, symbol: str | None) -> dict | None:
    """A registered alert with direction and stop becomes a proposal; the pipeline still decides."""
    import time as _time
    from app.services import decision
    source = (db.kv_get(SOURCES_KEY) or {}).get(alert.event)
    if not source or not source.get("enabled") or not symbol:
        return None
    if alert.direction not in {"long", "short"} or alert.stop is None:
        return {"status": "ignored", "reason": "a registered alert needs direction (long/short) and stop"}
    cfg = SignalSource.model_validate(source)
    p = decision.Proposal(source=f"tv:{alert.event}", symbol=symbol, direction=alert.direction, stop=alert.stop,
                          target=alert.target, entry=alert.entry, entry_type="limit" if alert.entry else "market",
                          expires_at=int(_time.time()) + cfg.expires_in_minutes * 60,
                          setup_key=alert.id or f"{alert.event}:{symbol}:{alert.time or int(_time.time())}",
                          evidence={"tradingview_alert": alert.model_dump(exclude={"secret"})})
    return await decision.submit(p, broker=cfg.broker, account_kind=cfg.account_kind, risk_amount=cfg.risk_amount,
                                 auto=True)


@router.get("/tradingview/sources", summary="TradingView alert names allowed to propose trades")
def list_sources() -> dict:
    return {"sources": db.kv_get(SOURCES_KEY) or {}}


@router.put("/tradingview/sources/{event}", summary="Allow an alert name to propose trades (demo only)")
def put_source(event: str, body: SignalSource) -> dict:
    sources = db.kv_get(SOURCES_KEY) or {}
    sources[event] = body.model_dump()
    db.kv_set(SOURCES_KEY, sources)
    events.publish("webhook.source_set", {"event": event} | body.model_dump(), message=f"Signal source {event} set")
    return {"sources": sources}


@router.delete("/tradingview/sources/{event}", summary="Stop an alert name from proposing trades")
def delete_source(event: str) -> dict:
    sources = db.kv_get(SOURCES_KEY) or {}
    sources.pop(event, None)
    db.kv_set(SOURCES_KEY, sources)
    return {"sources": sources}
