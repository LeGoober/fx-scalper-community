"""Strategy schema (versioned JSON) and its runtime.

A strategy = scanner params (structural trigger) + ordered filter nodes + execution rules.
Nodes are either:
  code : a deterministic detector from DETECTORS
  jev  : a narrow Jev question over *computed facts* (no raw price arithmetic),
         with a gate, and a code `fallback` used in `code` mode or when Jev is
         unavailable. That lets every backtest compare code-only vs code+Jev.
Every node carries citations (video id + timestamp) back to ICT's own words.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from app import config, db
from app.services.ict import features as F
from app.services.ict.scanner import ScanParams

# ================================================================== schema


class Citation(BaseModel):
    video_id: str
    t: int = Field(description="Seconds into the video")
    title: str | None = None
    note: str = ""


class NodeSpec(BaseModel):
    id: str
    label: str
    kind: Literal["trigger", "code", "jev"]
    description: str = ""
    detector: str | None = None            # code: DETECTORS key
    params: dict = Field(default_factory=dict)
    question: dict | None = None           # jev: {"type", "instructions", "criteria"}
    question_variants: list[dict] = Field(default_factory=list)  # alternative phrasings for the ensemble
    gate: dict = Field(default_factory=dict)
    fallback: str | None = None            # jev: DETECTORS key used in code mode
    on_fail: Literal["reject", "flag"] = "reject"
    citations: list[Citation] = Field(default_factory=list)


class TargetSpec(BaseModel):
    mode: Literal["liquidity", "fixed_r"] = "liquidity"
    r: float = 2.0
    min_rr: float = 1.5
    max_r: float = 5.0


class ContractSpec(BaseModel):
    type: Literal["multiplier", "rise_fall"] = "multiplier"
    multiplier: int = 100
    rise_fall_minutes: int = 15
    rise_fall_payout: float = 0.85


class ExecutionSpec(BaseModel):
    entry: Literal["fvg_ce", "fvg_edge"] = "fvg_ce"
    stop_buffer_atr: float = 0.1
    stop_buffer_pips: float | None = None     # overrides the ATR buffer when set (ICT: 5 pips, 10 when new)
    entry_expiry_ny: str | None = None        # cancel an unfilled entry at this NY time (ICT: 11:30)
    target: TargetSpec = Field(default_factory=TargetSpec)
    entry_window_bars: int = 20
    max_trades_per_day: int = 2
    session_exit_ny: str | None = "16:00"
    contract: ContractSpec = Field(default_factory=ContractSpec)
    risk_per_trade_pct: float = 1.0


class StrategySchema(BaseModel):
    id: str
    version: int
    name: str
    model: str = "ICT 2022 Mentorship model"
    description: str = ""
    status: Literal["draft", "review", "approved"] = "draft"
    symbols: list[str] = Field(default_factory=lambda: ["frxEURUSD"])
    entry_granularity: int = 300
    scan: dict = Field(default_factory=dict)
    nodes: list[NodeSpec]
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    execution_citations: list[Citation] = Field(default_factory=list)
    jev_model_pin: str | None = None
    ensemble: dict = Field(default_factory=lambda: {"weights": {"jev": 0.6, "laya": 0.4}, "threshold": 0.6})
    sources: list[dict] = Field(default_factory=list)
    authored_by: str = ""
    changelog: list[str] = Field(default_factory=list)

    def scan_params(self) -> ScanParams:
        return ScanParams(**{k: v for k, v in self.scan.items() if k in ScanParams.__dataclass_fields__})


# ================================================================== storage
def load_file(path) -> StrategySchema:
    return StrategySchema.model_validate(json.loads(path.read_text(encoding="utf-8")))


def sync_files() -> list[dict]:
    """Register every strategies/*.json (id+version) in the DB; files are the source of truth."""
    out = []
    for path in sorted(config.STRATEGIES_DIR.glob("*.json")):
        schema = load_file(path)
        with db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO strategies(id, version, name, schema_json, created_at) "
                         "VALUES (?, ?, ?, ?, COALESCE((SELECT created_at FROM strategies "
                         "WHERE id=? AND version=?), ?))",
                         (schema.id, schema.version, schema.name, schema.model_dump_json(), schema.id,
                          schema.version, db.utc_now()))
        out.append({"id": schema.id, "version": schema.version, "file": path.name})
    return out


def get(strategy_id: str, version: int | None = None) -> StrategySchema:
    sync_files()
    with db.connect() as conn:
        sql = "SELECT schema_json FROM strategies WHERE id = ?"
        params: tuple = (strategy_id,)
        if version is not None:
            sql += " AND version = ?"
            params += (version,)
        row = db.row(conn, sql + " ORDER BY version DESC LIMIT 1", params)
    if row is None:
        raise KeyError(f"Unknown strategy {strategy_id} v{version or 'latest'}")
    return StrategySchema.model_validate_json(row["schema_json"])


def list_all() -> list[dict]:
    sync_files()
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT id, version, name, created_at, schema_json FROM strategies ORDER BY id, version")
    out = []
    for r in rows:
        s = json.loads(r.pop("schema_json"))
        out.append({**r, "status": s.get("status"), "symbols": s.get("symbols"), "nodes": len(s.get("nodes", [])),
                    "entry_granularity": s.get("entry_granularity")})
    return out


# ================================================================== runtime
PAIR_CURRENCIES = {"frxEURUSD": ["EUR", "USD"], "frxGBPUSD": ["GBP", "USD"], "frxXAUUSD": ["USD"],
                   "frxUSDJPY": ["USD", "JPY"], "OTC_NDX": ["USD"], "OTC_SPC": ["USD"], "OTC_DJI": ["USD"]}


@dataclass
class Context:
    """Everything a node may need, computed by code at the setup's armed bar."""
    symbol: str
    bars: F.Bars
    swings: list[F.Swing]
    levels: list[F.SessionLevels]
    atr: list[float]
    news_lookup: Callable[[int, int, list[str], int], list[dict]] | None = None


@dataclass
class Plan:
    direction: str
    entry: float
    stop: float
    target: float
    risk: float
    rr: float
    target_name: str


@dataclass
class NodeResult:
    id: str
    kind: str
    passed: bool
    value: Any = None
    detail: str = ""
    source: str = "code"  # code | jev | fallback


@dataclass
class Evaluation:
    setup: F.Setup
    plan: Plan | None
    results: list[NodeResult] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    report: dict | None = None  # ensemble confidence JSON (mode="ensemble")

    @property
    def passed(self) -> bool:
        ok = self.plan is not None and all(r.passed for r in self.results if not r.id.startswith("flag:"))
        return ok and (self.report is None or self.report["decision"] == "execute")


def entry_deadline(armed_epoch: int, ex: ExecutionSpec, granularity: int) -> int:
    """Last epoch an entry may fill: entry_window_bars, capped by entry_expiry_ny on the armed day."""
    deadline = armed_epoch + granularity * ex.entry_window_bars
    if ex.entry_expiry_ny:
        h, m = (int(x) for x in ex.entry_expiry_ny.split(":"))
        armed = F.ny_time(armed_epoch)
        cut = armed.replace(hour=h, minute=m, second=0, microsecond=0)
        deadline = min(deadline, int(cut.timestamp())) if armed < cut else armed_epoch  # armed after cut: no entry
    return deadline


def build_plan(setup: F.Setup, ctx: Context, ex: ExecutionSpec) -> Plan | None:
    long = setup.direction == "long"
    fvg = setup.fvg
    entry = fvg.ce if ex.entry == "fvg_ce" else (fvg.top if long else fvg.bottom)
    a = ctx.atr[setup.armed_at]
    buffer = ex.stop_buffer_pips * F.pip_size(ctx.symbol) if ex.stop_buffer_pips is not None else ex.stop_buffer_atr * a
    stop = setup.sweep_extreme - buffer if long else setup.sweep_extreme + buffer
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    name, target = "fixed_r", entry + ex.target.r * risk if long else entry - ex.target.r * risk
    if ex.target.mode == "liquidity":
        found = _opposing_liquidity(setup, ctx)
        if found:
            name, target = found
    rr = abs(target - entry) / risk
    if rr > ex.target.max_r:
        target, rr, name = (entry + ex.target.max_r * risk if long else entry - ex.target.max_r * risk,
                            ex.target.max_r, name + " (capped)")
    return Plan(setup.direction, round(entry, 6), round(stop, 6), round(target, 6), risk, round(rr, 3), name)


def _opposing_liquidity(setup: F.Setup, ctx: Context) -> tuple[str, float] | None:
    i, long = setup.armed_at, setup.direction == "long"
    beyond = setup.leg_high if long else setup.leg_low
    cands: list[tuple[str, float]] = []
    for s in ctx.swings:
        if s.confirmed_at > i or i - s.index > 480:
            continue
        if long and s.kind == "high" and s.price > beyond:
            cands.append(("swing_high", s.price))
        if not long and s.kind == "low" and s.price < beyond:
            cands.append(("swing_low", s.price))
    lv = ctx.levels[i] if ctx.levels else None
    if lv:
        for name, v in ((("pdh", lv.pdh), ("asian_high", lv.asian_high)) if long else
                        (("pdl", lv.pdl), ("asian_low", lv.asian_low))):
            if v is not None and ((long and v > beyond) or (not long and v < beyond)):
                cands.append((name, v))
    if not cands:
        return None
    return min(cands, key=lambda c: c[1]) if long else max(cands, key=lambda c: c[1])


# --------------------------------------------------------------- detectors
Detector = Callable[[F.Setup, Context, Plan, dict], NodeResult]
DETECTORS: dict[str, Detector] = {}


def detector(name: str):
    def wrap(fn: Detector) -> Detector:
        DETECTORS[name] = fn
        return fn
    return wrap


@detector("killzone")
def _killzone(setup, ctx, plan, p):
    is_index = ctx.symbol.startswith("OTC_")
    zones = p.get("index_zones", ["ny_am_index"]) if is_index else p.get("zones", ["ny_am"])
    at = p.get("at", "mss")
    idx = {"sweep": [setup.sweep_index], "mss": [setup.mss_index],
           "either": [setup.sweep_index, setup.mss_index]}[at]
    hits = [F.killzone_of(ctx.bars.t[i], zones) for i in idx]
    zone = next((z for z in hits if z), None)
    return NodeResult("killzone", "code", zone is not None, zone,
                      f"{at} at {F.ny_time(ctx.bars.t[idx[-1]]):%H:%M} NY" + (f" in {zone}" if zone else ""))


@detector("liquidity_source")
def _liquidity_source(setup, ctx, plan, p):
    allowed = p.get("allowed")
    ok = allowed is None or any(setup.sweep_level_name.startswith(a) for a in allowed)
    return NodeResult("liquidity_source", "code", ok, setup.sweep_level_name, f"raided {setup.sweep_level_name}")


@detector("premium_discount")
def _premium_discount(setup, ctx, plan, p):
    """Entry position inside a dealing range: 0 = range low, 1 = range high.

    range="previous_day" (v2+): the prior NY trading day's high/low, i.e. the higher-timeframe dealing
    range ICT applies equilibrium to (Ep. 10 @4:09). range="leg" (v1): the displacement leg itself."""
    mode = p.get("range", "leg")
    lo, hi, label = setup.leg_low, setup.leg_high, "leg"
    if mode == "previous_day":
        lv = ctx.levels[setup.armed_at] if ctx.levels else None
        if lv is None or lv.pdh is None or lv.pdl is None:
            return NodeResult("premium_discount", "code", True, None, "no previous-day range (not filtered)")
        lo, hi, label = lv.pdl, lv.pdh, "previous-day range"
    span = hi - lo or 1e-12
    pos = (plan.entry - lo) / span
    limit = p.get("max_depth", 0.5)
    ok = pos <= limit if setup.direction == "long" else pos >= 1 - limit
    zone = "discount" if pos < 0.5 else "premium"
    return NodeResult("premium_discount", "code", ok, round(pos, 3), f"entry at {pos:.0%} of the {label} ({zone})")


@detector("min_rr")
def _min_rr(setup, ctx, plan, p):
    need = p.get("min_rr", 1.5)
    return NodeResult("min_rr", "code", plan.rr >= need, plan.rr, f"R:R {plan.rr:.2f} to {plan.target_name}")


@detector("opening_price")
def _opening_price(setup, ctx, plan, p):
    """ICT daily bias vs the opening price: shorts sell above the NY midnight open (premium of the day),
    longs buy below it (Ep. 10 @17:59, Ep. 16 @15:24)."""
    lv = ctx.levels[setup.armed_at] if ctx.levels else None
    if lv is None or lv.midnight_open is None:
        return NodeResult("opening_price", "code", p.get("pass_if_unknown", False), None,
                          "midnight open not yet known (setup before 00:00 NY)")
    long = setup.direction == "long"
    ok = plan.entry < lv.midnight_open if long else plan.entry > lv.midnight_open
    side = "below" if plan.entry < lv.midnight_open else "above"
    return NodeResult("opening_price", "code", ok, round(plan.entry - lv.midnight_open, 6),
                      f"entry {side} the NY midnight open")


@detector("cost_budget")
def _cost_budget(setup, ctx, plan, p):
    """Execution realism (not ICT doctrine): spread + commission must not eat too much of 1R."""
    cost = float(p.get("_cost") or 0.0)
    cost_r = cost / plan.risk if plan.risk else float("inf")
    limit = p.get("max_cost_r", 0.25)
    return NodeResult("cost_budget", "code", cost_r <= limit, round(cost_r, 3),
                      f"costs are {cost_r:.0%} of the risk (limit {limit:.0%})")


@detector("news_filter")
def _news_filter(setup, ctx, plan, p):
    if ctx.news_lookup is None:
        return NodeResult("news_filter", "code", True, None, "no calendar available (not filtered)")
    minutes = p.get("minutes", 30)
    t = ctx.bars.t[setup.armed_at]
    hits = ctx.news_lookup(t - minutes * 60, t + minutes * 60, PAIR_CURRENCIES.get(ctx.symbol, []),
                           p.get("min_importance", 3))
    summary = "; ".join(f"{h['currency']} {h['title']}" for h in hits[:3])
    return NodeResult("news_filter", "code", not hits, len(hits), summary or f"no high-impact news ±{minutes}m")


@detector("displacement_rule")
def _displacement_rule(setup, ctx, plan, p):
    """Code fallback for the Jev displacement judgment: normalised body/ATR strength."""
    value = max(0.0, min(1.0, (setup.displacement_body_atr - 0.5) / 2.5))
    need = p.get("min", 0.3)
    return NodeResult("displacement_quality", "code", value >= need, round(value, 3),
                      f"largest body {setup.displacement_body_atr:.2f}× ATR")


@detector("htf_bias_rule")
def _htf_bias_rule(setup, ctx, plan, p):
    """Code fallback for the Jev draw-on-liquidity judgment.

    mode="prev_close" (v1): follow the previous day's close direction.
    mode="unswept_opposing" (v2): the draw is the opposing previous-day extreme that has not
    traded yet today (buy-side above for longs, sell-side below for shorts)."""
    i = setup.armed_at
    lv = ctx.levels[i] if ctx.levels else None
    if lv is None or lv.pdh is None or lv.pdl is None:
        return NodeResult("htf_draw", "code", True, "unknown", "no previous-day data (not filtered)")
    want = "buy_side_above" if setup.direction == "long" else "sell_side_below"
    price = ctx.bars.c[i]
    if p.get("mode", "prev_close") == "unswept_opposing":
        hi, lo = F.today_extremes(ctx.bars, i)
        if setup.direction == "long":
            ok = hi < lv.pdh and price < lv.pdh
            detail = "previous day high still resting above" if ok else "previous day high already taken or below"
        else:
            ok = lo > lv.pdl and price > lv.pdl
            detail = "previous day low still resting below" if ok else "previous day low already taken or above"
        return NodeResult("htf_draw", "code", ok, want if ok else "unclear", detail)
    facts = F.daily_bias_facts(lv, price)
    bullish_day = facts["previous_day_closed"].startswith("bullish")
    draw = "buy_side_above" if bullish_day else "sell_side_below"
    return NodeResult("htf_draw", "code", draw == want, draw, f"previous day {facts['previous_day_closed']}")


# ------------------------------------------------------------ jev state
def setup_facts(setup: F.Setup, ctx: Context, plan: Plan, granularity: int) -> dict:
    """Plain-language facts for Jev. Code has already done every calculation."""
    b, long = ctx.bars, setup.direction == "long"
    leg = range(setup.sweep_index, setup.armed_at + 1)
    closes_near_extreme = sum(
        1 for j in leg if (b.h[j] - b.l[j]) > 0 and (
            (b.c[j] - b.l[j]) / (b.h[j] - b.l[j]) >= 0.7 if long else (b.h[j] - b.c[j]) / (b.h[j] - b.l[j]) >= 0.7))
    lv = ctx.levels[setup.armed_at] if ctx.levels else None
    level_names = {"pdl": "the previous day low", "pdh": "the previous day high", "asian_low": "the Asian session low",
                   "asian_high": "the Asian session high", "swing_low": "a recent swing low (sell stops)",
                   "swing_high": "a recent swing high (buy stops)"}
    return {
        "instrument": ctx.symbol.removeprefix("frx").removeprefix("OTC_"),
        "timeframe": f"{granularity // 60}-minute candles",
        "direction_under_consideration": "long (buy)" if long else "short (sell)",
        "time": f"{F.ny_time(b.t[setup.mss_index]):%H:%M} New York time"
                + (f" ({F.killzone_of(b.t[setup.mss_index])} session)" if F.killzone_of(b.t[setup.mss_index]) else ""),
        "liquidity_raid": f"Price traded {'below' if long else 'above'} "
                          f"{level_names.get(setup.sweep_level_name, 'a level')}, "
                          f"taking {'sell' if long else 'buy'}-side liquidity, then reversed.",
        "displacement": {
            "candles_from_raid_to_structure_break": setup.mss_index - setup.sweep_index,
            "largest_candle_body_vs_average_true_range": f"{setup.displacement_body_atr:.1f} times",
            "large_candles_in_the_move": setup.displacement_bars,
            "candles_closing_near_their_extreme_in_the_move_direction": f"{closes_near_extreme} of {len(leg)}",
            "structure_break": "a candle closed " + ("above the last short-term swing high" if long
                                                     else "below the last short-term swing low"),
        },
        "fair_value_gap": f"A {'bullish' if long else 'bearish'} fair value gap was left inside the move; "
                          f"the planned entry is in the {'lower' if long else 'upper'} part of the move "
                          f"({'discount' if long else 'premium'} side)",
        "daily_context": (F.daily_bias_facts(lv, b.c[setup.armed_at], F.today_extremes(b, setup.armed_at))
                          if lv else {"known": False}),
        "planned_target": f"{plan.target_name.replace('_', ' ')}, a reward of {plan.rr:.1f} times the risk",
    }


def _gate_jev(node: NodeSpec, answer: dict, direction: str) -> tuple[bool, Any, str]:
    g = node.gate
    if answer["type"] == "noul":
        p = answer["noul"]
        return p >= g.get("min", 0.5), p, f"P(yes)={p:.2f}"
    if answer["type"] == "score":
        v = answer["score_norm"]
        return v >= g.get("min", 0.5), v, f"score {v:.2f} (confidence {answer['confidence']:.2f})"
    choice, probs = answer["choice"], answer["probabilities"]
    want = g.get("expect", {}).get(direction)
    if want:
        p = probs.get(want, 0.0)
        return p >= g.get("min_probability", 0.5), choice, f"{choice}; P({want})={p:.2f}"
    return answer["confidence"] >= g.get("min_confidence", 0.0), choice, f"{choice} ({answer['confidence']:.2f})"


class Runtime:
    """Evaluates setups against a schema.

    mode: 'code' (fallback rules only) | 'jev' | 'laya' | 'ensemble' (every question phrasing,
    answered by Jev and Laya, aggregated into a confidence JSON; see ensemble.py)."""

    JUDGE_MODES = ("jev", "laya", "ensemble")

    def __init__(self, schema: StrategySchema, mode: Literal["code", "jev", "laya", "ensemble"] = "code", *,
                 cost: float = 0.0) -> None:
        self.schema = schema
        self.mode = mode
        self.cost = cost  # round-trip cost in price units, for the cost_budget node
        self.jev = self.laya = None
        if mode in ("jev", "ensemble"):
            from app.services.jev.client import JevClient
            self.jev = JevClient(schema.jev_model_pin or None)
        if mode in ("laya", "ensemble"):
            from app.services.laya_client import LayaClient
            self.laya = LayaClient()
        self.judge = self.jev if mode == "jev" else self.laya if mode == "laya" else None

    @property
    def available(self) -> bool:
        if self.mode == "code":
            return True
        if self.mode == "ensemble":
            return bool((self.jev and self.jev.available) or (self.laya and self.laya.available))
        return bool(self.judge and self.judge.available)

    def precheck(self, setup: F.Setup, ctx: Context) -> tuple[Evaluation, list[NodeSpec]]:
        """Run code nodes; return the evaluation so far and the Jev nodes still to ask."""
        plan = build_plan(setup, ctx, self.schema.execution)
        ev = Evaluation(setup, plan)
        pending: list[NodeSpec] = []
        if plan is None:
            return ev, pending
        # The plan must still be tradeable when the setup is armed: target beyond current price.
        close = ctx.bars.c[setup.armed_at]
        beyond = plan.target > close if setup.direction == "long" else plan.target < close
        ev.results.append(NodeResult("plan", "code", beyond, plan.target_name,
                                     "target is beyond the price at arming" if beyond
                                     else "target already reached before the setup armed"))
        if not beyond:
            return ev, pending
        for node in self.schema.nodes:
            if node.kind == "trigger":
                continue
            if node.kind == "jev" and self.mode in self.JUDGE_MODES:
                pending.append(node)
                continue
            name = node.detector if node.kind == "code" else node.fallback
            if not name:
                continue
            res = DETECTORS[name](setup, ctx, plan, node.params | node.gate | {"_cost": self.cost})
            res.id, res.source = node.id, ("code" if node.kind == "code" else "fallback")
            self._record(ev, node, res)
            if not res.passed and node.on_fail == "reject":
                return ev, []  # short-circuit: no Jev spend on rejected setups
        return ev, pending

    def jev_questions(self, pending: list[NodeSpec]) -> dict[str, dict]:
        return {n.id: n.question for n in pending if n.question}

    def finalize(self, ev: Evaluation, pending: list[NodeSpec], answers: dict[str, dict]) -> Evaluation:
        for node in pending:
            ans = answers.get(node.id)
            if ans is None:
                res = NodeResult(node.id, "jev", False, None, "no Jev answer", "jev")
            else:
                ok, value, detail = _gate_jev(node, ans, ev.setup.direction)
                res = NodeResult(node.id, "jev", ok, value, detail, "jev")
            self._record(ev, node, res)
        return ev

    @staticmethod
    def _record(ev: Evaluation, node: NodeSpec, res: NodeResult) -> None:
        if node.on_fail == "flag":
            if not res.passed:
                ev.flags.append(node.id)
            res.id = f"flag:{node.id}"
        ev.results.append(res)

    async def evaluate(self, setup: F.Setup, ctx: Context, *, sdk: Any = None) -> Evaluation:
        ev, pending = self.precheck(setup, ctx)
        if not pending or ev.plan is None:
            return ev
        state = setup_facts(setup, ctx, ev.plan, self.schema.entry_granularity)
        if self.mode != "ensemble":
            kwargs = {"sdk": sdk} if self.mode == "jev" else {}
            result = await self.judge.aask(state, self.jev_questions(pending), **kwargs)
            return self.finalize(ev, pending, result.answers)
        from app.services.ict import ensemble
        cfg = self.schema.ensemble or {}
        report = await ensemble.validate(
            state, pending, setup.direction, {"jev": self.jev, "laya": self.laya},
            weights=cfg.get("weights"), threshold=float(cfg.get("threshold", 0.6)),
            schemas_per_judge=cfg.get("schemas_per_judge"),
            code_results=[{"id": r.id, "passed": r.passed, "value": r.value, "detail": r.detail} for r in ev.results],
            plan={"entry": ev.plan.entry, "stop": ev.plan.stop, "target": ev.plan.target, "rr": ev.plan.rr,
                  "target_name": ev.plan.target_name},
            meta={"strategy": {"id": self.schema.id, "version": self.schema.version}, "symbol": ctx.symbol,
                  "armed_at": ctx.bars.t[setup.armed_at]})
        for node in pending:
            nr = report["nodes"].get(node.id, {})
            self._record(ev, node, NodeResult(node.id, "jev", bool(nr.get("passed")), nr.get("mean"),
                                              f"ensemble mean {nr.get('mean')} over {nr.get('n', 0)} answers "
                                              f"(spread {nr.get('spread')})", "ensemble"))
        ev.report = report
        return ev


def mermaid(schema: StrategySchema) -> str:
    """Flowchart of the decision graph (renders on GitHub and in VS Code)."""
    lines = ["flowchart TD", f'  start(["{schema.entry_granularity // 60}m candle closes"])']
    prev = "start"
    shape = {"trigger": ('[/"', '"/]'), "code": ('["', '"]'), "jev": ('{{"', '"}}')}
    for n in schema.nodes:
        o, c = shape[n.kind]
        tag = {"trigger": "TRIGGER", "code": "CODE", "jev": "JEV " + (n.question or {}).get("type", "").upper()}[n.kind]
        gate = ""
        if n.kind == "jev" and n.gate:
            gate = "<br/>gate: " + ", ".join(f"{k} {v}" for k, v in n.gate.items() if k != "expect")
        label = f"<b>{tag}</b> {n.label}{gate}".replace('"', "'")
        lines.append(f"  {n.id}{o}{label}{c}")
        lines.append(f"  {prev} --> {n.id}")
        if n.on_fail == "reject" and n.kind != "trigger":
            lines.append(f"  {n.id} -. fail .-> skip([no trade])")
        prev = n.id
    ex = schema.execution
    lines += [f'  plan["<b>PLAN</b> entry {ex.entry} · stop beyond raid + {ex.stop_buffer_atr} ATR · '
              f'target {ex.target.mode} (min {ex.target.min_rr}R, cap {ex.target.max_r}R)"]',
              f"  {prev} --> plan",
              '  risk{"<b>RISK GATE</b> demo lock · stake · daily loss · kill switch"}',
              "  plan --> risk", f'  risk --> order(["{ex.contract.type} order on Deriv (demo)"])',
              "  risk -. blocked .-> skip",
              "  classDef jev fill:#efe7ff,stroke:#7c4dff", "  classDef code fill:#e7f3ff,stroke:#2f7ed8",
              "  classDef trig fill:#fff4e0,stroke:#e08a00"]
    for n in schema.nodes:
        lines.append(f"  class {n.id} {'jev' if n.kind == 'jev' else 'trig' if n.kind == 'trigger' else 'code'}")
    return "\n".join(lines)


def graph(schema: StrategySchema) -> dict:
    """Node/edge list for the UI's native SVG renderer."""
    nodes = [{"id": "start", "kind": "start", "label": f"{schema.entry_granularity // 60}m candle closes"}]
    edges = []
    prev = "start"
    for n in schema.nodes:
        nodes.append({"id": n.id, "kind": n.kind, "label": n.label, "description": n.description,
                      "question_type": (n.question or {}).get("type"), "gate": n.gate, "on_fail": n.on_fail,
                      "citations": [c.model_dump() for c in n.citations]})
        edges.append({"from": prev, "to": n.id, "kind": "pass"})
        prev = n.id
    nodes += [{"id": "plan", "kind": "plan", "label": "Entry / stop / target"},
              {"id": "risk", "kind": "risk", "label": "Risk gate (demo lock, limits)"},
              {"id": "order", "kind": "order", "label": f"{schema.execution.contract.type} order (demo)"}]
    edges += [{"from": prev, "to": "plan", "kind": "pass"}, {"from": "plan", "to": "risk", "kind": "pass"},
              {"from": "risk", "to": "order", "kind": "pass"}]
    return {"nodes": nodes, "edges": edges}
