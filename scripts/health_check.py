"""
Health check — run by systemd timer every 5 minutes.
Restarts the bot if no equity snapshot in the last 6 minutes.
Exit codes:
  0 = healthy
  1 = unhealthy, recovery attempted
  2 = unhealthy, manual intervention needed
"""

import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/opt/hermes-trading-bot/data/hermes.db")
SERVICE = "hermes-bot.service"
STALE_MINUTES = 6


def check_db():
    if not DB_PATH.exists():
        print(f"HEALTH: DB not found at {DB_PATH}")
        return False
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute("SELECT timestamp FROM equity_snapshots ORDER BY timestamp DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row is None:
            print("HEALTH: DB empty — no equity snapshots yet (bot may be starting up)")
            return True
        ts_str = row[0]
        ts = datetime.fromisoformat(ts_str)
        age_minutes = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        if age_minutes > STALE_MINUTES:
            print(f"HEALTH: STALE — last snapshot {age_minutes:.0f} min ago at {ts_str} (limit: {STALE_MINUTES})")
            return False
        print(f"HEALTH: OK — last snapshot {age_minutes:.0f} min ago")
        return True
    except Exception as e:
        print(f"HEALTH: DB error: {e}")
        return False


def restart_bot():
    print(f"HEALTH: Attempting restart of {SERVICE}...")
    result = subprocess.run(
        ["sudo", "systemctl", "restart", SERVICE],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        print("HEALTH: Restart OK")
        return True
    print(f"HEALTH: Restart FAILED: {result.stderr}")
    return False


def main():
    db_ok = check_db()

    if db_ok:
        print("HEALTH: PASS")
        sys.exit(0)

    print("HEALTH: FAIL — attempting recovery")
    ok = restart_bot()
    if ok:
        print("HEALTH: Recovery successful")
        sys.exit(1)
    else:
        print("HEALTH: Manual intervention required")
        sys.exit(2)


if __name__ == "__main__":
    main()
