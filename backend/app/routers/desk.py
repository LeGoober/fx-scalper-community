"""Desk: the master switch, per-trade OFF, decisions with their evidence, and broker status."""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.services import decision, execution, risk
from app.services.brokers.base import BrokerError

router = APIRouter(tags=["desk"])


class AutonomyUpdate(BaseModel):
    master: Literal["off", "pause", "on"]


class ManualProposal(BaseModel):
    symbol: str = "frxEURUSD"
    direction: Literal["long", "short"]
    stop: float
    target: float | None = None
    entry: float | None = Field(None, description="Limit price; omit for a market order")
    risk_amount: float = Field(1.0, gt=0)
    expires_in_minutes: int = Field(60, ge=1, le=1440)
    broker: Literal["ctrader", "capital"] = "ctrader"


def _exposure() -> list[dict]:
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT id, mode, broker, symbol, direction, status, size, entry_price, stop_price, "
                             "target_price, risk_amount, opened_at, auto, deal_id, decision_id FROM trades "
                             f"WHERE status IN ({','.join('?' * len(risk.ACTIVE_STATUSES))}) ORDER BY opened_at DESC",
                       risk.ACTIVE_STATUSES)
    return rows


@router.get("/api/autonomy", summary="Master switch state, today's risk and everything that is still exposed")
def get_autonomy() -> dict:
    return risk.autonomy() | {"kill_switch": risk.kill_switch_active(), "today": risk.today_stats(),
                              "limits": risk.limits(), "exposure": _exposure(), "execution": execution.SERVICE.status()}


@router.put("/api/autonomy", summary="Set the master switch (OFF also cancels and closes automated trades)")
async def put_autonomy(body: AutonomyUpdate) -> dict:
    try:
        state = risk.set_autonomy(body.master)
    except (ValueError, risk.RiskBlocked) as exc:
        raise HTTPException(409, str(exc)) from exc
    flattened = await execution.SERVICE.flatten("master switch OFF") if body.master == "off" else None
    return state | {"flattened": flattened}


@router.post("/api/autonomy/flatten", summary="Cancel working orders and close every automated position now")
async def flatten() -> dict:
    return await execution.SERVICE.flatten("flatten button")


@router.post("/api/desk/trades/{trade_id}/close", summary="Per-trade OFF: cancel the order or close the position")
async def close_trade(trade_id: str) -> dict:
    try:
        return await execution.SERVICE.close_trade(trade_id, "trade switched OFF")
    except KeyError as exc:
        raise HTTPException(404, "Unknown trade.") from exc
    except (BrokerError, risk.RiskBlocked) as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/api/desk/decisions", summary="Recent decisions (newest first)")
def list_decisions(limit: int = 100) -> dict:
    return {"decisions": decision.recent(min(max(limit, 1), 500))}


@router.get("/api/desk/decisions/{decision_id}", summary="The full dossier behind one decision")
def get_decision(decision_id: str) -> dict:
    d = decision.dossier(decision_id)
    if d is None:
        raise HTTPException(404, "Unknown decision.")
    return d


@router.get("/api/desk/brokers", summary="Execution venues: configured, connected, account")
def brokers() -> dict:
    from app import config
    return {"ctrader": {"configured": bool(config.ctrader_client_id() and config.ctrader_client_secret()
                                           and config.ctrader_access_token()),
                        "app_set": bool(config.ctrader_client_id() and config.ctrader_client_secret())},
            "capital": {"configured": bool(config.capital_api_key() and config.capital_identifier()
                                           and config.capital_api_password())},
            "default": config.default_broker(),
            "connected": execution.SERVICE.status()["brokers"]}


@router.post("/api/desk/brokers/{name}/connect", summary="Connect a venue's DEMO account (verifies the keys)")
async def connect_venue(name: Literal["ctrader", "capital"]) -> dict:
    try:
        b = await execution.SERVICE.broker(name, "demo")
    except (BrokerError, risk.RiskBlocked) as exc:
        raise HTTPException(502, str(exc)) from exc
    return b.info()


@router.post("/api/desk/reconcile", summary="Reconcile the ledger with the broker now")
async def reconcile() -> dict:
    try:
        return await execution.SERVICE.reconcile()
    except BrokerError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/api/desk/proposals/manual", summary="Place one hand-made trade through the full pipeline (DEMO only)")
async def manual(body: ManualProposal) -> dict:
    p = decision.Proposal(source="manual", symbol=body.symbol, direction=body.direction, stop=body.stop,
                          target=body.target, entry=body.entry, entry_type="limit" if body.entry else "market",
                          expires_at=int(time.time()) + body.expires_in_minutes * 60,
                          setup_key=f"manual-{int(time.time() * 1000)}", evidence={"note": "manual proposal"})
    # auto=False: a human placed it, so the master switch doesn't gate it; every risk limit still does.
    return await decision.submit(p, broker=body.broker, account_kind="demo", risk_amount=body.risk_amount, auto=False)
