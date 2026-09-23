"""TradingView alert receiver.

An alert never places a trade by itself. The receiver:
  1. checks the shared secret (constant-time), then drops duplicate alert ids;
  2. maps the TradingView ticker to a Deriv symbol;
  3. logs it, and if the engine runs that symbol, asks it to re-evaluate NOW on fresh
     Deriv data. The strategy nodes and the risk gate still decide.
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

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

TICKER_MAP = {"EURUSD": "frxEURUSD", "GBPUSD": "frxGBPUSD", "USDJPY": "frxUSDJPY", "XAUUSD": "frxXAUUSD",
              "GOLD": "frxXAUUSD", "NAS100": "OTC_NDX", "NDX": "OTC_NDX", "US100": "OTC_NDX", "NQ1!": "OTC_NDX",
              "SPX500": "OTC_SPC", "SPX": "OTC_SPC", "US500": "OTC_SPC", "ES1!": "OTC_SPC", "US30": "OTC_DJI"}
SEEN_KEY = "tv_seen_alert_ids"


class TradingViewAlert(BaseModel):
    secret: str
    ticker: str = Field(examples=["FX:EURUSD", "EURUSD", "NAS100"])
    direction: str | None = Field(None, examples=["long", "short"])
    event: str = "ict_setup"
    id: str | None = Field(None, description="Idempotency key, e.g. '{{ticker}}-{{timenow}}'")
    price: float | None = None
    time: str | None = None
    note: str | None = None


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
    if watching:
        st = engine.ENGINE.states.get(symbol)
        await engine.ENGINE._evaluate(st)  # re-validate on fresh Deriv data; nodes + risk gate decide
    return {"status": "accepted", "symbol": symbol, "engine_rechecked": watching}
