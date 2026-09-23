"""System endpoints: API keys (write-only), risk controls, jobs, events, and the live stream."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app import config, events, jobs, secrets_store
from app.services import risk
from app.services.deriv.client import DerivClient, DerivError

router = APIRouter(prefix="/api", tags=["system"])


# ----------------------------------------------------------------- API keys
class SecretValue(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


@router.get("/secrets", summary="List managed keys (set / not set only, never values)")
def list_secrets() -> dict:
    return {"secrets": secrets_store.status(), "env_file": str(config.ENV_PATH.name)}


@router.put("/secrets/{name}", summary="Set a key (write-only)")
def put_secret(name: str, body: SecretValue) -> dict:
    try:
        secrets_store.set_secret(name, body.value)
    except secrets_store.SecretError as exc:
        raise HTTPException(400, str(exc)) from exc
    events.publish("system.secret_set", {"name": name}, message=f"{name} updated")
    return {"name": name, "is_set": True}


@router.delete("/secrets/{name}", summary="Clear a key")
def delete_secret(name: str) -> dict:
    try:
        secrets_store.clear_secret(name)
    except secrets_store.SecretError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"name": name, "is_set": False}


@router.post("/secrets/tradingview_webhook_secret/rotate",
             summary="Generate a new TradingView webhook secret (returned once)")
def rotate_tv_secret() -> dict:
    return {"name": "tradingview_webhook_secret", "value": secrets_store.rotate_webhook_secret(),
            "note": "Copy this into your TradingView alert message now; it is not shown again."}


async def _test_deriv() -> dict:
    client = DerivClient()
    try:
        info = await client.connect()
        info["rtt_ms"] = await client.ping()
        return {"ok": True, "detail": info}
    finally:
        await client.close()


async def _test_jev() -> dict:
    from app.services.jev.client import JevClient
    return await asyncio.to_thread(JevClient().self_test)


async def _test_openbb() -> dict:
    from app.services.data import openbb_feed
    return await asyncio.to_thread(openbb_feed.self_test)


TESTS = {"deriv_token": _test_deriv, "deriv_app_id": _test_deriv, "typesafe_api_key": _test_jev,
         "fmp_api_key": _test_openbb, "fred_api_key": _test_openbb, "tiingo_token": _test_openbb}


@router.post("/secrets/{name}/test", summary="Test the connection that uses this key")
async def test_secret(name: str) -> dict:
    fn = TESTS.get(name)
    if fn is None:
        raise HTTPException(400, f"No connection test for '{name}'.")
    started = time.perf_counter()
    try:
        result = await fn()
    except (DerivError, RuntimeError, ValueError, ImportError) as exc:
        result = {"ok": False, "detail": str(exc)}
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


# --------------------------------------------------------------------- risk
class LimitsUpdate(BaseModel):
    max_stake: float | None = None
    max_risk_per_trade: float | None = None
    max_daily_loss: float | None = None
    max_concurrent: int | None = None
    max_orders_per_day: int | None = None


class KillSwitch(BaseModel):
    active: bool


class ArmReal(BaseModel):
    confirm_phrase: str


@router.get("/risk", summary="Risk limits, today's usage, kill switch and real-money lock state")
def get_risk() -> dict:
    return {"limits": risk.limits(), "today": risk.today_stats(), "kill_switch": risk.kill_switch_active(),
            "real_trading": risk.real_trading_status()}


@router.put("/risk/limits")
def put_limits(body: LimitsUpdate) -> dict:
    try:
        return {"limits": risk.set_limits(body.model_dump(exclude_none=True))}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/risk/kill-switch", summary="Engage/release the kill switch (engaging also stops the engine)")
async def kill_switch(body: KillSwitch) -> dict:
    risk.set_kill_switch(body.active)
    if body.active:
        from app.services import engine
        await engine.stop(reason="kill switch")
    return {"kill_switch": risk.kill_switch_active()}


@router.post("/risk/real/arm", summary="Arm real trading for this process (only if .env allows it)")
def arm_real(body: ArmReal) -> dict:
    try:
        return risk.arm_real(body.confirm_phrase)
    except risk.RiskBlocked as exc:
        raise HTTPException(403, str(exc)) from exc


@router.post("/risk/real/disarm")
def disarm_real() -> dict:
    return risk.disarm_real()


# --------------------------------------------------------------- jobs/events
@router.get("/jobs")
def list_jobs(kind: str | None = None) -> dict:
    return {"jobs": jobs.list_jobs(kind)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job.")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    return {"cancelled": jobs.cancel(job_id)}


@router.get("/events", summary="Persisted event log (trades, signals, risk, jobs, broker)")
def list_events(limit: int = 100, kind: str = "") -> dict:
    return {"events": events.recent(limit, kind)}


@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    """Live event stream. Messages: {type, ts, level, message, data}."""
    await ws.accept()
    queue = events.subscribe()
    try:
        await ws.send_json({"type": "stream.hello", "ts": None, "level": "info", "message": "connected", "data": None})
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                message = {"type": "stream.heartbeat", "ts": None, "level": "debug", "message": "", "data": None}
            await ws.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        events.unsubscribe(queue)
