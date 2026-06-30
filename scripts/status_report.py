#!/usr/bin/env python3
import sqlite3, json, os, asyncio, httpx

def get_stats(path, label):
    try:
        c = sqlite3.connect(path)
        state = dict(c.execute("SELECT key, value FROM state").fetchall())
        eq = float(state.get("paper_equity","0").strip('"'))
        peak = float(state.get("paper_peak_equity","0").strip('"'))
        dd = (peak-eq)/peak*100 if peak>0 else 0
        trade_count = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        last_24h = c.execute("SELECT COUNT(*) FROM trades WHERE created_at >= datetime('now', '-1 day')").fetchone()[0]
        recent = c.execute("SELECT asset, side, pnl_dollars FROM trades ORDER BY id DESC LIMIT 3").fetchall()
        pos = json.loads(state.get("positions","[]"))
        today_trades = c.execute("SELECT pnl_dollars FROM trades WHERE created_at >= datetime('now', 'start of day')").fetchall()
        day_pnl = sum(float(r[0]) for r in today_trades if r[0]) if today_trades else 0
        c.close()
        last3 = "; ".join(f"{r[0]} {r[1]} ${r[2]:+.1f}" for r in recent) if recent else "none"
        issues = []
        if dd > 5: issues.append(f"DD {dd:.1f}% > 5%")
        if last_24h == 0: issues.append("No trades in 24h")
        if day_pnl < -eq * 0.02: issues.append(f"Daily loss ${day_pnl:.0f} > 2% equity")
        paused = state.get("bot_paused", "false").strip('"')
        if paused == "true": issues.append("BOT PAUSED")
        info = f"{label}: ${eq:.0f} ({'+' if day_pnl>=0 else ''}${day_pnl:.0f} today) DD {dd:.1f}% {len(pos)}pos {trade_count}t"
        return info, issues, last3, eq, day_pnl, dd, paused == "true"
    except Exception as e:
        return f"{label}: error {e}", [f"ERROR: {e}"], "", 0, 0, 0, False

async def send(tok, chat, msg):
    if tok and chat:
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg})

async def main():
    tok = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN","")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT_ID","")
    ci, ci2, cr, ceq, cp, cdd, cpaused = get_stats("/opt/hermes-trading-bot/data/hermes.db", "CONS")
    ai, ai2, ar, _, _, _, _ = get_stats("/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db", "AGGR")
    all_issues = ci2 + ai2

    lines = ["Hermes Status Report", ci, "  recent: " + cr, ai, "  recent: " + ar]
    if all_issues:
        lines.append("")
        lines.append("ISSUES DETECTED:")
        for issue in all_issues: lines.append("  " + issue)

    should_pause = False
    reason = ""
    if cdd > 8:
        should_pause = True; reason = f"drawdown {cdd:.1f}% > 8%"
    if cp < -ceq * 0.04:
        should_pause = True; reason = f"daily loss ${cp:.0f} > 4% equity"

    if should_pause and not cpaused:
        try:
            c = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
            c.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('bot_paused', ?)", ('"true"',))
            c.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('pause_reasons', ?)", ('["auto_watchdog: ' + reason + '"]',))
            c.commit()
            c.close()
            lines.append("")
            lines.append("AUTO-PAUSED: " + reason)
        except Exception as e:
            lines.append("")
            lines.append("AUTO-PAUSE FAILED: " + str(e))

    await send(tok, chat, "\n".join(lines))

asyncio.run(main())
