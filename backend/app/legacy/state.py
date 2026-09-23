"""Legacy dashboard state (settings, paper portfolio, history) stored as one SQLite kv row.

Replaces the old `store.json` that was rewritten on every request. On first run,
an existing store.json is imported once, and its Deriv token is moved into the
write-only secret store rather than kept in state.
"""
from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy

from app import config, db, secrets_store
from app.legacy.core import DEFAULT_STATE, sanitize_state

log = logging.getLogger("fxs.legacy")

KEY = "legacy_state"
_lock = threading.Lock()


def _import_store_json() -> dict | None:
    path = config.data_dir() / "store.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    token = str((raw.get("settings") or {}).get("deriv_token") or "").strip()
    if token and not config.deriv_token():
        secrets_store.set_secret("deriv_token", token)
        log.info("moved deriv token from store.json into .env")
    return raw


def load() -> dict:
    with _lock:
        stored = db.kv_get(KEY)
        if stored is None:
            stored = _import_store_json() or deepcopy(DEFAULT_STATE)
            db.kv_set(KEY, sanitize_state(stored))
    return sanitize_state(stored)


def save(state: dict) -> None:
    with _lock:
        db.kv_set(KEY, sanitize_state(state))


def reset() -> dict:
    state = deepcopy(DEFAULT_STATE)
    save(state)
    return state
