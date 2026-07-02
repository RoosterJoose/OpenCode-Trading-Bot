#!/bin/bash
source /opt/hermes-trading-bot/.env
token=$HERMES_TELEGRAM_BOT_TOKEN
chat=$HERMES_TELEGRAM_CHAT_ID
msg=$(python3 -c "
import sqlite3, json
c = sqlite3.connect('/opt/hermes-trading-bot/data/hermes.db')
eq = float(c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()[0].strip('"'))
peak = float(c.execute("SELECT value FROM state WHERE key='paper_peak_equity'").fetchone()[0].strip('"'))
mr = c.execute("SELECT COUNT(*) FROM trades WHERE strategy='mr' AND created_at >= '2026-07-01'").fetchone()[0]
mr_pnl = c.execute("SELECT COALESCE(SUM(pnl_dollars),0) FROM trades WHERE strategy='mr' AND created_at >= '2026-07-01'").fetchone()[0]
dd = (peak-eq)/peak*100 if peak>0 else 0
status = 'READY for go-live review' if mr_pnl > 0 and dd < 10 else 'NOT READY'
print('Hermes 7-Day Go-Live Review')
print('Equity: ${:.0f} (DD {:.1f}%)'.format(eq, dd))
print('MR since Jul 1: {}t +${:.0f}'.format(mr, mr_pnl))
print('Status: ' + status)
c.close()
")
curl -s -X POST "https://api.telegram.org/bot${token}/sendMessage" -d "chat_id=${chat}&text=${msg}" > /dev/null
