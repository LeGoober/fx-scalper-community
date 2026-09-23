"""Jev (TypeSafe System One) client: typed judgments with a persistent answer cache.

Questions are plain serialisable specs (so strategies can store them as JSON):
    {"type": "choice", "instructions": "...", "criteria": {"key": "description", ...}}
    {"type": "score",  "instructions": "...", "criteria": ["level 0", "level 1", ...]}
    {"type": "noul",   "instructions": "...", "criteria": {"true": "...", "false": "..."}}   # criteria optional

Answers come back normalised to plain dicts:
    choice → {type, choice, confidence, probabilities{key: p}}
    score  → {type, score, score_norm (0..1), confidence, probabilities{level: p}}
    noul   → {type, noul (P(yes))}

The cache is keyed by (requested model, state, questions), so re-running a
backtest makes zero new calls and is bit-for-bit reproducible. Pin
COMMUNITY_JEV_MODEL (e.g. jev-1.13.0) so `jev-latest` upgrades never silently
move backtest thresholds.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app import config, db

PRICE_PER_MTOK_INPUT = 0.042  # USD, launch pricing; output tokens are free


class JevUnavailable(RuntimeError):
    pass


@dataclass
class JevResult:
    answers: dict[str, dict]
    model: str | None
    input_tokens: int | None
    latency_ms: float
    cached: bool = False
    extra: dict = field(default_factory=dict)


def _to_sdk(spec: dict) -> Any:
    from typesafe_sdk import Choice, Noul, Score
    kind = spec["type"]
    if kind == "choice":
        return Choice(instructions=spec.get("instructions"), criteria=spec["criteria"])
    if kind == "score":
        return Score(instructions=spec.get("instructions"), criteria=spec["criteria"])
    if kind == "noul":
        return Noul(instructions=spec.get("instructions"), criteria=spec.get("criteria"))
    raise ValueError(f"Unknown question type {kind!r}")


def _normalise(name: str, answer: Any, spec: dict) -> dict:
    kind = answer.type
    if kind == "choice":
        return {"type": "choice", "choice": answer.choice, "confidence": round(float(answer.confidence), 4),
                "probabilities": {k: round(float(v), 4) for k, v in answer.probabilities.items()}}
    if kind == "score":
        levels = max(1, len(spec.get("criteria") or []) - 1)
        return {"type": "score", "score": round(float(answer.score), 4),
                "score_norm": round(float(answer.score) / levels, 4),
                "confidence": round(float(answer.confidence), 4),
                # string keys: identical before and after a JSON cache round-trip
                "probabilities": {str(int(k)): round(float(v), 4) for k, v in answer.probabilities.items()}}
    if kind == "noul":
        return {"type": "noul", "noul": round(float(answer.noul), 4)}
    raise ValueError(f"{name}: unexpected answer type {kind!r}")


def cache_key(model: str, state: Any, questions: dict[str, dict]) -> str:
    blob = json.dumps({"m": model, "s": state, "q": questions}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    with db.connect() as conn:
        row = db.row(conn, "SELECT model, response_json, input_tokens FROM jev_cache WHERE key = ?", (key,))
    return row


def _cache_put(key: str, result: JevResult) -> None:
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO jev_cache(key, model, response_json, latency_ms, input_tokens, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (key, result.model, db.dumps(result.answers), result.latency_ms, result.input_tokens,
                      db.utc_now()))


class JevClient:
    def __init__(self, model: str | None = None, *, use_cache: bool = True) -> None:
        self.model = model or config.jev_model()
        self.use_cache = use_cache

    @property
    def available(self) -> bool:
        return bool(config.typesafe_api_key())

    def _require(self) -> None:
        if not self.available:
            raise JevUnavailable("TYPESAFE_API_KEY is not set (Settings → API keys, or .env).")

    def _from_cache(self, key: str) -> JevResult | None:
        if not self.use_cache:
            return None
        row = _cache_get(key)
        if row is None:
            return None
        return JevResult(answers=json.loads(row["response_json"]), model=row["model"],
                         input_tokens=row["input_tokens"], latency_ms=0.0, cached=True)

    def ask(self, state: Any, questions: dict[str, dict]) -> JevResult:
        key = cache_key(self.model, state, questions)
        hit = self._from_cache(key)
        if hit:
            return hit
        self._require()
        from typesafe_sdk import TypeSafeClient
        started = time.perf_counter()
        with TypeSafeClient(api_key=config.typesafe_api_key(), model=self.model) as client:
            response = client.system_one(state=state, questions={k: _to_sdk(v) for k, v in questions.items()})
        return self._finish(key, response, questions, started)

    def async_sdk(self) -> Any:
        """A reusable AsyncTypeSafeClient (use as `async with`) for many concurrent `aask` calls."""
        self._require()
        from typesafe_sdk import AsyncTypeSafeClient
        return AsyncTypeSafeClient(api_key=config.typesafe_api_key(), model=self.model)

    async def aask(self, state: Any, questions: dict[str, dict], *, sdk: Any = None) -> JevResult:
        key = cache_key(self.model, state, questions)
        hit = self._from_cache(key)
        if hit:
            return hit
        self._require()
        started = time.perf_counter()
        sdk_questions = {k: _to_sdk(v) for k, v in questions.items()}
        if sdk is not None:
            response = await sdk.system_one(state=state, questions=sdk_questions)
        else:
            async with self.async_sdk() as client:
                response = await client.system_one(state=state, questions=sdk_questions)
        return self._finish(key, response, questions, started)

    def _finish(self, key: str, response: Any, questions: dict[str, dict], started: float) -> JevResult:
        answers = {name: _normalise(name, ans, questions[name]) for name, ans in response.answers.items()}
        usage = getattr(response, "usage", None)
        result = JevResult(answers=answers, model=getattr(response, "model", self.model),
                           input_tokens=getattr(usage, "input_tokens", None),
                           latency_ms=round((time.perf_counter() - started) * 1000, 1))
        if self.use_cache:
            _cache_put(key, result)
        return result

    def self_test(self) -> dict:
        result = JevClient(self.model, use_cache=False).ask(
            {"message": "Price swept the previous day's high, then broke the last swing low with a large candle."},
            {"is_bearish": {"type": "noul", "instructions": "The described price action suggests bearish intent."}})
        return {"ok": True, "detail": {"model": result.model, "answer": result.answers,
                                       "latency_ms": result.latency_ms, "input_tokens": result.input_tokens}}


def usage_summary() -> dict:
    with db.connect() as conn:
        row = db.row(conn, "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS tokens, "
                           "AVG(NULLIF(latency_ms, 0)) AS avg_latency_ms, MAX(created_at) AS last_call FROM jev_cache")
        models = db.rows(conn, "SELECT model, COUNT(*) AS calls FROM jev_cache GROUP BY model")
    return {"cached_calls": row["calls"], "input_tokens": row["tokens"],
            "estimated_cost_usd": round(row["tokens"] / 1_000_000 * PRICE_PER_MTOK_INPUT, 4),
            "avg_latency_ms": round(row["avg_latency_ms"], 1) if row["avg_latency_ms"] else None,
            "last_call": row["last_call"], "models": models, "configured_model": config.jev_model(),
            "key_set": bool(config.typesafe_api_key())}
