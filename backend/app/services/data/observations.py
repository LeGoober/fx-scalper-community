"""Point-in-time observation store: everything the system learns from outside, with when it learnt it.

Every row has two clocks:
  published_at : when the source says it happened (headline time, alert time)
  ingested_at  : when THIS system first had it
A decision made at time t may only use rows with ingested_at <= t (and published_at <= t). That is the
whole defence against look-ahead: a backtest or a replay that asks for a snapshot at t sees exactly
what the live system could have seen then, never a later revision.

Revisions: a source can re-publish the same item with new content (a calendar event gains its
'actual' figure). Items with a stable `key` keep every version; `snapshot` returns the latest
version that existed at t. Identical content is stored once (dedupe on the content hash).

Observed text (news, alerts) is untrusted data. It is stored as-is and only ever shown to agents
inside untrusted-data delimiters; nothing downstream executes or obeys it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Iterable

from app import db


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]


def record(source: str, kind: str, payload: dict, *, published_at: int | None = None, key: str | None = None,
           symbol: str | None = None, currencies: Iterable[str] = (), title: str | None = None,
           ingested_at: int | None = None) -> str | None:
    """Store one observation. Returns its id, or None when identical content was already stored."""
    content = _hash(payload)
    now = int(ingested_at if ingested_at is not None else time.time())
    obs_id = f"obs-{content[:16]}"
    with db.connect() as conn, db.tx(conn):
        prev = None
        if key:
            prev = db.row(conn, "SELECT id, content_hash FROM observations WHERE source = ? AND obs_key = ? "
                                "ORDER BY ingested_at DESC LIMIT 1", (source, key))
            if prev and prev["content_hash"] == content:
                return None
        try:
            conn.execute("INSERT INTO observations(id, source, kind, obs_key, symbol, currencies, title, published_at, "
                         "ingested_at, content_hash, revision_of, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (obs_id, source, kind, key, symbol, ",".join(sorted({c.upper() for c in currencies if c})),
                          title, published_at, now, content, prev["id"] if prev else None, db.dumps(payload)))
        except sqlite3.IntegrityError:
            return None
    return obs_id


def snapshot(ts: float, *, kinds: Iterable[str] | None = None, currencies: Iterable[str] | None = None,
             lookback_s: int = 86400, limit: int = 200) -> list[dict]:
    """What the system knew at `ts`: rows ingested (and published) by then, newest first, one per key."""
    kinds, currencies = list(kinds or []), [c.upper() for c in (currencies or [])]
    sql = ("SELECT * FROM observations WHERE ingested_at <= ? AND COALESCE(published_at, ingested_at) <= ? "
           "AND COALESCE(published_at, ingested_at) >= ?")
    params: list = [int(ts), int(ts), int(ts - lookback_s)]
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += kinds
    if currencies:
        sql += " AND (" + " OR ".join("(',' || currencies || ',') LIKE ?" for _ in currencies) + ")"
        params += [f"%,{c},%" for c in currencies]
    with db.connect() as conn:
        rows = db.rows(conn, sql + " ORDER BY ingested_at DESC", tuple(params))
    seen, out = set(), []
    for r in rows:  # newest version of each keyed item that existed at ts
        ident = (r["source"], r["obs_key"]) if r["obs_key"] else r["id"]
        if ident in seen:
            continue
        seen.add(ident)
        r["payload"] = db.loads(r.pop("payload_json"), {})
        r["currencies"] = [c for c in (r["currencies"] or "").split(",") if c]
        out.append(r)
        if len(out) >= limit:
            break
    return out


def recent(kind: str | None = None, limit: int = 100) -> list[dict]:
    return snapshot(time.time() + 1, kinds=[kind] if kind else None, lookback_s=365 * 86400, limit=limit)


def counts() -> list[dict]:
    with db.connect() as conn:
        return db.rows(conn, "SELECT source, kind, COUNT(*) AS n, MAX(ingested_at) AS last_ingested "
                             "FROM observations GROUP BY source, kind ORDER BY kind, source")
