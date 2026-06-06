"""
Weekly reflection — every Sunday at 00:10 UTC.
Combines all daily reflections from the past 7 days into a deep summary.
Writes insights to lessons table and surfaces key patterns.
"""
import sqlite3, json, sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path("/opt/hermes-trading-bot/data")
DB = DATA_DIR / "hermes.db"

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Get all lessons from the past 7 days
cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
rows = cur.execute(
    "SELECT * FROM lessons WHERE date >= ? ORDER BY date", (cutoff,)
).fetchall()

if not rows:
    print("No lessons in past 7 days. Skipping weekly reflection.")
    sys.exit(0)

# Aggregate by pattern_category
patterns = defaultdict(lambda: {"count": 0, "assets": set(), "days": set()})
for r in rows:
    cat = r["pattern_category"]
    patterns[cat]["count"] += r["frequency_count"]
    patterns[cat]["assets"].add(r["asset"])
    patterns[cat]["days"].add(r["date"])

# Find dominant patterns
sorted_patterns = sorted(
    patterns.items(), key=lambda x: x[1]["count"], reverse=True
)

print(f"Weekly Reflection: {len(rows)} lessons across {len(sorted_patterns)} categories")
print("")
for cat, data in sorted_patterns[:5]:
    print(f"  {cat:25s}  {data['count']:3d}x  across {len(data['assets'])} assets, {len(data['days'])} days")

# Determine top actionable insight
top = sorted_patterns[0] if sorted_patterns else None

conn.commit()
summary = {
    "date": datetime.now(timezone.utc).isoformat(),
    "type": "weekly_reflection",
    "total_lessons": len(rows),
    "categories": sorted_patterns[:10],
    "top_insight": {
        "category": top[0],
        "count": top[1]["count"],
        "assets": sorted(top[1]["assets"])[:10],
        "days": len(top[1]["days"]),
    } if top else None,
}

# Write to state key for dashboard
cur.execute(
    "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
    ("weekly_reflection", json.dumps(summary, default=str)),
)
conn.commit()
conn.close()

print(f"\nWeekly reflection written to state. Top insight: {summary['top_insight']['category'] if summary['top_insight'] else 'none'}")
