"""Laya (Convai Innovations, Apache-2.0): an open-weights, locally run alternative to Jev.

Same question format as Jev (type/instructions/criteria with choice | score | noul), so
it plugs in behind the same interface as JevClient: `ask`/`aask` return a JevResult with
answers normalised the same way, cached in the same table under model "laya:<checkpoint>".

Weights (~2.3 GB) download from Hugging Face on first load and are cached locally. The
model is loaded once per process; inference runs on CPU if there is no GPU.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from app import config
from app.services.jev.client import JevResult, _cache_get, _cache_put, cache_key

_model: Any = None
_lock = threading.Lock()


class LayaUnavailable(RuntimeError):
    pass


def checkpoint() -> tuple[str, str | None]:
    repo = config.env("COMMUNITY_LAYA_MODEL", "convaiinnovations/laya")
    sub = config.env("COMMUNITY_LAYA_SUBFOLDER") or None
    return repo, sub


def model_name() -> str:
    repo, sub = checkpoint()
    return f"laya:{repo}" + (f"/{sub}" if sub else "")


def installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("laya") is not None


def _load() -> Any:
    global _model
    with _lock:
        if _model is None:
            if not installed():
                raise LayaUnavailable("Laya is not installed (pip install laya).")
            import laya
            repo, sub = checkpoint()
            _model = laya.load(repo, subfolder=sub) if sub else laya.load(repo)
    return _model


def _num(x: Any) -> float:
    return float(x.item() if hasattr(x, "item") else x)


def _normalise(answer: dict, spec: dict) -> dict:
    kind = spec["type"]
    if kind == "noul":
        return {"type": "noul", "noul": round(_num(answer["noul"]), 4)}
    probs = answer.get("probabilities") or {}
    if kind == "choice":
        return {"type": "choice", "choice": answer["choice"],
                "confidence": round(_num(answer.get("confidence", 0.0)), 4),
                "probabilities": {str(k): round(_num(v), 4) for k, v in probs.items()}}
    levels = max(1, len(spec.get("criteria") or []) - 1)
    score = _num(answer["score"])
    return {"type": "score", "score": round(score, 4), "score_norm": round(score / levels, 4),
            "confidence": round(_num(answer.get("confidence", 0.0)), 4),
            "probabilities": {str(int(k)) if str(k).isdigit() else str(k): round(_num(v), 4)
                              for k, v in (probs.items() if isinstance(probs, dict) else enumerate(probs))}}


class LayaClient:
    def __init__(self, *, use_cache: bool = True) -> None:
        self.model = model_name()
        self.use_cache = use_cache

    @property
    def available(self) -> bool:
        return installed()

    def ask(self, state: Any, questions: dict[str, dict]) -> JevResult:
        key = cache_key(self.model, state, questions)
        if self.use_cache:
            row = _cache_get(key)
            if row is not None:
                import json
                return JevResult(json.loads(row["response_json"]), row["model"], row["input_tokens"], 0.0, True)
        agent = _load()
        started = time.perf_counter()
        raw = agent.predict(state, questions)
        answers = {name: _normalise(raw["answers"][name], spec) for name, spec in questions.items()}
        usage = raw.get("usage") or {}
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        result = JevResult(answers, self.model, tokens, round((time.perf_counter() - started) * 1000, 1))
        if self.use_cache:
            _cache_put(key, result)
        return result

    async def aask(self, state: Any, questions: dict[str, dict], **_: Any) -> JevResult:
        return await asyncio.to_thread(self.ask, state, questions)

    def self_test(self) -> dict:
        r = LayaClient(use_cache=False).ask(
            {"message": "Price swept the previous day's high, then broke the last swing low with a large candle."},
            {"is_bearish": {"type": "noul", "instructions": "The described price action suggests bearish intent."}})
        return {"ok": True, "detail": {"model": r.model, "answer": r.answers, "latency_ms": r.latency_ms}}
