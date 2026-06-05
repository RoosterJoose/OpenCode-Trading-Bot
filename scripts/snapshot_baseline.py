#!/usr/bin/env python3
"""Snapshot current DB state for pre-migration baseline."""
import json, sqlite3, sys
from pathlib import Path

db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/opt/hermes-trading-bot/data/hermes.db")
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row

snapshot = {}

# Equity
row = conn.execute("SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
snapshot["equity"] = dict(row) if row else None

# Trade count
snapshot["total_trades"] = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

# Recent trades
rows = conn.execute("SELECT asset, side, strategy, exit_reason, pnl_dollars FROM trades ORDER BY id DESC LIMIT 10").fetchall()
snapshot["recent_trades"] = [dict(r) for r in rows]

# Open positions from exchange state
row = conn.execute("SELECT value FROM state WHERE key='positions'").fetchone()
if row:
    snapshot["positions"] = json.loads(row[0])
else:
    snapshot["positions"] = []

print(json.dumps(snapshot, indent=2, default=str))
conn.close()
