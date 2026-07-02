"""SQLite summaries: windows, orders, fills, latency, incidents, K captures.

Write rate is low (per-window summaries + order events), so a synchronous
sqlite3 connection in WAL mode driven through run_in_executor keeps things
simple without blocking the loop. The dashboard opens its own read-only
connection — the engine never serves reads.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    window_start INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    k REAL,
    k_captured_wall_ns INTEGER,
    outcome TEXT,
    close_price REAL,
    n_trades INTEGER DEFAULT 0,
    stake_usdc REAL DEFAULT 0,
    entry_edge REAL,
    realized_pnl_usdc REAL,
    fees_usdc REAL DEFAULT 0,
    fees_shares REAL DEFAULT 0,
    mode TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    session_slug TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    tif TEXT NOT NULL,
    strategy TEXT,
    state TEXT NOT NULL,
    filled_size REAL DEFAULT 0,
    created_wall_ns INTEGER,
    sent_mono_ns INTEGER,
    acked_mono_ns INTEGER,
    terminal_mono_ns INTEGER
);
CREATE TABLE IF NOT EXISTS fills (
    trade_id TEXT,
    order_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    fee_shares REAL DEFAULT 0,
    fee_usdc REAL DEFAULT 0,
    exchange_ts_ms INTEGER,
    recv_mono_ns INTEGER,
    PRIMARY KEY (order_id, trade_id)
);
CREATE TABLE IF NOT EXISTS latency_samples (
    wall_ns INTEGER,
    hop TEXT NOT NULL,       -- event_to_decision | decision_to_sent | sent_to_ack | order_rtt
    micros REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    wall_ns INTEGER,
    feed TEXT,
    kind TEXT NOT NULL,      -- reconnect | staleness | book_dirty | recorder_drop | kill ...
    detail TEXT
);
CREATE TABLE IF NOT EXISTS k_captures (
    window_start INTEGER PRIMARY KEY,
    k REAL NOT NULL,
    oracle_ts_ms INTEGER NOT NULL,
    recv_wall_ns INTEGER NOT NULL,
    lag_ms REAL              -- oracle_ts - boundary, for boundary-capture QA
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def connect(self) -> None:
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        conn = self._conn
        if conn is None:
            raise RuntimeError("database not connected")

        def _run() -> None:
            conn.execute(sql, params)
            conn.commit()

        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(None, _run)

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        conn = self._conn
        if conn is None:
            raise RuntimeError("database not connected")

        def _run() -> None:
            conn.executemany(sql, rows)
            conn.commit()

        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(None, _run)

    # -- typed helpers --------------------------------------------------------
    async def incident(self, feed: str, kind: str, detail: str, wall_ns_: int) -> None:
        await self.execute(
            "INSERT INTO incidents (wall_ns, feed, kind, detail) VALUES (?,?,?,?)",
            (wall_ns_, feed, kind, detail),
        )

    async def latency(self, hop: str, micros: float, wall_ns_: int) -> None:
        await self.execute(
            "INSERT INTO latency_samples (wall_ns, hop, micros) VALUES (?,?,?)",
            (wall_ns_, hop, micros),
        )

    async def k_capture(
        self, window_start: int, k: float, oracle_ts_ms: int, recv_wall_ns: int
    ) -> None:
        lag_ms = oracle_ts_ms - window_start * 1000
        await self.execute(
            "INSERT OR REPLACE INTO k_captures "
            "(window_start, k, oracle_ts_ms, recv_wall_ns, lag_ms) VALUES (?,?,?,?,?)",
            (window_start, k, oracle_ts_ms, recv_wall_ns, lag_ms),
        )
