#!/usr/bin/env python3
"""Check A/B bot milestones and send Telegram alert when thresholds are met."""
import json
import os
import sqlite3
import sys
from pathlib import Path

DB_PATHS = {
    "conservative": "/opt/hermes-trading-bot/data/hermes.db",
    "aggressive": "/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db",
}

THRESHOLDS = {
    "aggressive": [30, 50, 100],
    "conservative": [500],
}

def load_state(db_path):
    """Get trade count from open trades (closed) + current trade count."""
    conn = sqlite3.connect(db_path, timeout=5)
    row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
    count = row[0] if row else 0
    conn.close()
    return count

def load_notified():
    """Load which milestones have already been notified."""
    path = "/tmp/ab_milestones_notified.json"
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return set()

def save_notified(notified):
    with open("/tmp/ab_milestones_notified.json", "w") as f:
        json.dump(list(notified), f)

def send_telegram(msg):
    """Send Telegram message using the bot's .env creds."""
    import httpx
    token = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"No Telegram creds, would send: {msg}", file=sys.stderr)
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send error: {e}", file=sys.stderr)

def main():
    notified = load_notified()
    new_alerts = []

    for name, db_path in DB_PATHS.items():
        if not Path(db_path).exists():
            continue
        count = load_state(db_path)
        for thresh in THRESHOLDS.get(name, []):
            key = f"{name}_{thresh}"
            if count >= thresh and key not in notified:
                new_alerts.append((name, thresh, count))
                notified.add(key)

    if new_alerts:
        save_notified(notified)
        lines = []
        for name, thresh, count in new_alerts:
            lines.append(f"⚡ {name.title()} bot hit {thresh} trades ({count} total)")
        msg = "📊 A/B Milestone Reached\n" + "\n".join(lines)
        msg += "\n\nTime to evaluate the A/B test!"
        send_telegram(msg)
        print(msg)
    else:
        # Print current counts for reference
        counts = {}
        for name, db_path in DB_PATHS.items():
            if Path(db_path).exists():
                counts[name] = load_state(db_path)
        print(f"AB check: {counts.get('conservative', 0)}t cons / {counts.get('aggressive', 0)}t agg")

if __name__ == "__main__":
    main()
