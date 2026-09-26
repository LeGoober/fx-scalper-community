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
# One inference at a time: torch already uses every CPU core per call, and parallel calls
# thrash each other (measured: ~250 s per call with 12 concurrent vs a few seconds serial).
_infer_lock = threading.Lock()
BATCH_SIZE = 16


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
        with _infer_lock:
            started = time.perf_counter()
            raw = agent.predict(state, questions)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
        result = self._result(raw, questions, elapsed)
        if self.use_cache:
            _cache_put(key, result)
        return result

    def _result(self, raw: dict, questions: dict[str, dict], latency_ms: float) -> JevResult:
        answers = {name: _normalise(raw["answers"][name], spec) for name, spec in questions.items()}
        usage = raw.get("usage") or {}
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        return JevResult(answers, self.model, tokens, latency_ms)

    def ask_batch(self, states: list[Any], questions: dict[str, dict],
                  progress: Any = None) -> list[JevResult]:
        """Many states, one question set, batched through Laya's predict_batch; fills the same cache."""
        import json
        keys = [cache_key(self.model, s, questions) for s in states]
        out: list[JevResult | None] = [None] * len(states)
        misses = []
        for i, k in enumerate(keys):
            row = _cache_get(k) if self.use_cache else None
            if row is not None:
                out[i] = JevResult(json.loads(row["response_json"]), row["model"], row["input_tokens"], 0.0, True)
            else:
                misses.append(i)
        agent = _load() if misses else None
        # Chunked so every BATCH_SIZE states land in the cache: progress survives interruption
        # and a long run can be resumed.
        for c in range(0, len(misses), BATCH_SIZE):
            chunk = misses[c:c + BATCH_SIZE]
            with _infer_lock:
                started = time.perf_counter()
                raws = agent.predict_batch([states[i] for i in chunk], questions, batch_size=BATCH_SIZE)
                per_item = round((time.perf_counter() - started) * 1000 / len(chunk), 1)
            for i, raw in zip(chunk, raws):
                res = self._result(raw, questions, per_item)
                out[i] = res
                if self.use_cache:
                    _cache_put(keys[i], res)
            if progress:
                progress(min(c + BATCH_SIZE, len(misses)), len(misses), per_item)
        return [r for r in out if r is not None]

    async def aask(self, state: Any, questions: dict[str, dict], **_: Any) -> JevResult:
        return await asyncio.to_thread(self.ask, state, questions)

    def self_test(self) -> dict:
        r = LayaClient(use_cache=False).ask(
            {"message": "Price swept the previous day's high, then broke the last swing low with a large candle."},
            {"is_bearish": {"type": "noul", "instructions": "The described price action suggests bearish intent."}})
        return {"ok": True, "detail": {"model": r.model, "answer": r.answers, "latency_ms": r.latency_ms}}
