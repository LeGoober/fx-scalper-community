"""In-process event bus feeding the `/api/stream` WebSocket, plus a persisted event log.

`publish()` is safe to call from worker threads (sync endpoints, background jobs).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import db

log = logging.getLogger("fxs.events")

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None

# Kinds worth keeping in the `events` table (progress ticks are not).
PERSISTED_PREFIXES = ("trade.", "signal.", "risk.", "broker.", "job.", "backtest.", "webhook.", "engine.")


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def _fanout(message: dict) -> None:
    for queue in list(_subscribers):
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:  # slow client: drop rather than block the engine
            pass


def publish(kind: str, data: Any = None, *, message: str = "", level: str = "info") -> None:
    event = {"type": kind, "ts": db.utc_now(), "level": level, "message": message, "data": data}
    if kind.startswith(PERSISTED_PREFIXES):
        try:
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO events(ts, kind, level, message, data_json) VALUES (?, ?, ?, ?, ?)",
                    (event["ts"], kind, level, message or kind, db.dumps(data)),
                )
        except Exception:  # logging must never break the caller
            log.exception("failed to persist event %s", kind)
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _fanout(event)
    else:
        loop.call_soon_threadsafe(_fanout, event)


def recent(limit: int = 100, kind_prefix: str = "") -> list[dict]:
    with db.connect() as conn:
        items = db.rows(
            conn,
            "SELECT id, ts, kind, level, message, data_json FROM events "
            "WHERE kind LIKE ? ORDER BY id DESC LIMIT ?",
            (f"{kind_prefix}%", max(1, min(limit, 1000))),
        )
    for item in items:
        item["data"] = db.loads(item.pop("data_json"))
    return items
