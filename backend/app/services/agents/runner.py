"""Run the enabled agents on a proposal and apply their (monotone) authority.

The runner is the only bridge between agents and the decision pipeline. Its contract:
  * returns ('execute' | 'skip', reasons, outputs); it can turn execute into skip, never the reverse;
  * every agent answer (or failure) is stored in `agent_outputs` with its LLM call id, and whether it
    WOULD have vetoed; that record is what attribution uses to earn (or deny) veto authority;
  * an agent that fails follows its role's on_failure; a timeout counts as a failure.
"""
from __future__ import annotations

import asyncio
import uuid

from app import db, events
from app.services.agents import llm as L
from app.services.agents import roles as R

AGENT_TIMEOUT_S = 40.0


async def _run_role(role: R.Role, cfg: dict, proposal: dict, ts: float, client: L.LLMClient) -> dict:
    given = role.build_input(proposal, ts)
    visible = {k: v for k, v in given.items() if not k.startswith("_")}
    try:
        res = await asyncio.wait_for(
            client.structured(role.name, role.system, visible, role.schema, model=cfg["model"],
                              prompt_version=role.prompt_version,
                              check=lambda out: role.check(out, given)),
            timeout=AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        res = L.AgentResult(False, None, f"timed out after {AGENT_TIMEOUT_S:.0f}s", cfg["model"])
    would_veto = bool(res.ok and role.vetoes(res.output))
    return {"role": role.name, "authority": cfg["authority"], "ok": res.ok, "error": res.error,
            "output": res.output.model_dump() if res.output is not None else None, "would_veto": would_veto,
            "llm_call_id": res.call_id, "model": res.model, "cached": res.cached, "latency_ms": res.latency_ms,
            "cost": res.cost, "on_failure": role.on_failure, "inputs": len(given.get("_ids", []))}


async def review(proposal: dict, *, ts: float | None = None,
                 client: L.LLMClient | None = None) -> tuple[str, list, list]:
    """Run every role whose authority isn't 'off'. Returns (verdict, reasons, outputs)."""
    cfgs = R.settings()
    client = client or L.LLMClient()
    # Not configured (no key, or no model chosen for the role) = not running: nothing to record.
    active = [(R.ROLES[n], c) for n, c in cfgs.items() if c["authority"] != "off" and c["model"]]
    if not active or not client.available:
        return "execute", [], []
    ts = ts if ts is not None else R.now()
    outputs = await asyncio.gather(*(_run_role(r, c, proposal, ts, client) for r, c in active))
    verdict, reasons = "execute", []
    for out in outputs:
        if out["authority"] != "veto":
            continue  # 'log': recorded for attribution only
        if out["ok"] and out["would_veto"]:
            verdict = "skip"
            level = (out["output"] or {}).get("level")
            reasons.append(f"{out['role']} veto ({level}): {(out['output'] or {}).get('rationale', '')[:160]}")
        elif not out["ok"] and out["on_failure"] == "skip":
            verdict = "skip"
            reasons.append(f"{out['role']} unavailable ({out['error']}) and it fails closed")
    return verdict, reasons, outputs


def record(outputs: list, *, proposal_id: str, decision_id: str) -> None:
    if not outputs:
        return
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO agent_outputs(id, created_at, proposal_id, decision_id, role, authority, ok, would_veto, "
            "output_json, error, llm_call_id, model, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"ao-{uuid.uuid4().hex[:10]}", db.utc_now(), proposal_id, decision_id, o["role"], o["authority"],
              int(bool(o["ok"])), int(bool(o.get("would_veto"))), db.dumps(o.get("output")), o.get("error"),
              o.get("llm_call_id"), o.get("model"), o.get("latency_ms")) for o in outputs])
    for o in outputs:
        if not o["ok"]:
            events.publish("agent.error", {"role": o["role"], "error": o.get("error")}, level="warning",
                           message=f"Agent {o['role']} failed: {o.get('error')}")
