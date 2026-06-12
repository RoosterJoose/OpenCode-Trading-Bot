"""Check if BTC trend churn is resolved after chandelier fix.
Runs 24h after deploy to verify fix effectiveness."""
import sqlite3, json, sys
from pathlib import Path

DB = Path("/opt/hermes-trading-bot/data/hermes.db")
conn = sqlite3.connect(str(DB))

rows = list(conn.execute(
    "SELECT asset, strategy, entry_time, exit_time, pnl_dollars, exit_reason "
    "FROM trades WHERE asset='BTC' AND strategy='trend' "
    "ORDER BY id DESC LIMIT 30"
))

if not rows:
    print("NO_BTC_TREND_TRADES_24H")
    conn.close()
    sys.exit(0)

rapid_stops = 0
for r in rows:
    et = r[2] if r[2] else ""
    xt = r[3] if r[3] else ""
    reason = r[5]
    if reason == "stop_loss" and et and xt:
        try:
            from datetime import datetime
            e = datetime.fromisoformat(et)
            x = datetime.fromisoformat(xt)
            minutes = (x - e).total_seconds() / 60
            if minutes < 5:
                rapid_stops += 1
        except:
            pass

print(f"BTC trend trades (24h): {len(rows)}")
print(f"Rapid stop_loss (<5min): {rapid_stops}")

if rapid_stops > 5:
    print("STATUS: CHURN_CONTINUES")
    print("ACTION: Chandelier fix not sufficient. Investigate entry logic.")
elif rapid_stops > 0:
    print("STATUS: IMPROVED")
    print("ACTION: Some rapid stops remain but significantly reduced. Monitor.")
else:
    print("STATUS: RESOLVED")
    print("ACTION: Zero rapid stops. Chandelier fix working correctly.")

conn.close()
