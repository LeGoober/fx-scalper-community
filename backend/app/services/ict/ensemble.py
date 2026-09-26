"""Validation ensemble → one confidence JSON per trade setup.

  setup facts (computed by code)
     │
     ├── schema 1 ─┬─ Jev  ┐   each schema = the strategy's judge questions, one phrasing each
     ├── schema 2 ─┼─ Laya ┤   (question + question_variants from the strategy file), so the
     └── schema k ─┘  ...  ┘   verdict is not hostage to one wording or one model
                     │
             confidence report (JSON)
               per node: weighted mean over schemas × judges, spread, gate, pass
               overall: confidence = mean(gating node values) × agreement
               decision: execute only if every gating node passes AND confidence ≥ threshold

Weights and threshold live in the strategy (`ensemble`), and they must be validated in
backtests like any other parameter. An ensemble reduces wording and model noise; it
cannot create an edge that the underlying setup does not have.
"""
from __future__ import annotations

import asyncio
from statistics import mean, pstdev
from typing import Any

from app import db


def schemas_for(nodes: list) -> list[dict[str, dict]]:
    """k schemas; schema i asks every judge node in its i-th phrasing (cycling shorter variant lists)."""
    phrasings = {n.id: [n.question, *(n.question_variants or [])] for n in nodes if n.question}
    k = max((len(v) for v in phrasings.values()), default=0)
    return [{nid: v[i % len(v)] for nid, v in phrasings.items()} for i in range(k)]


def value_of(answer: dict, node: Any, direction: str) -> float:
    """Map any answer to [0, 1] on the node's own gate scale (same semantics as strategy._gate_jev)."""
    if answer["type"] == "noul":
        return float(answer["noul"])
    if answer["type"] == "score":
        return float(answer["score_norm"])
    want = (node.gate.get("expect") or {}).get(direction)
    return float(answer["probabilities"].get(want, 0.0)) if want else float(answer["confidence"])


def gate_of(node: Any) -> float:
    g = node.gate
    return float(g.get("min", g.get("min_probability", g.get("min_confidence", 0.5))))


async def validate(state: dict, nodes: list, direction: str, judges: dict[str, Any], *,
                   weights: dict[str, float] | None = None, threshold: float = 0.6,
                   code_results: list[dict] | None = None, plan: dict | None = None,
                   meta: dict | None = None) -> dict:
    weights = weights or {"jev": 0.6, "laya": 0.4}
    active = {name: j for name, j in judges.items() if j is not None and getattr(j, "available", True)}
    if not active:
        raise RuntimeError("No judge available for the ensemble (need TYPESAFE_API_KEY and/or laya installed).")
    schemas = schemas_for(nodes)

    async def run(i: int, name: str, judge: Any) -> dict:
        try:
            res = await judge.aask(state, schemas[i])
            return {"schema": i + 1, "judge": name, "model": res.model, "answers": res.answers,
                    "latency_ms": res.latency_ms, "cached": res.cached, "error": None}
        except Exception as exc:  # one failing judge must not sink the report; it is recorded
            return {"schema": i + 1, "judge": name, "model": None, "answers": {}, "latency_ms": None,
                    "cached": False, "error": f"{type(exc).__name__}: {exc}"}

    validations = await asyncio.gather(*(run(i, n, j) for i in range(len(schemas)) for n, j in active.items()))
    node_report: dict[str, dict] = {}
    for node in nodes:
        pts = [(v["judge"], value_of(v["answers"][node.id], node, direction))
               for v in validations if node.id in v["answers"]]
        if not pts:
            node_report[node.id] = {"label": node.label, "mean": None, "passed": False, "on_fail": node.on_fail,
                                    "gate": gate_of(node), "n": 0, "note": "no judge answered"}
            continue
        wsum = sum(weights.get(j, 0.0) for j, _ in pts) or 1.0
        m = sum(weights.get(j, 0.0) * x for j, x in pts) / wsum
        by_judge = {j: round(mean(x for jj, x in pts if jj == j), 4) for j in {jj for jj, _ in pts}}
        spread = round(pstdev([x for _, x in pts]), 4)
        node_report[node.id] = {"label": node.label, "mean": round(m, 4), "spread": spread,
                                "by_judge": by_judge, "gate": gate_of(node), "passed": m >= gate_of(node),
                                "on_fail": node.on_fail, "n": len(pts)}
    gating = [r for r in node_report.values() if r["on_fail"] == "reject"]
    means = [r["mean"] for r in gating if r["mean"] is not None]
    spreads = [r["spread"] for r in gating if r.get("spread") is not None]
    agreement = max(0.0, 1.0 - 2 * (mean(spreads) if spreads else 0.0))
    confidence = round((mean(means) if means else 0.0) * agreement, 4)
    code_ok = all(c.get("passed", True) for c in (code_results or []) if not str(c.get("id", "")).startswith("flag:"))
    judges_ok = all(r["passed"] for r in gating)
    execute = code_ok and judges_ok and confidence >= threshold
    reasons = ([f"code node failed: {c['id']}" for c in (code_results or [])
                if not c.get("passed", True) and not str(c.get("id", "")).startswith("flag:")]
               + [f"{nid} below gate ({r['mean']} < {r['gate']})" for nid, r in node_report.items()
                  if r["on_fail"] == "reject" and not r["passed"]]
               + ([f"confidence {confidence} < threshold {threshold}"] if confidence < threshold else []))
    return {
        "kind": "fxs.confidence/v1", "created_at": db.utc_now(), **(meta or {}),
        "direction": direction, "plan": plan, "state": state,
        "judges": {n: getattr(j, "model", n) for n, j in active.items()}, "weights": weights,
        "schemas": len(schemas), "validations": validations, "code_nodes": code_results or [],
        "nodes": node_report, "agreement": round(agreement, 4), "confidence": confidence, "threshold": threshold,
        "decision": "execute" if execute else "skip", "reasons": reasons or ["all checks passed"],
    }
