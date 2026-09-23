"""The original dashboard API (same paths and response shapes as the Flask server).

Kept so the existing Vue frontend keeps working during the transition. Changes:
state lives in SQLite; Deriv tokens go to the write-only secret store; live
orders go through the async client, the risk gate and the `trades` table.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app import db, events, secrets_store
from app.legacy import core, state as legacy_state
from app.services import risk
from app.services.deriv.client import DerivClient, DerivError

router = APIRouter(prefix="/api", tags=["legacy"])


def _err(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _client(settings: dict) -> DerivClient:
    return DerivClient(account_mode=settings.get("deriv_options_account_mode", "demo"),
                       account_id=settings.get("deriv_options_account_id", ""))


@router.get("/health")
def health() -> dict:
    return {"ok": True, "service": "fx-scalper-community", "broker_provider": "deriv", "api": "fastapi"}


@router.get("/bootstrap")
def bootstrap() -> dict:
    return core.bootstrap_payload(legacy_state.load())


@router.get("/rules")
def rules() -> dict:
    return {"rules": core.RULES}


@router.post("/config")
def update_config(payload: dict = Body(default_factory=dict)) -> dict:
    # A token or app id submitted by the old form goes to the secret store, never into state.
    token = str(payload.pop("deriv_token", "") or "").strip()
    if token:
        secrets_store.set_secret("deriv_token", token)
    app_id = str(payload.pop("deriv_app_id", "") or "").strip()
    if app_id.isdigit():
        secrets_store.set_secret("deriv_app_id", app_id)
    state = legacy_state.load()
    state["settings"] = core.normalize_settings(payload, state.get("settings", {}))
    legacy_state.save(state)
    return {"message": "Settings updated", "settings": core.public_settings(state["settings"])}


@router.get("/broker/status")
def broker_status() -> dict:
    state = legacy_state.load()
    settings = state.get("settings", {})
    return {"status": state.get("last_broker_status"), "setup": core.setup_status(settings, core.market_view(settings))}


@router.post("/broker/test")
async def broker_test() -> dict:
    state = legacy_state.load()
    settings = state.get("settings", {})
    client = _client(settings)
    if not client.token:
        status = core.record_broker_status(state, {
            "ok": False, "connected": False, "token_mode": "none",
            "message": "Add a Deriv API token (Settings → API keys) before testing the broker connection."})
        legacy_state.save(state)
        return {"ok": False, "message": status["message"], "status": status}
    try:
        info = await client.connect()
        info["rtt_ms"] = await client.ping()
        symbols = await client.active_symbols()
        if info.get("loginid") and client.token_mode == "pat":
            state["settings"]["deriv_options_account_id"] = client.account.get("account_id", "")
        status = core.record_broker_status(state, {"ok": True, "message": "Deriv connection verified successfully.",
                                                   "active_symbol_count": len(symbols), **info})
        events.publish("broker.connected", status, message="Deriv connection verified")
        ok = True
    except DerivError as exc:
        status = core.record_broker_status(state, {"ok": False, "connected": False, "message": str(exc),
                                                   "app_id": client.app_id, "token_mode": client.token_mode})
        events.publish("broker.error", status, level="error", message=str(exc))
        ok = False
    finally:
        await client.close()
    legacy_state.save(state)
    return {"ok": ok, "message": status["message"], "status": status}


@router.post("/paper-trades/open")
def paper_trade_open(payload: dict = Body(default_factory=dict)):
    state = legacy_state.load()
    settings = state.get("settings", {})
    symbol_map = core.symbol_map_from_settings(settings)
    market_map = {row["symbol"]: row for row in core.configured_market_snapshots(settings)}
    if not settings.get("paper_trading_enabled", True):
        return _err("Paper trading is disabled in settings.")
    symbol = str(payload.get("symbol") or "").strip()
    side = core.sanitize_trade_side(payload.get("side"), "BUY")
    try:
        exposure = float(payload.get("exposure") or 0)
        price = float(payload.get("price") or 0)
    except (TypeError, ValueError):
        return _err("Exposure and price must be numeric.")
    if not symbol or exposure <= 0 or price <= 0:
        return _err("Invalid paper trade payload.")
    if len(state.get("portfolio", [])) >= int(settings.get("max_open_positions", 6)):
        return _err("Max open positions reached.")
    meta = symbol_map.get(symbol)
    if not meta:
        return _err("Unknown symbol. Add it in settings before opening a paper trade.")
    position = {
        "id": f"pos-{uuid.uuid4().hex[:8]}", "symbol": symbol, "asset_class": meta["asset_class"], "side": side,
        "exposure": round(exposure, 2), "entry_price": round(price, 5),
        "current_price": round(float((market_map.get(symbol) or {}).get("price") or price), 5),
        "opened_at": core.utc_now_iso(),
    }
    state.setdefault("portfolio", []).append(position)
    state.setdefault("trade_history", []).insert(0, {
        "timestamp": core.utc_now_iso(), "type": "OPEN", "symbol": symbol, "side": side,
        "exposure": position["exposure"], "note": "Paper trade opened from community dashboard."})
    legacy_state.save(state)
    return {"message": "Paper trade opened", "position": position}


@router.post("/live-trades/open")
async def live_trade_open(payload: dict = Body(default_factory=dict)):
    state = legacy_state.load()
    settings = state.get("settings", {})
    if not settings.get("live_trading_enabled", False):
        return _err("Live trading is disabled in settings.")
    symbol = str(payload.get("symbol") or "").strip()
    side = core.sanitize_trade_side(payload.get("side"), "BUY")
    if symbol not in core.symbol_map_from_settings(settings):
        return _err("Unknown symbol. Add it in settings before placing a live trade.")
    amount = core.sanitize_live_amount(payload.get("amount", settings.get("live_trade_stake", 1.0)),
                                       settings.get("live_trade_stake", 1.0))
    duration = core.sanitize_duration(payload.get("duration", settings.get("live_trade_duration", 5)),
                                      settings.get("live_trade_duration", 5))
    duration_unit = core.sanitize_duration_unit(payload.get("duration_unit", settings.get("live_trade_duration_unit",
                                                                                          "t")), "t")
    currency = core.sanitize_currency(payload.get("currency", settings.get("live_trade_currency", "USD")), "USD")
    contract_type = core.sanitize_live_contract_type(payload.get("contract_type"), side)
    client = _client(settings)
    request = {"symbol": symbol, "side": side, "contract_type": contract_type, "amount": amount,
               "currency": currency, "duration": duration, "duration_unit": duration_unit}
    try:
        await client.connect()
        proposal = await client.proposal(amount=amount, basis="stake", contract_type=contract_type,
                                         currency=currency, duration=duration, duration_unit=duration_unit,
                                         underlying_symbol=symbol)
        proposal_id = str(proposal.get("id") or "").strip()
        if not proposal_id:
            raise DerivError("Deriv did not return a proposal ID for this trade.")
        ask_price = core.sanitize_live_amount(proposal.get("ask_price", amount), amount)
        risk.check_order(risk.OrderIntent(symbol=symbol, stake=ask_price, contract_type=contract_type,
                                          is_virtual=client.is_virtual))
        buy = await client.buy(proposal_id, ask_price)
        mode = "demo" if client.is_virtual else "real"
        trade_id = f"t-{uuid.uuid4().hex[:10]}"
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO trades(id, mode, symbol, direction, contract_type, stake, entry_price, status, "
                "opened_at, contract_id, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                (trade_id, mode, symbol, side, contract_type, ask_price, proposal.get("spot"), db.utc_now(),
                 str(buy.get("contract_id") or ""), db.dumps({"source": "legacy-live", "request": request})))
        status = core.record_broker_status(state, {"ok": True, "message": "Deriv trade submitted successfully.",
                                                   **client.account_info()})
        state.setdefault("trade_history", []).insert(0, {
            "timestamp": core.utc_now_iso(), "type": "LIVE_OPEN", "symbol": symbol, "side": side, "exposure": amount,
            "note": f"{mode.upper()} {contract_type} via Deriv. Contract {buy.get('contract_id') or 'pending'}."})
        legacy_state.save(state)
        events.publish("trade.opened", {"id": trade_id, "mode": mode, **request, "contract_id": buy.get("contract_id")},
                       message=f"{mode.upper()} {contract_type} {symbol} opened")
        return {"message": "Live trade submitted", "status": status, "proposal": proposal, "buy": buy,
                "request": request}
    except (DerivError, risk.RiskBlocked) as exc:
        status = core.record_broker_status(state, {"ok": False, "connected": client.connected, "message": str(exc),
                                                   "app_id": client.app_id, "token_mode": client.token_mode})
        state.setdefault("trade_history", []).insert(0, {
            "timestamp": core.utc_now_iso(), "type": "LIVE_ERROR", "symbol": symbol, "side": side,
            "exposure": amount, "note": str(exc)})
        legacy_state.save(state)
        return _err(str(exc), status=403 if isinstance(exc, risk.RiskBlocked) else 400, status_detail=status)
    finally:
        await client.close()


@router.post("/paper-trades/rebalance")
def paper_trade_rebalance():
    state = legacy_state.load()
    if not state.get("settings", {}).get("rebalance_enabled", True):
        return _err("Rebalancing is disabled in settings.")
    plan = core.build_rebalance_plan(state)
    closed = []
    for action in plan["actions"]:
        if action["action"] != "CLOSE_POSITION" or not action.get("position_id"):
            continue
        for index, position in enumerate(list(state.get("portfolio", []))):
            if str(position.get("id")) == action["position_id"]:
                state["portfolio"].pop(index)
                state.setdefault("trade_history", []).insert(0, {
                    "timestamp": core.utc_now_iso(), "type": "REBALANCE_CLOSE", "symbol": position["symbol"],
                    "side": position["side"], "exposure": position["exposure"], "note": action["reason"]})
                closed.append(position)
                break
    legacy_state.save(state)
    return {"message": "Rebalance applied", "plan": plan, "closed_positions": closed}


@router.post("/reset")
def reset() -> dict:
    state = legacy_state.reset()
    return {"message": "Community edition state reset", "settings": core.public_settings(state["settings"])}


def legacy_backtest() -> dict:
    """The original 3-scenario rule check (used by POST /api/backtests/run with an empty body)."""
    state = legacy_state.load()
    state["last_backtest"] = core.backtest_summary(state)
    legacy_state.save(state)
    return {"message": "Backtests completed", "summary": state["last_backtest"]}
