#!/usr/bin/env python3
"""Check A/B bot milestones and send Telegram alert."""
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
THRESHOLDS = {"aggressive": [30, 50, 100], "conservative": [500]}


def load_state(db_path):
    conn = sqlite3.connect(db_path, timeout=5)
    row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
    conn.close()
    return row[0] if row else 0


def load_notified():
    path = "/tmp/ab_milestones_notified.json"
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_notified(notified):
    with open("/tmp/ab_milestones_notified.json", "w") as f:
        json.dump(list(notified), f)


def send_telegram(msg):
    try:
        import httpx

        token = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=10,
            )
        else:
            print(f"No creds: {msg}", file=sys.stderr)
    except Exception as e:
        print(f"TG: {e}", file=sys.stderr)


def main():
    notified = load_notified()
    new_alerts, counts = [], {}
    for name, db_path in DB_PATHS.items():
        if not Path(db_path).exists():
            continue
        count = load_state(db_path)
        counts[name] = count
        for thresh in THRESHOLDS.get(name, []):
            key = f"{name}_{thresh}"
            if count >= thresh and key not in notified:
                new_alerts.append((name, thresh, count))
                notified.add(key)

    if new_alerts:
        save_notified(notified)
        lines = [f"{n.title()} bot {t} trades" for n, t, _ in new_alerts]
        send_telegram("A/B: " + ", ".join(lines))
        print("Alerts:", lines)

    # Staleness check
    if "aggressive" in counts:
        conn = sqlite3.connect(DB_PATHS["aggressive"])
        row = conn.execute("SELECT MAX(exit_time) FROM trades").fetchone()
        conn.close()
        lt = row[0] if row else None
        if lt:
            ts = lt.replace("Z", "+00:00") if "T" in lt else lt
            h = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600
            k = "stale_sent"
            if h > 12 and k not in notified:
                notified.add(k)
                save_notified(notified)
                send_telegram(f"STALE: {h:.0f}h no trades, last {lt[:16]}")
                print(f"Stale {h:.0f}h")

    print(f"CONS {counts.get('conservative', 0)}t  AGG {counts.get('aggressive', 0)}t")


if __name__ == "__main__":
    main()
