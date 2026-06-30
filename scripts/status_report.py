#!/usr/bin/env python3
import sqlite3, json, os, asyncio, httpx

def get_stats(path, label):
    try:
        c = sqlite3.connect(path)
        state = dict(c.execute("SELECT key, value FROM state").fetchall())
        eq = float(state.get("paper_equity","0").strip('"'))
        peak = float(state.get("paper_peak_equity","0").strip('"'))
        dd = (peak-eq)/peak*100 if peak>0 else 0
        trades = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        recent = c.execute("SELECT asset, side, pnl_dollars FROM trades ORDER BY id DESC LIMIT 3").fetchall()
        pos = json.loads(state.get("positions","[]"))
        c.close()
        last3 = "; ".join(f"{r[0]} {r[1]} ${r[2]:+.1f}" for r in recent)
        return f"{label}: ${eq:.0f} (DD {dd:.1f}%) {len(pos)}pos {trades}t\n  recent: {last3}"
    except Exception as e:
        return f"{label}: error {e}"

async def main():
    token = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN","")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT_ID","")
    msg = "📊 Daily Status Report\n" + \
        get_stats("/opt/hermes-trading-bot/data/hermes.db", "CONS") + "\n" + \
        get_stats("/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db", "AGGR")
    if token and chat:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": msg})
    else:
        print(msg)

asyncio.run(main())
