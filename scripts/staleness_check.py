#!/usr/bin/env python3
"""Check if both bots have traded in the last 12h — alert via Telegram if stale."""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATHS = {
    "conservative": "/opt/hermes-trading-bot/data/hermes.db",
    "aggressive": "/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db",
}

TELEGRAM_BOT_TOKEN = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN") or os.environ.get("HERMES_TELEGRAM_WEBHOOK")
TELEGRAM_CHAT_ID = os.environ.get("HERMES_TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage" if TELEGRAM_BOT_TOKEN else None


def send_telegram(msg: str):
    if not TELEGRAM_API or not TELEGRAM_CHAT_ID:
        return
    import urllib.request
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(TELEGRAM_API, payload, timeout=10)
    except Exception:
        pass


now = datetime.now(timezone.utc)
stale_bots = []

for label, path in DB_PATHS.items():
    db = Path(path)
    if not db.exists():
        continue
    conn = sqlite3.connect(str(db), timeout=5)
    row = conn.execute("SELECT MAX(entry_time) FROM trades").fetchone()
    conn.close()
    if row and row[0]:
        last = row[0]
        if isinstance(last, str):
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        else:
            last_dt = last
        hours_ago = (now - last_dt).total_seconds() / 3600
        if hours_ago > 12:
            stale_bots.append(f"{label}: {hours_ago:.0f}h since last trade ({last})")

if stale_bots:
    msg = "STALE ALERT\n" + "\n".join(stale_bots) + "\n\nBot may be stuck — check journalctld"
    send_telegram(msg)
    print(msg)
    sys.exit(1)
else:
    print(f"Staleness OK — both bots active")
    sys.exit(0)
