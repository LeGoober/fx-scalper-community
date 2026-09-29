"""Provider-agnostic LLM client for typed agent outputs (OpenAI-compatible chat completions).

Default provider: OpenRouter (one key, many models; set COMMUNITY_LLM_BASE_URL for another
OpenAI-compatible endpoint). No SDK dependency: plain httpx.

Rules this client enforces, because an LLM is untrusted software:
  * Output must be JSON matching a Pydantic schema. First attempt uses strict `json_schema`
    response_format; if the model/provider refuses it, a forced tool call with the same schema.
    Anything that doesn't validate gets ONE repair turn quoting the error, then the call fails
    and the role's fixed default applies. Unvalidated text never reaches the decision code.
  * temperature 0, and answers cached by (model, prompt hash, input hash): same question, same answer,
    no second charge.
  * A daily spend cap (credits ≈ USD) is a circuit breaker: over it, calls are refused.
  * Every call is written to `llm_calls` (request without the key, response, parse result, tokens,
    cost, latency) so each decision can show exactly what the model was asked and said.
  * `provider.data_collection = "deny"` by default: only providers that don't train on prompts.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Generic, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app import config, db

T = TypeVar("T", bound=BaseModel)
BUDGET_KEY = "llm_budget"
DEFAULT_DAILY_USD = 1.0


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class AgentResult(Generic[T]):
    ok: bool
    output: T | None
    error: str | None
    model: str
    call_id: str | None = None
    cached: bool = False
    latency_ms: float | None = None
    cost: float | None = None


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON Schema in the shape strict structured-output modes require: every object closed
    (additionalProperties false) and every property listed as required."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)
    return schema


def budget() -> dict:
    stored = db.kv_get(BUDGET_KEY) or {}
    return {"daily_usd": float(stored.get("daily_usd", DEFAULT_DAILY_USD))}


def set_budget(daily_usd: float) -> dict:
    if daily_usd < 0:
        raise ValueError("daily_usd must be ≥ 0")
    db.kv_set(BUDGET_KEY, {"daily_usd": float(daily_usd)})
    return budget()


def spend_today() -> dict:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db.connect() as conn:
        r = db.row(conn, "SELECT COUNT(*) AS calls, COALESCE(SUM(cost), 0) AS cost, "
                         "COALESCE(SUM(in_tok), 0) AS in_tok, COALESCE(SUM(out_tok), 0) AS out_tok "
                         "FROM llm_calls WHERE substr(created_at, 1, 10) = ? AND cached = 0", (day,))
    return {"day": day, "calls": r["calls"], "cost": round(r["cost"], 6), "in_tok": r["in_tok"],
            "out_tok": r["out_tok"]}


class LLMClient:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 45.0) -> None:
        self.base_url = (base_url or config.llm_base_url()).rstrip("/")
        self.api_key = api_key if api_key is not None else config.llm_api_key()
        self._transport, self._timeout = transport, timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def structured(self, role: str, system: str, user: dict, schema: type[T], *, model: str,
                         prompt_version: str, max_tokens: int = 700,
                         check: Callable[[T], str | None] | None = None) -> AgentResult[T]:
        """Ask for one typed answer. `check` adds semantic validation (e.g. cited ids exist)."""
        if not self.available:
            return AgentResult(False, None, "no LLM API key configured", model)
        if not model:
            return AgentResult(False, None, f"no model configured for role {role}", model)
        prompt_hash = _sha({"system": system, "version": prompt_version, "schema": schema.model_json_schema()})
        input_hash = _sha(user)
        cached = self._cached(model, prompt_hash, input_hash, schema)
        if cached is not None:
            return cached
        if spend_today()["cost"] >= budget()["daily_usd"]:
            return AgentResult(False, None, "daily LLM budget reached (circuit breaker)", model)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(user, ensure_ascii=False, default=str)}]
        last_error = "no attempt"
        for mode in ("json_schema", "tool"):
            result, retry_other_mode = await self._attempt(role, mode, messages, schema, model, prompt_version,
                                                           prompt_hash, input_hash, max_tokens, check)
            if result.ok or not retry_other_mode:
                return result
            last_error = result.error or last_error
        return AgentResult(False, None, last_error, model)

    # ------------------------------------------------------------ internals
    def _cached(self, model: str, prompt_hash: str, input_hash: str, schema: type[T]) -> AgentResult[T] | None:
        with db.connect() as conn:
            row = db.row(conn, "SELECT id, parsed_json FROM llm_calls WHERE model = ? AND prompt_hash = ? AND "
                               "input_hash = ? AND valid = 1 ORDER BY created_at DESC LIMIT 1",
                         (model, prompt_hash, input_hash))
        if not row:
            return None
        try:
            parsed = schema.model_validate_json(row["parsed_json"])
            return AgentResult(True, parsed, None, model, row["id"], cached=True)
        except ValidationError:
            return None

    def _body(self, mode: str, role: str, messages: list, schema: type[BaseModel], model: str, max_tokens: int) -> dict:
        body: dict = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        if "openrouter.ai" in self.base_url:
            body["provider"] = {"require_parameters": True,
                                "data_collection": config.env("COMMUNITY_LLM_DATA_COLLECTION", "deny")}
        if mode == "json_schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": role, "strict": True, "schema": strict_schema(schema)}}
        else:
            body["tools"] = [{"type": "function", "function": {"name": role, "description": f"Return the {role} answer",
                                                               "parameters": strict_schema(schema), "strict": True}}]
            body["tool_choice"] = {"type": "function", "function": {"name": role}}
        return body

    async def _post(self, body: dict) -> tuple[int, dict, float]:
        started = time.perf_counter()
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Title": "fx-scalper-community"}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
            r = await http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"message": r.text[:300]}}
        return r.status_code, data, (time.perf_counter() - started) * 1000

    async def _attempt(self, role, mode, messages, schema, model, prompt_version, prompt_hash, input_hash,
                       max_tokens, check) -> tuple[AgentResult, bool]:
        """One structured-output mode, with at most one repair turn. Returns (result, try_other_mode)."""
        convo = list(messages)
        for turn in (1, 2):
            body = self._body(mode, role, convo, schema, model, max_tokens)
            try:
                status, data, ms = await self._post(body)
            except httpx.HTTPError as exc:
                return AgentResult(False, None, f"{type(exc).__name__}: {exc}", model), False
            usage = data.get("usage") or {}
            cost = usage.get("cost")
            if status >= 400:
                err = str((data.get("error") or {}).get("message") or data)[:300]
                call_id = self._log(role, model, prompt_version, prompt_hash, input_hash, body, data, None, False,
                                    f"HTTP {status}: {err}", usage, cost, ms)
                # 400/404/422 on a structured-output request usually means the mode is unsupported: try tools.
                return AgentResult(False, None, f"HTTP {status}: {err}", model, call_id), status in {400, 404, 422}
            raw = self._extract(data, mode)
            error, parsed = None, None
            try:
                parsed = schema.model_validate_json(raw or "")
                error = check(parsed) if check else None
            except ValidationError as exc:
                error = f"schema: {exc.errors()[0].get('msg')} at {exc.errors()[0].get('loc')}"
            call_id = self._log(role, model, prompt_version, prompt_hash, input_hash, body, data,
                                parsed.model_dump_json() if parsed is not None and not error else None,
                                error is None, error, usage, cost, ms)
            if error is None:
                return AgentResult(True, parsed, None, model, call_id, latency_ms=ms, cost=cost), False
            if turn == 1:  # one repair turn quoting the problem
                convo = convo + [{"role": "assistant", "content": raw or ""},
                                 {"role": "user", "content": f"That answer was invalid: {error}. Reply again with "
                                                             "only the corrected JSON object."}]
        return AgentResult(False, None, f"invalid output after repair: {error}", model, call_id), raw in (None, "")

    @staticmethod
    def _extract(data: dict, mode: str) -> str | None:
        message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        if mode == "tool":
            calls = message.get("tool_calls") or []
            return (calls[0].get("function") or {}).get("arguments") if calls else None
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
            if text.startswith("```"):  # some models fence JSON despite the schema
                text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
            return text
        return None

    @staticmethod
    def _log(role, model, prompt_version, prompt_hash, input_hash, body, response, parsed, valid, error, usage, cost,
             ms) -> str:
        call_id = f"llm-{uuid.uuid4().hex[:12]}"
        with db.connect() as conn:
            conn.execute("INSERT INTO llm_calls(id, created_at, role, model, prompt_version, prompt_hash, input_hash, "
                         "request_json, response_json, parsed_json, valid, error, in_tok, out_tok, cost, latency_ms, "
                         "cached) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                         (call_id, db.utc_now(), role, model, prompt_version, prompt_hash, input_hash, db.dumps(body),
                          db.dumps(response), parsed, int(valid), error, usage.get("prompt_tokens"),
                          usage.get("completion_tokens"), cost, round(ms, 1)))
        return call_id
