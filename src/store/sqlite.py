"""
SQLite store — trades, signals, equity snapshots, system state.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL,
                    exit_price REAL,
                    size REAL,
                    leverage REAL,
                    pnl_pct REAL,
                    pnl_dollars REAL,
                    fees REAL,
                    funding_paid REAL,
                    exit_reason TEXT,
                    strategy TEXT,
                    signal_source TEXT,
                    entry_confidence REAL,
                    entry_time TEXT,
                    exit_time TEXT,
                    regime TEXT DEFAULT '',
                    r_multiple REAL DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    equity REAL,
                    peak_equity REAL,
                    timestamp TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                );
            """)
            self._conn.commit()

    def save_trade(self, trade: dict):
        with self._lock:
            cols = ", ".join(trade.keys())
            placeholders = ":" + ", :".join(trade.keys())
            sql = f"INSERT INTO trades ({cols}) VALUES ({placeholders})"
            self._conn.execute(sql, trade)
            self._conn.commit()

    def save_equity_snapshot(self, equity: float, peak: float):
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity_snapshots (equity, peak_equity) VALUES (?, ?)",
                (equity, peak),
            )
            self._conn.commit()

    def trades(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def put_state(self, key: str, value: Any):
        with self._lock:
            serialized = json.dumps(value, default=str)
            self._conn.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                (key, serialized),
            )
            self._conn.commit()

    def get_state(self, key: str) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["value"])

    def recent_equity(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def close(self):
        self._conn.close()
