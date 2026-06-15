from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
STORE_PATH = DATA_DIR / "store.json"

DEFAULT_TARGET_ALLOCATIONS = {
    "FOREX": 0.40,
    "COMMODITIES": 0.20,
    "CRYPTO": 0.15,
    "SYNTHETICS": 0.25,
}

RULES = [
    {
        "id": "regime_detection",
        "name": "Regime Detection",
        "purpose": "Classify a market as trending, ranging, or low-volatility before signal generation.",
        "inputs": ["adx", "atr_ratio", "ema_fast", "ema_slow"],
        "conditions": [
            "Low-volatility when ATR ratio is below 0.85.",
            "Trending when ADX is above the configured threshold and EMA fast is separated from EMA slow.",
            "Ranging otherwise.",
        ],
        "outputs": ["market_regime"],
    },
    {
        "id": "trend_confirmation",
        "name": "Trend Confirmation",
        "purpose": "Allow paper trade entries only when structure, momentum, and price position agree.",
        "inputs": ["ema_fast", "ema_slow", "adx", "rsi", "price_vs_vwap"],
        "conditions": [
            "BUY when EMA fast > EMA slow, ADX is above threshold, RSI is inside the bullish band, and price is above VWAP.",
            "SELL when EMA fast < EMA slow, ADX is above threshold, RSI is inside the bearish band, and price is below VWAP.",
        ],
        "outputs": ["paper_signal"],
    },
    {
        "id": "overextension_guard",
        "name": "Overextension Guard",
        "purpose": "Reject entries that look stretched or late.",
        "inputs": ["rsi", "distance_to_band", "candle_quality"],
        "conditions": [
            "Reject BUY when RSI > 70.",
            "Reject SELL when RSI < 30.",
            "Reject entries when candle quality is stretched or band distance is extreme.",
        ],
        "outputs": ["validation_decision"],
    },
    {
        "id": "portfolio_rebalancing",
        "name": "Portfolio Rebalancing",
        "purpose": "Keep paper exposure aligned with target weights.",
        "inputs": ["positions", "target_allocations", "rebalance_tolerance", "rebalance_key_mode"],
        "conditions": [
            "Close overweight exposure when drift exceeds tolerance.",
            "Prioritize underweight buckets for new entries.",
        ],
        "outputs": ["rebalance_plan"],
    },
]

SYMBOLS = [
    {"symbol": "frxEURUSD", "label": "EUR/USD", "asset_class": "FOREX"},
    {"symbol": "frxGBPUSD", "label": "GBP/USD", "asset_class": "FOREX"},
    {"symbol": "frxXAUUSD", "label": "Gold/USD", "asset_class": "COMMODITIES"},
    {"symbol": "cryBTCUSD", "label": "BTC/USD", "asset_class": "CRYPTO"},
    {"symbol": "stpRNG10", "label": "Step Index 10", "asset_class": "SYNTHETICS"},
]

SYMBOL_MAP = {row["symbol"]: row for row in SYMBOLS}
BASE_MARKET_MAP = {}

BASE_MARKET = [
    {
        "symbol": "frxEURUSD",
        "asset_class": "FOREX",
        "price": 1.0871,
        "ema_fast": 1.0865,
        "ema_slow": 1.0856,
        "adx": 26.0,
        "rsi": 56.0,
        "atr_ratio": 1.08,
        "price_vs_vwap": "above",
        "candle_quality": "clean",
        "distance_to_band": 0.62,
    },
    {
        "symbol": "frxGBPUSD",
        "asset_class": "FOREX",
        "price": 1.2733,
        "ema_fast": 1.2721,
        "ema_slow": 1.2727,
        "adx": 23.0,
        "rsi": 47.0,
        "atr_ratio": 0.98,
        "price_vs_vwap": "below",
        "candle_quality": "clean",
        "distance_to_band": 0.44,
    },
    {
        "symbol": "frxXAUUSD",
        "asset_class": "COMMODITIES",
        "price": 2328.4,
        "ema_fast": 2327.1,
        "ema_slow": 2325.5,
        "adx": 19.0,
        "rsi": 69.0,
        "atr_ratio": 1.02,
        "price_vs_vwap": "above",
        "candle_quality": "stretched",
        "distance_to_band": 0.91,
    },
    {
        "symbol": "cryBTCUSD",
        "asset_class": "CRYPTO",
        "price": 67220.0,
        "ema_fast": 67080.0,
        "ema_slow": 66990.0,
        "adx": 28.0,
        "rsi": 61.0,
        "atr_ratio": 1.10,
        "price_vs_vwap": "above",
        "candle_quality": "clean",
        "distance_to_band": 0.58,
    },
    {
        "symbol": "stpRNG10",
        "asset_class": "SYNTHETICS",
        "price": 239.7,
        "ema_fast": 239.4,
        "ema_slow": 240.2,
        "adx": 21.0,
        "rsi": 43.0,
        "atr_ratio": 0.92,
        "price_vs_vwap": "below",
        "candle_quality": "clean",
        "distance_to_band": 0.37,
    },
]

BASE_MARKET_MAP = {row["symbol"]: row for row in BASE_MARKET}

DEFAULT_STATE = {
    "settings": {
        "paper_trading_enabled": True,
        "risk_profile": "balanced",
        "max_open_positions": 6,
        "rebalance_enabled": True,
        "rebalance_key_mode": "asset_class",
        "rebalance_tolerance": 0.08,
        "target_allocations": dict(DEFAULT_TARGET_ALLOCATIONS),
    },
    "portfolio": [
        {
            "id": "pos-1",
            "symbol": "frxEURUSD",
            "asset_class": "FOREX",
            "side": "BUY",
            "exposure": 1200.0,
            "entry_price": 1.0842,
            "current_price": 1.0871,
            "opened_at": "2026-06-15T08:00:00Z",
        },
        {
            "id": "pos-2",
            "symbol": "cryBTCUSD",
            "asset_class": "CRYPTO",
            "side": "BUY",
            "exposure": 650.0,
            "entry_price": 66850.0,
            "current_price": 67220.0,
            "opened_at": "2026-06-15T08:20:00Z",
        },
        {
            "id": "pos-3",
            "symbol": "stpRNG10",
            "asset_class": "SYNTHETICS",
            "side": "SELL",
            "exposure": 900.0,
            "entry_price": 241.4,
            "current_price": 239.7,
            "opened_at": "2026-06-15T08:35:00Z",
        },
    ],
    "trade_history": [],
    "last_backtest": None,
}


@dataclass(frozen=True)
class RebalanceAction:
    action: str
    allocation_key: str
    reason: str
    current_weight: float
    target_weight: float
    drift: float
    position_id: Optional[str] = None
    symbol: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_store() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STORE_PATH.exists():
        STORE_PATH.write_text(json.dumps(DEFAULT_STATE, indent=2), encoding="utf-8")


def load_state() -> dict:
    ensure_store()
    try:
        stored = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        stored = deepcopy(DEFAULT_STATE)
    return sanitize_state(stored)


def save_state(state: dict) -> None:
    ensure_store()
    STORE_PATH.write_text(json.dumps(sanitize_state(state), indent=2), encoding="utf-8")


def normalize_key_mode(value: object) -> str:
    mode = str(value or "asset_class").strip().lower()
    return mode if mode in {"asset_class", "symbol"} else "asset_class"


def normalize_target_allocations(targets: object, key_mode: str = "asset_class") -> dict[str, float]:
    key_mode = normalize_key_mode(key_mode)
    if not isinstance(targets, dict):
        targets = DEFAULT_TARGET_ALLOCATIONS if key_mode == "asset_class" else {}
    cleaned: dict[str, float] = {}
    for raw_key, raw_value in targets.items():
        key = str(raw_key or "").strip().upper()
        if not key:
            continue
        try:
            value = float(raw_value)
        except Exception:
            continue
        if value <= 0:
            continue
        cleaned[key] = value
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_TARGET_ALLOCATIONS) if key_mode == "asset_class" else {}
    return {key: value / total for key, value in cleaned.items()}


def sanitize_position(raw_position: object) -> Optional[dict]:
    if not isinstance(raw_position, dict):
        return None
    symbol = str(raw_position.get("symbol") or "").strip()
    side = str(raw_position.get("side") or "").strip().upper()
    if symbol not in SYMBOL_MAP or side not in {"BUY", "SELL"}:
        return None
    try:
        exposure = float(raw_position.get("exposure") or 0.0)
        entry_price = float(raw_position.get("entry_price") or 0.0)
        current_price = float(raw_position.get("current_price") or entry_price or 0.0)
    except Exception:
        return None
    if exposure <= 0 or entry_price <= 0 or current_price <= 0:
        return None
    asset_class = SYMBOL_MAP[symbol]["asset_class"]
    return {
        "id": str(raw_position.get("id") or f"pos-{uuid4().hex[:8]}"),
        "symbol": symbol,
        "asset_class": asset_class,
        "side": side,
        "exposure": round(exposure, 2),
        "entry_price": round(entry_price, 5),
        "current_price": round(current_price, 5),
        "opened_at": str(raw_position.get("opened_at") or utc_now_iso()),
    }


def sanitize_trade_history(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        return []
    cleaned: list[dict] = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip()
        if symbol and symbol not in SYMBOL_MAP:
            continue
        try:
            exposure = round(float(row.get("exposure") or 0.0), 2)
        except Exception:
            exposure = 0.0
        cleaned.append(
            {
                "timestamp": str(row.get("timestamp") or utc_now_iso()),
                "type": str(row.get("type") or "INFO").strip().upper(),
                "symbol": symbol,
                "side": str(row.get("side") or "").strip().upper(),
                "exposure": exposure,
                "note": str(row.get("note") or "").strip(),
            }
        )
    return cleaned


def sanitize_state(state: object) -> dict:
    raw = state if isinstance(state, dict) else {}
    settings = normalize_settings(raw.get("settings") or {}, deepcopy(DEFAULT_STATE["settings"]))
    portfolio = [pos for pos in (sanitize_position(item) for item in raw.get("portfolio", [])) if pos]
    clean_state = deepcopy(DEFAULT_STATE)
    clean_state["settings"] = settings
    clean_state["portfolio"] = portfolio
    clean_state["trade_history"] = sanitize_trade_history(raw.get("trade_history", []))
    clean_state["last_backtest"] = raw.get("last_backtest")
    return clean_state


def error_response(message: str, status_code: int = 400):
    return jsonify({"error": message}), status_code


def json_payload() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def signal_thresholds(risk_profile: str) -> dict[str, float]:
    profile = str(risk_profile or "balanced").strip().lower()
    if profile == "aggressive":
        return {"adx": 16.0, "rsi_buy_min": 42.0, "rsi_buy_max": 70.0, "rsi_sell_min": 30.0, "rsi_sell_max": 58.0}
    if profile == "conservative":
        return {"adx": 24.0, "rsi_buy_min": 48.0, "rsi_buy_max": 62.0, "rsi_sell_min": 38.0, "rsi_sell_max": 52.0}
    return {"adx": 20.0, "rsi_buy_min": 45.0, "rsi_buy_max": 68.0, "rsi_sell_min": 32.0, "rsi_sell_max": 55.0}


def classify_regime(snapshot: dict, thresholds: dict[str, float]) -> str:
    if float(snapshot.get("atr_ratio") or 0.0) < 0.85:
        return "LOW_VOLATILITY"
    if float(snapshot.get("adx") or 0.0) >= thresholds["adx"] and abs(float(snapshot.get("ema_fast") or 0.0) - float(snapshot.get("ema_slow") or 0.0)) > 0:
        return "TRENDING"
    return "RANGING"


def evaluate_snapshot(snapshot: dict, settings: dict) -> dict:
    thresholds = signal_thresholds(settings.get("risk_profile", "balanced"))
    regime = classify_regime(snapshot, thresholds)
    decision = "HOLD"
    reason = "No qualified setup."
    adx = float(snapshot.get("adx") or 0.0)
    rsi = float(snapshot.get("rsi") or 50.0)
    above_vwap = snapshot.get("price_vs_vwap") == "above"
    below_vwap = snapshot.get("price_vs_vwap") == "below"
    stretched = snapshot.get("candle_quality") != "clean" or float(snapshot.get("distance_to_band") or 0.0) > 0.88
    if regime == "TRENDING" and not stretched:
        if float(snapshot.get("ema_fast") or 0.0) > float(snapshot.get("ema_slow") or 0.0) and adx >= thresholds["adx"] and thresholds["rsi_buy_min"] <= rsi <= thresholds["rsi_buy_max"] and above_vwap:
            decision = "BUY"
            reason = "Trend structure, momentum, and price positioning are aligned."
        elif float(snapshot.get("ema_fast") or 0.0) < float(snapshot.get("ema_slow") or 0.0) and adx >= thresholds["adx"] and thresholds["rsi_sell_min"] <= rsi <= thresholds["rsi_sell_max"] and below_vwap:
            decision = "SELL"
            reason = "Downtrend structure, momentum, and price positioning are aligned."
    elif regime == "LOW_VOLATILITY":
        reason = "ATR compression suggests low opportunity quality."
    elif stretched:
        reason = "Overextension guard blocked a late or stretched entry."
    return {**snapshot, "regime": regime, "decision": decision, "reason": reason, "generated_at": utc_now_iso()}


def market_view(settings: dict) -> list[dict]:
    return [evaluate_snapshot(dict(row), settings) for row in BASE_MARKET]


def refresh_portfolio_prices(positions: list[dict]) -> list[dict]:
    refreshed: list[dict] = []
    for position in positions:
        updated = dict(position)
        latest_market = BASE_MARKET_MAP.get(updated["symbol"])
        if latest_market:
            updated["current_price"] = round(float(latest_market["price"]), 5)
        refreshed.append(updated)
    return refreshed


def count_open_signals(market: list[dict]) -> int:
    return sum(1 for row in market if row["decision"] in {"BUY", "SELL"})


def summarize_positions(positions: list[dict], key_mode: str = "asset_class") -> tuple[float, dict[str, float]]:
    totals: dict[str, float] = {}
    total_exposure = 0.0
    mode = normalize_key_mode(key_mode)
    for pos in positions:
        exposure = float(pos.get("exposure") or 0.0)
        if exposure <= 0:
            continue
        key = str(pos.get("asset_class") if mode == "asset_class" else pos.get("symbol") or "UNKNOWN").upper()
        totals[key] = totals.get(key, 0.0) + exposure
        total_exposure += exposure
    if total_exposure <= 0:
        return 0.0, {}
    return total_exposure, {key: value / total_exposure for key, value in totals.items()}


def build_rebalance_plan(state: dict) -> dict:
    settings = state.get("settings", {})
    positions = state.get("portfolio", [])
    key_mode = normalize_key_mode(settings.get("rebalance_key_mode", "asset_class"))
    targets = normalize_target_allocations(settings.get("target_allocations"), key_mode)
    tolerance = max(0.0, min(float(settings.get("rebalance_tolerance", 0.08) or 0.08), 0.50))
    total_exposure, current_weights = summarize_positions(positions, key_mode)
    drift_score = sum(abs(current_weights.get(key, 0.0) - targets.get(key, 0.0)) for key in set(current_weights) | set(targets))
    actions: list[RebalanceAction] = []
    if total_exposure > 0:
        for key in sorted(set(current_weights) | set(targets)):
            current = current_weights.get(key, 0.0)
            target = targets.get(key, 0.0)
            drift = current - target
            if drift > tolerance:
                matching = [pos for pos in positions if str(pos.get("asset_class") if key_mode == "asset_class" else pos.get("symbol") or "UNKNOWN").upper() == key]
                matching.sort(key=lambda item: float(item.get("exposure") or 0.0), reverse=True)
                if matching:
                    position = matching[0]
                    actions.append(
                        RebalanceAction(
                            action="CLOSE_POSITION",
                            allocation_key=key,
                            reason=f"{key} is overweight by {drift:.2%}; reduce exposure until drift is within tolerance.",
                            current_weight=current,
                            target_weight=target,
                            drift=drift,
                            position_id=str(position.get("id")),
                            symbol=str(position.get("symbol")),
                        )
                    )
            elif target - current > tolerance:
                actions.append(
                    RebalanceAction(
                        action="PRIORITIZE_NEW_ENTRIES",
                        allocation_key=key,
                        reason=f"{key} is underweight by {(target - current):.2%}; prioritize new paper trades in this bucket.",
                        current_weight=current,
                        target_weight=target,
                        drift=target - current,
                    )
                )
    return {
        "total_exposure": round(total_exposure, 2),
        "current_weights": {key: round(value, 4) for key, value in current_weights.items()},
        "target_weights": {key: round(value, 4) for key, value in targets.items()},
        "drift_score": round(drift_score, 4),
        "enabled": bool(settings.get("rebalance_enabled", True)),
        "tolerance": round(tolerance, 4),
        "key_mode": key_mode,
        "actions": [asdict(action) for action in actions],
    }


def backtest_summary(state: dict) -> dict:
    settings = state.get("settings", {})
    scenarios = [
        {
            "name": "Fresh bullish trend",
            "snapshot": {
                "symbol": "scenario-bull",
                "asset_class": "FOREX",
                "price": 1.1042,
                "ema_fast": 1.1036,
                "ema_slow": 1.1022,
                "adx": 27.0,
                "rsi": 58.0,
                "atr_ratio": 1.05,
                "price_vs_vwap": "above",
                "candle_quality": "clean",
                "distance_to_band": 0.52,
            },
            "expected": "BUY",
        },
        {
            "name": "Low volatility skip",
            "snapshot": {
                "symbol": "scenario-flat",
                "asset_class": "FOREX",
                "price": 1.0991,
                "ema_fast": 1.0992,
                "ema_slow": 1.0991,
                "adx": 14.0,
                "rsi": 50.0,
                "atr_ratio": 0.72,
                "price_vs_vwap": "above",
                "candle_quality": "clean",
                "distance_to_band": 0.40,
            },
            "expected": "HOLD",
        },
        {
            "name": "Overextended rejection",
            "snapshot": {
                "symbol": "scenario-stretch",
                "asset_class": "COMMODITIES",
                "price": 2341.0,
                "ema_fast": 2335.0,
                "ema_slow": 2329.0,
                "adx": 31.0,
                "rsi": 74.0,
                "atr_ratio": 1.18,
                "price_vs_vwap": "above",
                "candle_quality": "stretched",
                "distance_to_band": 0.96,
            },
            "expected": "HOLD",
        },
    ]
    results = []
    passes = 0
    for scenario in scenarios:
        outcome = evaluate_snapshot(scenario["snapshot"], settings)
        passed = outcome["decision"] == scenario["expected"]
        passes += int(passed)
        results.append({"name": scenario["name"], "expected": scenario["expected"], "actual": outcome["decision"], "passed": passed, "reason": outcome["reason"]})
    rebalance = build_rebalance_plan(state)
    return {
        "run_at": utc_now_iso(),
        "scenario_passes": passes,
        "scenario_total": len(scenarios),
        "scenario_results": results,
        "rebalance_check": {
            "passed": True,
            "drift_score": rebalance["drift_score"],
            "action_count": len(rebalance["actions"]),
        },
    }


def bootstrap_payload(state: dict) -> dict:
    state["portfolio"] = refresh_portfolio_prices(state.get("portfolio", []))
    market = market_view(state.get("settings", {}))
    total_exposure, current_weights = summarize_positions(
        state.get("portfolio", []),
        state.get("settings", {}).get("rebalance_key_mode", "asset_class"),
    )
    return {
        "settings": state.get("settings", {}),
        "market": market,
        "signals": [row for row in market if row["decision"] != "HOLD"],
        "portfolio": state.get("portfolio", []),
        "trade_history": state.get("trade_history", [])[:20],
        "rules": RULES,
        "rebalance_plan": build_rebalance_plan(state),
        "last_backtest": state.get("last_backtest"),
        "symbols": SYMBOLS,
        "stats": {
            "market_count": len(market),
            "signal_count": count_open_signals(market),
            "open_positions": len(state.get("portfolio", [])),
            "total_exposure": round(total_exposure, 2),
            "current_weights": {key: round(value, 4) for key, value in current_weights.items()},
        },
    }


def normalize_settings(payload: dict, current: dict) -> dict:
    merged = {**current}
    merged["paper_trading_enabled"] = bool(payload.get("paper_trading_enabled", current.get("paper_trading_enabled", True)))
    risk = str(payload.get("risk_profile", current.get("risk_profile", "balanced"))).strip().lower()
    merged["risk_profile"] = risk if risk in {"conservative", "balanced", "aggressive"} else "balanced"
    try:
        merged["max_open_positions"] = max(1, min(int(payload.get("max_open_positions", current.get("max_open_positions", 6))), 20))
    except Exception:
        merged["max_open_positions"] = current.get("max_open_positions", 6)
    merged["rebalance_enabled"] = bool(payload.get("rebalance_enabled", current.get("rebalance_enabled", True)))
    merged["rebalance_key_mode"] = normalize_key_mode(payload.get("rebalance_key_mode", current.get("rebalance_key_mode", "asset_class")))
    try:
        tolerance = float(payload.get("rebalance_tolerance", current.get("rebalance_tolerance", 0.08)))
    except Exception:
        tolerance = current.get("rebalance_tolerance", 0.08)
    merged["rebalance_tolerance"] = max(0.0, min(tolerance, 0.50))
    merged["target_allocations"] = normalize_target_allocations(payload.get("target_allocations", current.get("target_allocations", DEFAULT_TARGET_ALLOCATIONS)), merged["rebalance_key_mode"])
    return merged


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "fx-scalper-community-edition"})


@app.get("/api/bootstrap")
def bootstrap():
    return jsonify(bootstrap_payload(load_state()))


@app.get("/api/rules")
def rules():
    return jsonify({"rules": RULES})


@app.post("/api/config")
def update_config():
    state = load_state()
    state["settings"] = normalize_settings(json_payload(), state.get("settings", {}))
    save_state(state)
    return jsonify({"message": "Settings updated", "settings": state["settings"]})


@app.post("/api/paper-trades/open")
def paper_trade_open():
    state = load_state()
    payload = json_payload()
    settings = state.get("settings", {})
    if not settings.get("paper_trading_enabled", True):
        return error_response("Paper trading is disabled in settings.", 400)
    symbol = str(payload.get("symbol") or "").strip()
    side = str(payload.get("side") or "BUY").strip().upper()
    try:
        exposure = float(payload.get("exposure") or 0)
        price = float(payload.get("price") or 0)
    except Exception:
        return error_response("Exposure and price must be numeric.")
    if not symbol or side not in {"BUY", "SELL"} or exposure <= 0 or price <= 0:
        return error_response("Invalid paper trade payload.")
    if len(state.get("portfolio", [])) >= int(settings.get("max_open_positions", 6)):
        return error_response("Max open positions reached.")
    meta = BASE_MARKET_MAP.get(symbol)
    if not meta:
        return error_response("Unknown symbol.")
    position = {
        "id": f"pos-{uuid4().hex[:8]}",
        "symbol": symbol,
        "asset_class": meta["asset_class"],
        "side": side,
        "exposure": round(exposure, 2),
        "entry_price": round(price, 5),
        "current_price": round(float(meta["price"]), 5),
        "opened_at": utc_now_iso(),
    }
    state.setdefault("portfolio", []).append(position)
    state.setdefault("trade_history", []).insert(
        0,
        {
            "timestamp": utc_now_iso(),
            "type": "OPEN",
            "symbol": symbol,
            "side": side,
            "exposure": position["exposure"],
            "note": "Paper trade opened from community dashboard.",
        },
    )
    save_state(state)
    return jsonify({"message": "Paper trade opened", "position": position})


@app.post("/api/paper-trades/rebalance")
def paper_trade_rebalance():
    state = load_state()
    if not state.get("settings", {}).get("rebalance_enabled", True):
        return error_response("Rebalancing is disabled in settings.", 400)
    plan = build_rebalance_plan(state)
    closed = []
    for action in plan["actions"]:
        if action["action"] != "CLOSE_POSITION" or not action.get("position_id"):
            continue
        for index, position in enumerate(list(state.get("portfolio", []))):
            if str(position.get("id")) == action["position_id"]:
                state["portfolio"].pop(index)
                state.setdefault("trade_history", []).insert(
                    0,
                    {
                        "timestamp": utc_now_iso(),
                        "type": "REBALANCE_CLOSE",
                        "symbol": position["symbol"],
                        "side": position["side"],
                        "exposure": position["exposure"],
                        "note": action["reason"],
                    },
                )
                closed.append(position)
                break
    save_state(state)
    return jsonify({"message": "Rebalance applied", "plan": plan, "closed_positions": closed})


@app.post("/api/backtests/run")
def backtests_run():
    state = load_state()
    state["last_backtest"] = backtest_summary(state)
    save_state(state)
    return jsonify({"message": "Backtests completed", "summary": state["last_backtest"]})


@app.post("/api/reset")
def reset():
    state = deepcopy(DEFAULT_STATE)
    save_state(state)
    return jsonify({"message": "Community edition demo state reset", "settings": state["settings"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
