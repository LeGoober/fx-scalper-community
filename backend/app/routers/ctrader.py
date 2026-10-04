"""cTrader sign-in (OAuth2): grant this app trading access to your Deriv cTrader account.

Flow: /api/ctrader/oauth/start redirects to cTrader ID; after you approve, cTrader redirects back to
/api/ctrader/oauth/callback with a one-minute code, which is exchanged for an access token (~30 days)
and a refresh token (no expiry). Both go to the write-only secret store; nothing is shown back.
The callback URL must be listed as a redirect URI in your app at openapi.ctrader.com. If cTrader won't
accept a local address there, paste a token from your app's Playground in Settings instead.
"""
from __future__ import annotations

import secrets as pysecrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import config, db, events, secrets_store

router = APIRouter(prefix="/api/ctrader", tags=["ctrader"])
AUTH_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"
STATE_KEY = "ctrader_oauth_state"


def redirect_uri(request: Request) -> str:
    return config.env("COMMUNITY_CTRADER_REDIRECT_URI") or str(request.url_for("ctrader_callback"))


@router.get("/oauth/info", summary="The redirect URI to register in your cTrader app, and what is configured")
def info(request: Request) -> dict:
    return {"redirect_uri": redirect_uri(request), "client_id_set": bool(config.ctrader_client_id()),
            "client_secret_set": bool(config.ctrader_client_secret()),
            "access_token_set": bool(config.ctrader_access_token())}


@router.get("/oauth/start", summary="Open cTrader ID to grant trading access (redirect)")
def start(request: Request) -> RedirectResponse:
    if not (config.ctrader_client_id() and config.ctrader_client_secret()):
        raise HTTPException(412, "Add the cTrader client ID and secret in Settings → API keys first.")
    state = pysecrets.token_urlsafe(16)
    db.kv_set(STATE_KEY, state)
    query = urlencode({"client_id": config.ctrader_client_id(), "redirect_uri": redirect_uri(request),
                       "scope": "trading", "product": "web", "state": state})
    return RedirectResponse(f"{AUTH_URL}?{query}")


@router.get("/oauth/callback", name="ctrader_callback", summary="cTrader redirects here with a code")
def callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code:
        raise HTTPException(400, f"cTrader did not grant access: {error or 'no code'}")
    expected = db.kv_get(STATE_KEY)
    if expected and state and state != expected:
        raise HTTPException(400, "State mismatch: start the sign-in again from the dashboard.")
    params = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(request),
              "client_id": config.ctrader_client_id(), "client_secret": config.ctrader_client_secret()}
    try:
        r = httpx.get(TOKEN_URL, params=params, timeout=20, headers={"Accept": "application/json"})
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Token exchange failed: {type(exc).__name__}") from exc
    if r.status_code >= 400 or not data.get("accessToken"):
        why = data.get("errorCode") or data.get("description") or r.text[:200]
        raise HTTPException(502, f"Token exchange refused: {why}")
    secrets_store.set_secret("ctrader_access_token", data["accessToken"])
    if data.get("refreshToken"):
        secrets_store.set_secret("ctrader_refresh_token", data["refreshToken"])
    db.kv_set(STATE_KEY, None)
    events.publish("broker.connected", {"broker": "ctrader", "step": "oauth"}, message="cTrader access granted")
    return RedirectResponse("/desk?ctrader=connected")
