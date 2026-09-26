"""Strategy: Jev tagging of transcripts, the concept digest, and versioned ICT strategy schemas + diagrams."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app import jobs
from app.services.ict import strategy as S
from app.services.jev import tagging
from app.services.jev.client import JevClient, JevUnavailable, usage_summary

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


class TagRequest(BaseModel):
    video_ids: list[str] | None = Field(None, description="Default: every stored transcript")
    limit: int | None = Field(None, ge=1)
    retag: bool = False


@router.get("/concepts", summary="ICT concept taxonomy used for Jev tagging, with tag counts")
def concepts() -> dict:
    counts = {c["concept"]: c for c in tagging.concept_summary()}
    return {"concepts": [{"id": k, "description": v, **{x: counts.get(k, {}).get(x) for x in
                                                        ("windows", "rule_windows", "avg_specificity")}}
                         for k, v in tagging.ICT_CONCEPTS.items()],
            "questions": tagging.QUESTIONS}


@router.post("/tagging/jobs", summary="Tag transcript windows with Jev (background; progress on /api/stream)")
def start_tagging(body: TagRequest) -> dict:
    if not JevClient().available:
        raise HTTPException(412, "TYPESAFE_API_KEY is not set. Add it under Settings → API keys.")

    async def run(job: jobs.Job) -> dict:
        stats = await tagging.tag_videos(body.video_ids, limit=body.limit, retag=body.retag,
                                         progress=lambda s: job.update(**s))
        stats["digest"] = await asyncio.to_thread(tagging.write_digest)
        return stats
    return jobs.start("tagging", body.model_dump(), run).to_dict()


@router.get("/tags", summary="Tagged windows ranked by rule-likeness (optionally for one concept)")
def tags(concept: str | None = None, min_rule: float = 0.6, limit: int = 60) -> dict:
    return {"passages": tagging.ranked_passages(concept, min_rule, limit)}


@router.post("/digest", summary="Rebuild digest_by_concept.md from stored tags")
def digest(per_concept: int = 25, min_rule: float = 0.6) -> dict:
    return tagging.write_digest(per_concept, min_rule)


@router.get("/jev/usage", summary="Jev calls, tokens, estimated cost and latency")
def jev_usage() -> dict:
    return usage_summary()


@router.post("/jev/test", summary="One live Jev call (verifies key and latency)")
async def jev_test() -> dict:
    try:
        return await asyncio.to_thread(JevClient().self_test)
    except JevUnavailable as exc:
        raise HTTPException(412, str(exc)) from exc


# ------------------------------------------------------------------ schemas
@router.get("/schemas", summary="All strategy schema versions")
def schemas() -> dict:
    return {"strategies": S.list_all()}


@router.get("/schemas/{strategy_id}", summary="One schema (latest version unless ?version=)")
def schema(strategy_id: str, version: int | None = None) -> dict:
    try:
        return S.get(strategy_id, version).model_dump()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/schemas/{strategy_id}/graph", summary="Decision graph (nodes/edges) for the UI's SVG renderer")
def schema_graph(strategy_id: str, version: int | None = None) -> dict:
    try:
        return S.graph(S.get(strategy_id, version))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/schemas/{strategy_id}/mermaid", response_class=PlainTextResponse,
            summary="Mermaid flowchart source of the decision graph")
def schema_mermaid(strategy_id: str, version: int | None = None) -> str:
    try:
        return S.mermaid(S.get(strategy_id, version))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/laya/status", summary="Is Laya (open-weights judge) installed, which checkpoint, is it loaded")
def laya_status() -> dict:
    from app.services import laya_client
    return {"installed": laya_client.installed(), "model": laya_client.model_name(),
            "loaded": laya_client._model is not None}


@router.post("/laya/test", summary="One local Laya call (first call downloads/loads weights: slow)")
async def laya_test() -> dict:
    from app.services.laya_client import LayaClient, LayaUnavailable
    try:
        return await asyncio.to_thread(LayaClient().self_test)
    except LayaUnavailable as exc:
        raise HTTPException(412, str(exc)) from exc
