#!/usr/bin/env python3
"""ETL: migrate existing daily_reflection_* state keys into lessons table."""
import json, sqlite3, sys
from pathlib import Path

db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/hermes.db")
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

keys = conn.execute(
    "SELECT key, value FROM state WHERE key LIKE 'daily_reflection_%' ORDER BY key"
).fetchall()

inserted = 0
for key, val in keys:
    date = key.replace("daily_reflection_", "")
    data = json.loads(val)
    for l in data.get("learning", []):
        if l["type"] == "market_summary":
            continue
        cat = l["type"]
        detail = l.get("reason", l.get("action", ""))
        cnt = l.get("count", 1)
        action = l.get("action", "")
        assets = l.get("assets", [])
        for asset in assets if assets else ["PORTFOLIO_WIDE"]:
            conn.execute(
                "INSERT OR IGNORE INTO lessons (date, asset, pattern_category, pattern_detail, frequency_count, action) VALUES (?, ?, ?, ?, ?, ?)",
                (date, asset, cat, detail, cnt, action),
            )
            inserted += 1

conn.commit()
total = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
print(f"Migrated {inserted} rows into lessons table ({total} total)")
conn.close()
