#!/usr/bin/env python3
import sqlite3, json, os, asyncio, httpx

def sleeve_report(c, name):
    recent = c.execute(f"SELECT pnl_dollars FROM trades WHERE strategy=? ORDER BY id DESC LIMIT 20", (name,)).fetchall()
    pnls = [float(r[0]) for r in recent if r[0]]
    n = len(pnls)
    wr = sum(1 for p in pnls if p > 0) / max(n, 1) * 100
    avg_pnl = sum(pnls) / max(n, 1)
    total = c.execute(f"SELECT COUNT(*) FROM trades WHERE strategy=?", (name,)).fetchone()[0]
    y = c.execute("SELECT COALESCE(SUM(pnl_dollars),0) FROM trades WHERE strategy=? AND created_at >= datetime('now', '-1 day', 'start of day') AND created_at < datetime('now', 'start of day')", (name,)).fetchone()[0]
    t = c.execute("SELECT COALESCE(SUM(pnl_dollars),0) FROM trades WHERE strategy=? AND created_at >= datetime('now', 'start of day')", (name,)).fetchone()[0]
    tm = c.execute("SELECT value FROM state WHERE key=?", (f"{name}_last_mod_cycle",)).fetchone()
    mod_trades = total - int(tm[0]) if tm else 999
    locked = mod_trades < 30
    lines = [f"[{name.upper()}]"]
    if n >= 20:
        lines.append(f"  20t WR: {wr:.0f}% | Avg: ${avg_pnl:.2f} | Total: {total}t")
    else:
        lines.append(f"  Trades: {total}t (need 20 for rolling)")
    lines.append(f"  Today: ${float(t):+.2f} | Yesterday: ${float(y):+.2f}")
    lines.append(f"  Lockout: {'LOCKED' if locked else 'OPEN'} ({mod_trades}/30)")
    if total < 30:
        lines.append("  Rec: Wait (need 30t)")
    elif float(t) < 0 and float(y) < 0 and wr < 40:
        lines.append("  Rec: TIGHTEN")
    elif float(t) > 0 and float(y) > 0 and wr > 50:
        lines.append("  Rec: Hold — positive")
    else:
        lines.append("  Rec: Hold")
    return "\n".join(lines)

async def main():
    tok = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
    c = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
    eq = float(c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()[0].strip('"'))
    sections = [f"Daily Recommendation Report — ${eq:.0f}"]
    for sleeve in ["mr", "trend", "xs_momentum", "donchian"]:
        sections.append(sleeve_report(c, sleeve))
    gs = int(c.execute("SELECT value FROM state WHERE key='risk_global_loss_streak'").fetchone()[0].strip('"'))
    sh = c.execute("SELECT COUNT(*) FROM state WHERE key='self_heal'").fetchone()[0]
    sections.append(f"[RISK] GS={gs} | Self-heal: {sh}")
    c.close()
    msg = "\n\n".join(sections)
    if tok and chat:
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg})

asyncio.run(main())
