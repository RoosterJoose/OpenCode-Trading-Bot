#!/usr/bin/env python3
"""Weekly reflection — every Sunday at 00:10 UTC.
Aggregates 7 days of data per sleeve, sends Telegram report."""
import sqlite3, json, os, asyncio, httpx
from datetime import datetime, timezone, timedelta
from collections import defaultdict

def week_range():
    now = datetime.now(timezone.utc)
    end = now
    start = now - timedelta(days=7)
    return start.isoformat()[:19], end.isoformat()[:19]

async def main():
    start, end = week_range()
    c = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
    
    eq = float(c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()[0].strip('"'))
    eq_pk = float(c.execute("SELECT value FROM state WHERE key='paper_peak_equity'").fetchone()[0].strip('"'))
    total_trades = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    week_trades = c.execute("SELECT COUNT(*) FROM trades WHERE created_at >= ? AND created_at < ?", (start, end)).fetchone()[0]
    
    sections = [f"Weekly Report — ${eq:.0f} (${eq_pk:.0f} peak, {(eq_pk-eq)/eq_pk*100:.1f}% DD)"]
    sections.append(f"Period: {start[:10]} → {end[:10]} | {week_trades}t this week / {total_trades}t lifetime")
    
    for sleeve in ["mr", "trend", "xs_momentum", "donchian"]:
        week = c.execute("SELECT pnl_dollars FROM trades WHERE strategy=? AND created_at >= ? AND created_at < ?", (sleeve, start, end)).fetchall()
        total = c.execute("SELECT COUNT(*) FROM trades WHERE strategy=?", (sleeve,)).fetchone()[0]
        w_pnls = [float(r[0]) for r in week if r[0]]
        w_n = len(w_pnls)
        if w_n == 0:
            sections.append(f"[{sleeve.upper()}] No trades this week")
            continue
        wr = sum(1 for p in w_pnls if p > 0) / w_n * 100
        avg = sum(w_pnls) / w_n
        total_w = sum(w_pnls)
        
        # Last 7 days trend
        pnl_by_day = defaultdict(float)
        for pnl_str, day_str in c.execute("SELECT pnl_dollars, created_at FROM trades WHERE strategy=? AND created_at >= ? AND created_at < ?", (sleeve, start, end)).fetchall():
            if pnl_str:
                pnl_by_day[day_str[:10]] += float(pnl_str)
        
        trend_str = " | ".join(f"{d[-5:]}:${pnl:+.0f}" for d, pnl in sorted(pnl_by_day.items()))
        
        sections.append(f"[{sleeve.upper()}]")
        sections.append(f"  Week: ${total_w:+.0f} | {w_n}t | WR {wr:.0f}% | Avg ${avg:.2f}")
        sections.append(f"  Total lifetime: {total}t")
        sections.append(f"  Trend: {trend_str}")
    
    if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='lessons'").fetchone():
        l_count = c.execute("SELECT COUNT(*) FROM lessons WHERE created_at >= ?", (start,)).fetchone()[0]
        sections.append(f"\n[LESSONS] {l_count} entries this week")
    
    gs = int(c.execute("SELECT value FROM state WHERE key='risk_global_loss_streak'").fetchone()[0].strip('"'))
    sections.append(f"[RISK] GS={gs}")
    c.close()
    
    msg = "\n".join(sections)
    tok = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
    if tok and chat:
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg})

asyncio.run(main())
