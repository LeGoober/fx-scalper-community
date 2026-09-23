"""Live engine: the backtested strategy running on streaming Deriv candles.

Modes
  paper : no broker. Fills and exits are simulated on live ticks with the backtest's rules.
  demo  : Deriv demo account. Refuses to start unless the authorised account is virtual,
          and every order still passes risk.check_order() (fail-closed demo lock).
There is no 'real' mode here. Real money would need the .env unlock plus a per-session
arm, and even then risk.check_order() is the gate.

Flow per symbol: subscribe to 1m candles → when an entry-timeframe bar closes, scan the
trailing window → evaluate new setups (code, or code+Jev) → signal → pending entry at the
FVG level → on a tick through the level: paper fill, or a Deriv market order (multiplier
with stop-loss/take-profit amounts derived from the planned levels) → track to exit.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app import db, events
from app.services import risk
from app.services.deriv import history, profile
from app.services.deriv.client import DerivClient, DerivError
from app.services.ict import features as F
from app.services.ict import strategy as S
from app.services.ict.scanner import Scanner

log = logging.getLogger("fxs.engine")

WARMUP_DAYS = 4
MIN_STAKE = 1.0


class EngineConfig(BaseModel):
    strategy_id: str = "ict_2022_model"
    version: int | None = None
    symbols: list[str] = Field(default_factory=lambda: ["frxEURUSD"])
    mode: Literal["paper", "demo"] = "paper"
    evaluation: Literal["code", "jev"] = "code"
    risk_amount: float = Field(1.0, gt=0, description="Account currency lost at the planned stop (1R)")
    currency: str = "USD"


@dataclass
class Pending:
    id: str
    symbol: str
    signal_id: str
    plan: S.Plan
    expires_at: int
    created_at: int


@dataclass
class Position:
    trade_id: str
    symbol: str
    direction: str
    entry: float
    stop: float
    target: float
    risk_amount: float
    opened_at: int
    exit_at: float | None
    contract_id: str | None = None
    mode: str = "paper"


@dataclass
class SymbolState:
    symbol: str
    bars: list[dict] = field(default_factory=list)   # closed 1m bars
    live: dict | None = None                         # forming 1m bar
    last_price: float | None = None
    last_tick: int | None = None
    pending: list[Pending] = field(default_factory=list)
    position: Position | None = None
    trades_today: dict[str, int] = field(default_factory=dict)
    setups_seen: set = field(default_factory=set)
    scans: int = 0                                   # entry-timeframe bar closes scanned
    last_scan: int | None = None
    evaluations: int = 0
    signals: int = 0
    errors: int = 0
    last_error: str | None = None


class LiveEngine:
    def __init__(self) -> None:
        self.cfg: EngineConfig | None = None
        self.schema: S.StrategySchema | None = None
        self.runtime: S.Runtime | None = None
        self.states: dict[str, SymbolState] = {}
        self.tasks: list[asyncio.Task] = []
        self.feed: DerivClient | None = None
        self.broker: DerivClient | None = None
        self.started_at: str | None = None
        self.stopped_reason: str | None = None

    # ------------------------------------------------------------ control
    @property
    def running(self) -> bool:
        return any(not t.done() for t in self.tasks)

    async def start(self, cfg: EngineConfig) -> dict:
        if self.running:
            raise RuntimeError("Engine is already running; stop it first.")
        if risk.kill_switch_active():
            raise risk.RiskBlocked("Kill switch is engaged; release it before starting the engine.")
        self.cfg = cfg
        self.schema = S.get(cfg.strategy_id, cfg.version)
        self.runtime = S.Runtime(self.schema, cfg.evaluation, cost=0.0)
        if cfg.evaluation == "jev" and not self.runtime.jev.available:
            raise RuntimeError("evaluation='jev' needs TYPESAFE_API_KEY.")
        if cfg.mode == "demo":
            self.broker = DerivClient()
            if not self.broker.token:
                raise RuntimeError("Demo mode needs a Deriv API token (Settings → API keys).")
            info = await self.broker.connect()
            if self.broker.is_virtual is not True:
                await self.broker.close()
                self.broker = None
                raise risk.RiskBlocked(f"Refusing to start: account {info.get('loginid')} is not verified as demo.")
            for sym in cfg.symbols:
                prof = profile.get(sym)
                if not prof:
                    await profile.calibrate([sym], self.schema.execution.contract.rise_fall_minutes)
        self.states = {s: SymbolState(s) for s in cfg.symbols}
        self.feed = DerivClient(token="")
        await self.feed.connect(authorize=False)
        self.started_at, self.stopped_reason = db.utc_now(), None
        self.tasks = [asyncio.create_task(self._run_symbol(s), name=f"engine:{s}") for s in cfg.symbols]
        events.publish("engine.started", self.status(), message=f"Engine started ({cfg.mode}, {cfg.evaluation})")
        return self.status()

    async def stop(self, reason: str = "stopped") -> dict:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.tasks = []
        for client in (self.feed, self.broker):
            if client:
                await client.close()
        self.feed = self.broker = None
        cancelled = 0
        for st in self.states.values():
            cancelled += len(st.pending)
            st.pending.clear()
        if self.started_at:
            self.stopped_reason = reason
            events.publish("engine.stopped", {"reason": reason, "cancelled_pending": cancelled},
                           message=f"Engine stopped: {reason}")
        return self.status()

    def status(self) -> dict:
        return {
            "running": self.running, "started_at": self.started_at, "stopped_reason": self.stopped_reason,
            "config": self.cfg.model_dump() if self.cfg else None,
            "strategy": {"id": self.schema.id, "version": self.schema.version} if self.schema else None,
            "feed_rtt_ms": self.feed.last_rtt_ms if self.feed else None,
            "broker": self.broker.account_info() if self.broker else None,
            "symbols": [{
                "symbol": st.symbol, "bars": len(st.bars), "last_price": st.last_price, "last_tick": st.last_tick,
                "pending": [{"id": p.id, "direction": p.plan.direction, "entry": p.plan.entry, "stop": p.plan.stop,
                             "target": p.plan.target, "expires_at": p.expires_at} for p in st.pending],
                "position": asdict(st.position) if st.position else None,
                "scans": st.scans, "last_scan": st.last_scan,
                "evaluations": st.evaluations, "signals": st.signals, "errors": st.errors,
                "last_error": st.last_error} for st in self.states.values()],
        }

    # --------------------------------------------------------------- feed
    async def _run_symbol(self, symbol: str) -> None:
        st = self.states[symbol]
        backoff = 2
        now = int(time.time())
        await history.backfill(symbol, 60, now - WARMUP_DAYS * 86400, now, client=self.feed)
        st.bars = history.load(symbol, 60, now - WARMUP_DAYS * 86400)
        while True:
            try:
                if self.feed is None or not self.feed.connected:
                    self.feed = DerivClient(token="")
                    await self.feed.connect(authorize=False)
                sub = await self.feed.subscribe({"ticks_history": symbol, "style": "candles", "granularity": 60,
                                                 "end": "latest", "count": 60})
                async for message in sub:
                    await self._on_message(st, message)
                    backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                st.errors += 1
                st.last_error = f"{type(exc).__name__}: {exc}"
                events.publish("engine.feed_error", {"symbol": symbol, "error": st.last_error}, level="warning",
                               message=f"{symbol} feed error; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _on_message(self, st: SymbolState, message: dict) -> None:
        if message.get("msg_type") == "candles":
            for c in message.get("candles") or []:
                self._append_closed(st, c)
            return
        ohlc = message.get("ohlc")
        if not ohlc:
            return
        bar = {"epoch": int(ohlc["open_time"]), "open": float(ohlc["open"]), "high": float(ohlc["high"]),
               "low": float(ohlc["low"]), "close": float(ohlc["close"])}
        st.last_price, st.last_tick = bar["close"], int(ohlc["epoch"])
        if st.live and st.live["epoch"] != bar["epoch"]:
            closed = st.live
            self._append_closed(st, closed)
            await asyncio.to_thread(history.upsert, st.symbol, 60, [closed])
            if (closed["epoch"] + 60) % self.schema.entry_granularity == 0:
                await self._evaluate(st)
        st.live = bar
        await self._on_tick(st, st.last_price, st.last_tick)

    @staticmethod
    def _append_closed(st: SymbolState, candle: dict) -> None:
        c = {k: (int(candle[k]) if k == "epoch" else float(candle[k]))
             for k in ("epoch", "open", "high", "low", "close")}
        if st.bars and c["epoch"] <= st.bars[-1]["epoch"]:
            if c["epoch"] == st.bars[-1]["epoch"]:
                st.bars[-1] = c
            return
        st.bars.append(c)
        cutoff = c["epoch"] - WARMUP_DAYS * 86400
        while st.bars and st.bars[0]["epoch"] < cutoff:
            st.bars.pop(0)

    # ---------------------------------------------------------- strategy
    async def _evaluate(self, st: SymbolState) -> None:
        g = self.schema.entry_granularity
        candles = history.resample(st.bars, g) if g > 60 else list(st.bars)
        if len(candles) < 50:
            return
        bars = F.Bars.from_candles(candles, g)
        st.scans += 1
        st.last_scan = bars.t[-1]
        scanner = Scanner(bars, self.schema.scan_params())
        from app.services.backtest.engine import _dedupe, _news_lookup
        fresh = [s for s in _dedupe(scanner.run()) if s.armed_at == len(bars) - 1]
        if not fresh:
            return
        ctx = S.Context(st.symbol, bars, scanner.swings, scanner.levels, scanner.atr, _news_lookup)
        self.runtime.cost = self._cost(st.symbol)
        for setup in fresh:
            key = (setup.direction, bars.t[setup.mss_index])
            if key in st.setups_seen:
                continue
            st.setups_seen.add(key)
            st.evaluations += 1
            ev = await self.runtime.evaluate(setup, ctx)
            self._record_signal(st, ev, bars)

    def _cost(self, symbol: str) -> float:
        from app.services.backtest.engine import DEFAULT_COST
        return float(profile.get(symbol).get("cost_price") or DEFAULT_COST.get(symbol, 0.0))

    def _record_signal(self, st: SymbolState, ev: S.Evaluation, bars: F.Bars) -> None:
        signal_id = f"sig-{uuid.uuid4().hex[:10]}"
        decision = {"passed": ev.passed, "flags": ev.flags,
                    "plan": asdict(ev.plan) if ev.plan else None,
                    "nodes": [asdict(r) for r in ev.results],
                    "setup": {"sweep": ev.setup.sweep_level_name, "armed_at": bars.t[ev.setup.armed_at],
                              "killzone": F.killzone_of(bars.t[ev.setup.mss_index])}}
        status = "accepted" if ev.passed else "rejected"
        with db.connect() as conn:
            conn.execute("INSERT INTO signals(id, created_at, symbol, direction, strategy_id, strategy_version, "
                         "source, status, decision_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         (signal_id, db.utc_now(), st.symbol, ev.setup.direction, self.schema.id, self.schema.version,
                          f"engine:{self.cfg.evaluation}", status, db.dumps(decision)))
        events.publish("signal.new", {"id": signal_id, "symbol": st.symbol, "direction": ev.setup.direction,
                                      "status": status, "plan": decision["plan"], "flags": ev.flags},
                       message=f"{st.symbol} {ev.setup.direction} setup {status}")
        if not ev.passed:
            return
        st.signals += 1
        ex = self.schema.execution
        day = F.ny_trading_day(bars.t[-1])
        if st.position or st.pending or st.trades_today.get(day, 0) >= ex.max_trades_per_day:
            return
        expires = S.entry_deadline(bars.t[-1] + self.schema.entry_granularity, ex, self.schema.entry_granularity)
        st.pending.append(Pending(f"po-{uuid.uuid4().hex[:8]}", st.symbol, signal_id, ev.plan, expires, bars.t[-1]))

    # --------------------------------------------------------- execution
    async def _on_tick(self, st: SymbolState, price: float, ts: int) -> None:
        for p in list(st.pending):
            long = p.plan.direction == "long"
            if ts > p.expires_at or (price >= p.plan.target if long else price <= p.plan.target):
                st.pending.remove(p)
                events.publish("signal.expired", {"id": p.signal_id, "symbol": st.symbol},
                               message=f"{st.symbol} entry not reached; cancelled")
                continue
            if (price <= p.plan.entry) if long else (price >= p.plan.entry):
                st.pending.remove(p)
                await self._open(st, p, price, ts)
        pos = st.position
        if pos and pos.mode == "paper":
            long = pos.direction == "long"
            hit_stop = price <= pos.stop if long else price >= pos.stop
            hit_target = price >= pos.target if long else price <= pos.target
            if hit_stop or hit_target or (pos.exit_at and ts >= pos.exit_at):
                exit_px = pos.stop if hit_stop else pos.target if hit_target else price
                reason = "stop" if hit_stop else "target" if hit_target else "session_exit"
                await self._close_paper(st, exit_px, reason)

    def _session_exit_ts(self, ts: int) -> float | None:
        cut = self.schema.execution.session_exit_ny
        if not cut:
            return None
        h, m = (int(x) for x in cut.split(":"))
        now = F.ny_time(ts)
        exit_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now >= exit_dt:
            from datetime import timedelta
            exit_dt += timedelta(days=1)
        return exit_dt.timestamp()

    async def _open(self, st: SymbolState, p: Pending, price: float, ts: int) -> None:
        cfg = self.cfg
        trade_id = f"t-{uuid.uuid4().hex[:10]}"
        day = F.ny_trading_day(ts)
        st.trades_today[day] = st.trades_today.get(day, 0) + 1
        fill = price if (price < p.plan.entry if p.plan.direction == "long" else price > p.plan.entry) else p.plan.entry
        pos = Position(trade_id, st.symbol, p.plan.direction, fill, p.plan.stop, p.plan.target, cfg.risk_amount, ts,
                       self._session_exit_ts(ts), mode=cfg.mode)
        meta = {"signal_id": p.signal_id, "planned_entry": p.plan.entry, "rr_planned": p.plan.rr}
        if cfg.mode == "demo":
            try:
                order = await self._demo_order(st.symbol, pos)
            except (risk.RiskBlocked, DerivError, ValueError) as exc:
                events.publish("trade.rejected", {"symbol": st.symbol, "reason": str(exc)}, level="warning",
                               message=f"{st.symbol} order not placed: {exc}")
                return
            pos.contract_id = str(order["contract_id"])
            pos.entry = order.get("entry_spot") or pos.entry
            meta.update(order)
            asyncio.create_task(self._track_contract(st, pos))
        st.position = pos
        with db.connect() as conn:
            conn.execute("INSERT INTO trades(id, mode, symbol, direction, contract_type, stake, entry_price, "
                         "stop_price, target_price, status, opened_at, contract_id, signal_id, strategy_id, meta_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?,?)",
                         (trade_id, cfg.mode, st.symbol, pos.direction, self.schema.execution.contract.type,
                          meta.get("stake"), pos.entry, pos.stop, pos.target, db.utc_now(), pos.contract_id,
                          p.signal_id, self.schema.id, db.dumps(meta)))
        events.publish("trade.opened", {"id": trade_id, "mode": cfg.mode, "symbol": st.symbol,
                                        "direction": pos.direction, "entry": pos.entry, "stop": pos.stop,
                                        "target": pos.target, "contract_id": pos.contract_id},
                       message=f"{cfg.mode.upper()} {pos.direction} {st.symbol} @ {pos.entry}")
        try:
            from app.services import notify
            await asyncio.to_thread(notify.notify_trade_opened, st.symbol, "CALL" if pos.direction == "long" else "PUT",
                                    float(meta.get("stake") or cfg.risk_amount), float(pos.entry), cfg.mode)
        except Exception:
            pass

    async def _close_paper(self, st: SymbolState, exit_px: float, reason: str) -> None:
        pos = st.position
        risk_px = abs(pos.entry - pos.stop) or 1e-12
        gross = ((exit_px - pos.entry) if pos.direction == "long" else (pos.entry - exit_px)) / risk_px
        r = gross - self._cost(st.symbol) / risk_px
        await self._finish(st, pos, exit_px, r, round(r * pos.risk_amount, 2), reason)

    async def _finish(self, st: SymbolState, pos: Position, exit_px: float | None, r: float, pnl: float,
                      reason: str) -> None:
        st.position = None
        with db.connect() as conn:
            conn.execute("UPDATE trades SET status='closed', exit_price=?, r_multiple=?, pnl=?, closed_at=?, "
                         "meta_json=json_set(COALESCE(meta_json,'{}'), '$.exit_reason', ?) WHERE id=?",
                         (exit_px, round(r, 4), pnl, db.utc_now(), reason, pos.trade_id))
        events.publish("trade.closed", {"id": pos.trade_id, "mode": pos.mode, "symbol": st.symbol, "r": round(r, 3),
                                        "pnl": pnl, "reason": reason},
                       message=f"{pos.mode.upper()} {st.symbol} closed {r:+.2f}R ({reason})")
        try:
            from app.services import notify
            await asyncio.to_thread(notify.notify_trade_closed, st.symbol,
                                    "CALL" if pos.direction == "long" else "PUT", pnl, reason)
        except Exception:
            pass

    # --------------------------------------------------------- Deriv demo
    def _choose_multiplier(self, symbol: str, entry: float, stop: float) -> tuple[int, float]:
        accepted = profile.get(symbol).get("multipliers") or []
        if not accepted:
            raise ValueError(f"{symbol} offers no multipliers on Deriv; use a Rise/Fall strategy variant.")
        frac = abs(entry - stop) / entry
        usable = [m for m in accepted if frac < 0.8 / m]  # our stop must sit inside Deriv's stop-out
        if not usable:
            raise ValueError("Stop is too wide for any accepted multiplier.")
        m = max(usable)
        stake = max(MIN_STAKE, math.ceil(self.cfg.risk_amount / (m * frac) * 100) / 100)
        return m, stake

    async def _demo_order(self, symbol: str, pos: Position) -> dict:
        if self.broker is None or not self.broker.connected:
            self.broker = DerivClient()
            await self.broker.connect()
        contract = self.schema.execution.contract
        long = pos.direction == "long"
        if contract.type == "rise_fall":
            stake = max(MIN_STAKE, round(self.cfg.risk_amount, 2))
            fields = {"amount": stake, "basis": "stake", "contract_type": "CALL" if long else "PUT",
                      "currency": self.cfg.currency, "duration": contract.rise_fall_minutes, "duration_unit": "m",
                      "underlying_symbol": symbol}
            multiplier = None
        else:
            multiplier, stake = self._choose_multiplier(symbol, pos.entry, pos.stop)
            rr = abs(pos.target - pos.entry) / abs(pos.entry - pos.stop)
            fields = {"amount": stake, "basis": "stake", "contract_type": "MULTUP" if long else "MULTDOWN",
                      "currency": self.cfg.currency, "multiplier": multiplier, "underlying_symbol": symbol,
                      "limit_order": {"stop_loss": round(self.cfg.risk_amount, 2),
                                      "take_profit": round(self.cfg.risk_amount * rr, 2)}}
        proposal = await self.broker.proposal(**fields)
        ask = float(proposal.get("ask_price") or stake)
        risk.check_order(risk.OrderIntent(symbol=symbol, stake=ask, contract_type=fields["contract_type"],
                                          is_virtual=self.broker.is_virtual, risk_amount=self.cfg.risk_amount))
        bought = await self.broker.buy(proposal["id"], ask)
        return {"contract_id": bought.get("contract_id"), "stake": ask, "multiplier": multiplier,
                "entry_spot": float(proposal["spot"]) if proposal.get("spot") else None,
                "deriv_limit_order": proposal.get("limit_order"), "longcode": bought.get("longcode")}

    async def _track_contract(self, st: SymbolState, pos: Position) -> None:
        try:
            sub = await self.broker.subscribe({"proposal_open_contract": 1, "contract_id": int(pos.contract_id)})
            async for msg in sub:
                poc = msg.get("proposal_open_contract") or {}
                if poc.get("is_sold") or poc.get("status") in {"sold", "won", "lost"}:
                    profit = float(poc.get("profit") or 0.0)
                    r = profit / pos.risk_amount if pos.risk_amount else 0.0
                    exit_px = float(poc["exit_tick"]) if poc.get("exit_tick") else None
                    reason = poc.get("status") or "sold"
                    await self._finish(st, pos, exit_px, r, round(profit, 2), reason)
                    await sub.close()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            events.publish("trade.tracking_error", {"id": pos.trade_id, "error": str(exc)}, level="error",
                           message=f"Lost track of contract {pos.contract_id}: check Deriv")


ENGINE = LiveEngine()


async def start(cfg: EngineConfig) -> dict:
    return await ENGINE.start(cfg)


async def stop(reason: str = "stopped") -> dict:
    return await ENGINE.stop(reason)


def status() -> dict:
    return ENGINE.status()
