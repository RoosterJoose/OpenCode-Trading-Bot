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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
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

                CREATE TABLE IF NOT EXISTS trade_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    source TEXT NOT NULL DEFAULT 'unknown',
                    strategy TEXT NOT NULL DEFAULT '',
                    asset TEXT NOT NULL,
                    side TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    intended_entry_price REAL NOT NULL DEFAULT 0,
                    requested_stop_price REAL,
                    requested_leverage REAL NOT NULL DEFAULT 1,
                    components TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}',
                    reject_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    processed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    asset TEXT NOT NULL DEFAULT 'PORTFOLIO_WIDE',
                    pattern_category TEXT NOT NULL,
                    pattern_detail TEXT NOT NULL,
                    frequency_count INTEGER NOT NULL DEFAULT 1,
                    action TEXT NOT NULL DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_lessons_date_cat ON lessons(date, pattern_category);

                CREATE TABLE IF NOT EXISTS parameter_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT DEFAULT (datetime('now')),
                    parameter_name TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    suggested_value TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    applied_at TEXT,
                    impact_notes TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS altfins_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    source TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    bucket TEXT DEFAULT '',
                    signal_time TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_altfins_asset_time ON altfins_signals(asset, signal_time);

                CREATE TABLE IF NOT EXISTS candles (
                    asset TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    PRIMARY KEY (asset, timestamp)
                );
                CREATE INDEX IF NOT EXISTS idx_param_changes_status ON parameter_changes(status);

                CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
                CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
                CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
                CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_intents_expires ON trade_intents(expires_at);
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

    def save_intent(self, intent: dict) -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO trade_intents (
                        idempotency_key, status, source, strategy, asset, side, confidence,
                        intended_entry_price, requested_stop_price, requested_leverage,
                        components, payload, created_at, expires_at
                    ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent["idempotency_key"],
                        intent.get("source", "unknown"),
                        intent.get("strategy", ""),
                        intent["asset"],
                        intent["side"],
                        float(intent.get("confidence", 0)),
                        float(intent.get("intended_entry_price", 0)),
                        intent.get("requested_stop_price"),
                        float(intent.get("requested_leverage", 1)),
                        json.dumps(intent.get("components", []), default=str),
                        json.dumps(intent, default=str),
                        intent["created_at"],
                        intent["expires_at"],
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def pending_intents(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trade_intents WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_intent_status(self, intent_id: int, status: str, reason: str = ""):
        with self._lock:
            self._conn.execute(
                """
                UPDATE trade_intents
                SET status = ?, reject_reason = ?, processed_at = datetime('now')
                WHERE id = ?
                """,
                (status, reason, intent_id),
            )
            self._conn.commit()

    def intents(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trade_intents ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Delegation Gap metrics (NotebookLM) ─────────────────────────

    def record_delegation_metric(self, source: str, accepted: bool, impl_shortfall_pct: float = 0.0):
        with self._lock:
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)",
                (f"delegation_{source}_{datetime.now(timezone.utc).isoformat()}",
                 json.dumps({"accepted": accepted, "shortfall_pct": round(impl_shortfall_pct, 4)})),
            )
            self._conn.commit()

    def delegation_summary(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM state WHERE key LIKE 'delegation_%'"
            ).fetchall()
            if not rows:
                return {"total": 0, "accepted": 0, "rejected": 0, "avg_shortfall": 0.0}
            total = len(rows)
            accepted = sum(1 for _, v in rows if json.loads(v).get("accepted"))
            shortfalls = [json.loads(v).get("shortfall_pct", 0) for _, v in rows if json.loads(v).get("accepted")]
            avg_sf = sum(shortfalls) / len(shortfalls) if shortfalls else 0.0
            return {
                "total": total,
                "accepted": accepted,
                "rejected": total - accepted,
                "rejection_rate": round((total - accepted) / total * 100, 1) if total > 0 else 0.0,
                "avg_impl_shortfall_bps": round(avg_sf * 100, 2),
            }

    # ── Lessons (append-only learning log, NotebookLM design) ─────────

    def insert_lesson(self, date: str, asset: str, category: str, detail: str, count: int, action: str = ""):
        with self._lock:
            self._conn.execute(
                "INSERT INTO lessons (date, asset, pattern_category, pattern_detail, frequency_count, action) VALUES (?, ?, ?, ?, ?, ?)",
                (date, asset, category, detail, count, action),
            )
            self._conn.commit()

    def lessons(self, days: int = 30, category: str = "") -> list[dict]:
        with self._lock:
            if category:
                rows = self._conn.execute(
                    "SELECT * FROM lessons WHERE date >= date('now', ? || ' days') AND pattern_category = ? ORDER BY date DESC",
                    (f"-{days}", category),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM lessons WHERE date >= date('now', ? || ' days') ORDER BY date DESC, frequency_count DESC LIMIT 200",
                    (f"-{days}",),
                ).fetchall()
            return [dict(r) for r in rows]

    def lessons_summary(self, days: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT pattern_category, pattern_detail, SUM(frequency_count) as total, COUNT(DISTINCT date) as days_seen
                   FROM lessons WHERE date >= date('now', ? || ' days')
                   GROUP BY pattern_category, pattern_detail ORDER BY total DESC LIMIT 20""",
                (f"-{days}",),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Parameter change tracking ─────────────────────────────────────

    def insert_param_change(self, param: str, old_val: str, new_val: str, status: str = "pending"):
        with self._lock:
            self._conn.execute(
                "INSERT INTO parameter_changes (parameter_name, old_value, suggested_value, status) VALUES (?, ?, ?, ?)",
                (param, old_val, new_val, status),
            )
            self._conn.commit()

    def param_changes(self, status: str = "") -> list[dict]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM parameter_changes WHERE status = ? ORDER BY created_at DESC", (status,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM parameter_changes ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
            return [dict(r) for r in rows]

    def save_candles(self, asset: str, candles: list) -> None:
        with self._lock:
            for cdl in candles[-300:]:
                self._conn.execute(
                    "INSERT OR REPLACE INTO candles (asset, timestamp, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (asset, cdl.timestamp, cdl.open, cdl.high, cdl.low, cdl.close, cdl.volume),
                )
            self._conn.execute(
                "DELETE FROM candles WHERE asset = ? AND timestamp NOT IN (SELECT timestamp FROM candles WHERE asset = ? ORDER BY timestamp DESC LIMIT 300)",
                (asset, asset),
            )
            self._conn.commit()

    def load_candles(self, asset: str, max_bars: int = 250) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM candles WHERE asset = ? ORDER BY timestamp DESC LIMIT ?",
                (asset, max_bars),
            ).fetchall()
            rows = list(reversed(rows))
            from src.core.types import PerpCandle
            return [PerpCandle(
                timestamp=float(r["timestamp"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
            ) for r in rows]

    def candle_asset_count(self, stale_hours: float = 2.0) -> dict:
        cutoff = datetime.now(timezone.utc).timestamp() - (stale_hours * 3600)
        with self._lock:
            rows = self._conn.execute(
                "SELECT asset, COUNT(*) as cnt, MAX(timestamp) as latest FROM candles WHERE timestamp > ? GROUP BY asset",
                (cutoff,),
            ).fetchall()
            return {r["asset"]: {"count": r["cnt"], "latest": r["latest"]} for r in rows}

    def save_altfins_signal(self, asset: str, source: str, direction: str, confidence: float, bucket: str, signal_time: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO altfins_signals (asset, source, direction, confidence, bucket, signal_time) VALUES (?, ?, ?, ?, ?, ?)",
                (asset, source, direction, confidence, bucket, signal_time),
            )
            self._conn.commit()

    def close(self):
        self._conn.close()
