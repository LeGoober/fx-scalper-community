"""Agents: configuration, spend, a live test call, and attribution (do vetoes earn their keep?)."""
from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.services.agents import attribution, llm, roles, runner

router = APIRouter(prefix="/api/agents", tags=["agents"])


class RoleUpdate(BaseModel):
    authority: Literal["off", "log", "veto"] | None = None
    model: str | None = Field(None, max_length=200)


class BudgetUpdate(BaseModel):
    daily_usd: float = Field(ge=0, le=1000)


class TestRequest(BaseModel):
    symbol: str = "frxEURUSD"
    direction: Literal["long", "short"] = "long"


@router.get("", summary="Agents: roles, authority, models, budget and today's spend")
def get_agents() -> dict:
    client = llm.LLMClient()
    with db.connect() as conn:
        counts = db.rows(conn, "SELECT role, COUNT(*) AS reviewed, SUM(would_veto) AS would_veto, "
                               "SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed FROM agent_outputs GROUP BY role")
    return {"key_set": client.available, "base_url": client.base_url, "roles": roles.settings(),
            "budget": llm.budget(), "spend_today": llm.spend_today(), "counts": {c["role"]: c for c in counts}}


@router.put("/budget", summary="Daily spend cap (credits ≈ USD); calls are refused above it")
def put_budget(body: BudgetUpdate) -> dict:
    return llm.set_budget(body.daily_usd)


@router.put("/{role}", summary="Set a role's authority (off | log | veto) and model")
def put_role(role: str, body: RoleUpdate) -> dict:
    try:
        return roles.set_role(role, authority=body.authority, model=body.model)
    except KeyError as exc:
        raise HTTPException(404, "Unknown agent role.") from exc


@router.post("/{role}/test", summary="Run one role now on a synthetic proposal (checks key, model and output)")
async def test_role(role: str, body: TestRequest) -> dict:
    if role not in roles.ROLES:
        raise HTTPException(404, "Unknown agent role.")
    r = roles.ROLES[role]
    cfg = roles.settings()[role]
    if not cfg["model"]:
        raise HTTPException(412, f"Choose a model for {role} first.")
    client = llm.LLMClient()
    if not client.available:
        raise HTTPException(412, "Add an OpenRouter API key in Settings → API keys first.")
    proposal = {"symbol": body.symbol, "direction": body.direction, "entry_type": "limit"}
    out = await runner._run_role(r, cfg | {"authority": "log"}, proposal, time.time(), client)
    return out


@router.get("/attribution", summary="Per role: outcomes of setups it would veto vs let through")
def get_attribution(role: str | None = None) -> dict:
    return attribution.report(role)
