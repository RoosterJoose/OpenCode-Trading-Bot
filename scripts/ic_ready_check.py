#!/usr/bin/env python3
"""Check if MR/XS have enough trades to build IC weighting. Sends Telegram if ready."""
import sqlite3, json, asyncio, httpx, os

def check(path, label):
    c = sqlite3.connect(path)
    # Trades since confidence gate change (Jul 1)
    mr = c.execute("SELECT COUNT(*) FROM trades WHERE strategy='mr' AND created_at >= '2026-07-01'").fetchone()[0]
    xs = c.execute("SELECT COUNT(*) FROM trades WHERE strategy='xs_momentum' AND created_at >= '2026-07-01'").fetchone()[0]
    c.close()
    return {"label": label, "mr": mr, "xs": xs, "ready": mr >= 30 and xs >= 30}

con = check("/opt/hermes-trading-bot/data/hermes.db", "CONS")
aggr = check("/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db", "AGGR")

# Write ready state
state = {"cons": con, "aggr": aggr}
c = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", ("ic_ready_check", json.dumps(state)))
c.commit(); c.close()

msg_parts = []
for d in [con, aggr]:
    msg_parts.append(f"{d['label']}: MR={d['mr']}t XS={d['xs']}t {'READY' if d['ready'] else ''}")
    if d['ready']:
        msg_parts.append("IC weighting data sufficient — build IC calculator")

msg = " | ".join(msg_parts)

# Send Telegram if either ready
if con["ready"] or aggr["ready"]:
    tok = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
    if tok and chat:
        async def send():
            async with httpx.AsyncClient(timeout=10) as cl:
                await cl.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                    json={"chat_id": chat, "text": "IC READY CHECK: " + msg})
        asyncio.run(send())

print(msg)
