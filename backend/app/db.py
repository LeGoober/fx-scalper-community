"""SQLite storage (stdlib only). WAL mode, one short-lived connection per unit of work."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app import config

_init_lock = threading.Lock()
_initialized: set[str] = set()

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE candles (
        symbol TEXT NOT NULL,
        granularity INTEGER NOT NULL,
        epoch INTEGER NOT NULL,
        open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'deriv',
        PRIMARY KEY (symbol, granularity, epoch)
    ) WITHOUT ROWID;
    CREATE TABLE transcript_jobs (
        id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL,
        total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
        error TEXT, created_at TEXT NOT NULL, finished_at TEXT
    );
    CREATE TABLE videos (
        video_id TEXT PRIMARY KEY, title TEXT, channel TEXT, upload_date TEXT,
        duration_s INTEGER, playlist_id TEXT, url TEXT,
        status TEXT NOT NULL DEFAULT 'pending', error TEXT, language TEXT,
        segment_count INTEGER NOT NULL DEFAULT 0, fetched_at TEXT, source TEXT
    );
    CREATE TABLE transcript_segments (
        video_id TEXT NOT NULL, idx INTEGER NOT NULL,
        start REAL NOT NULL, duration REAL NOT NULL, text TEXT NOT NULL,
        PRIMARY KEY (video_id, idx)
    ) WITHOUT ROWID;
    CREATE TABLE segment_tags (
        video_id TEXT NOT NULL, window_idx INTEGER NOT NULL,
        start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL,
        concept TEXT, concept_confidence REAL, concept_probs TEXT,
        is_rule REAL, specificity REAL, bias TEXT, bias_confidence REAL,
        model TEXT, tagged_at TEXT,
        PRIMARY KEY (video_id, window_idx)
    ) WITHOUT ROWID;
    CREATE TABLE strategies (
        id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
        schema_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (id, version)
    );
    CREATE TABLE jev_cache (
        key TEXT PRIMARY KEY, model TEXT, response_json TEXT NOT NULL,
        latency_ms REAL, input_tokens INTEGER, created_at TEXT NOT NULL
    );
    CREATE TABLE backtest_runs (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
        params_json TEXT NOT NULL, summary_json TEXT, trades_json TEXT,
        equity_json TEXT, error TEXT, finished_at TEXT
    );
    CREATE TABLE signals (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL, symbol TEXT NOT NULL,
        direction TEXT NOT NULL, strategy_id TEXT, strategy_version INTEGER,
        source TEXT NOT NULL, status TEXT NOT NULL, decision_json TEXT NOT NULL
    );
    CREATE TABLE trades (
        id TEXT PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL,
        direction TEXT NOT NULL, contract_type TEXT, stake REAL,
        entry_price REAL, exit_price REAL, stop_price REAL, target_price REAL,
        r_multiple REAL, pnl REAL, status TEXT NOT NULL,
        opened_at TEXT NOT NULL, closed_at TEXT, contract_id TEXT,
        signal_id TEXT, strategy_id TEXT, meta_json TEXT
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
        level TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT
    );
    CREATE INDEX idx_events_ts ON events(ts);
    CREATE INDEX idx_trades_opened ON trades(opened_at);
    """,
    # 2 — economic calendar events (news filter), accumulated from every fetch
    """
    CREATE TABLE econ_events (
        ts INTEGER NOT NULL, currency TEXT NOT NULL, title TEXT NOT NULL,
        importance INTEGER NOT NULL, source TEXT NOT NULL,
        actual TEXT, forecast TEXT, previous TEXT,
        PRIMARY KEY (ts, currency, title)
    ) WITHOUT ROWID;
    CREATE INDEX idx_econ_currency_ts ON econ_events(currency, ts);
    """,
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            for statement in script.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def init() -> None:
    path = str(config.db_path())
    with _init_lock:
        if path in _initialized:
            return
        conn = _open(path)
        try:
            _migrate(conn)
        finally:
            conn.close()
        _initialized.add(path)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Autocommit connection. Use `with tx(conn):` for multi-statement atomic writes."""
    init()
    conn = _open(str(config.db_path()))
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def rows(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def kv_get(key: str, default: Any = None) -> Any:
    with connect() as conn:
        r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def kv_set(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), utc_now()),
        )


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def loads(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else default
