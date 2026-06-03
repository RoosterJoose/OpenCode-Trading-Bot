#!/usr/bin/env python3
import json, urllib.request

def get(p):
    return json.loads(urllib.request.urlopen(urllib.request.Request('http://localhost:8081' + p), timeout=5).read())

s = get('/api/status')
pos = get('/api/positions')
trades = get('/api/trades')
dr = get('/api/daily-reflection')
learnings = get('/api/learnings')

eq = float(s.get('equity', 10000))
peak = float(s.get('peak_equity', eq))
dd = (peak - eq) / peak * 100 if peak > 0 else 0
start_pnl = eq - 10000
start_pnl_pct = start_pnl / 10000 * 100

print('=== BOT STATUS ===')
print(f'  Equity: ${eq:,.2f}  Starting: $10,000  P&L: ${start_pnl:+,.2f} ({start_pnl_pct:+.2f}%)')
print(f'  Peak: ${peak:,.2f}  DD: {dd:.2f}%  Exposure: ${float(s.get("gross_exposure",0)):,.2f}')
print(f'  Leverage: {float(s.get("effective_leverage",0)):.2f}x  Trades: {s.get("total_trades",0)}')
print(f'  Win Rate: {float(s.get("win_rate",0))*100:.1f}%  PF: {float(s.get("profit_factor",0)):.2f}')

print('\n=== OPEN POSITIONS ===')
total_upnl = 0.0
for p in pos:
    upnl = float(p.get('unrealized_pnl', 0))
    total_upnl += upnl
    print(f'  {p["asset"]:<6} {p["side"]:<6} entry=${float(p["entry_price"]):>8,.2f}  upnl=${upnl:>+7.2f}  lev={float(p.get("leverage",1)):.1f}x  stop=${float(p.get("stop_loss",0)):>8,.2f}')
print(f'  Total unrealized: ${total_upnl:+,.2f}')

print('\n=== CLOSED TRADES ===')
for t in trades:
    pnl = float(t.get('pnl_pct', 0))
    d = float(t.get('pnl_dollars', 0))
    print(f'  #{t["id"]} {t["asset"]:<6} {t["side"]:<6} {t["strategy"]:<10} pnl={pnl:+.2f}% ${d:+,.2f}  reason={t.get("exit_reason","")}')

print('\n=== DAILY REFLECTION ===')
print(f'  {dr.get("date","-")} bias={dr.get("bias","-")} sig={dr.get("significant_moves",0)} missed={dr.get("missed_moves",0)}')

print('\n=== CUMULATIVE LEARNINGS ===')
c = learnings.get('cumulative', {})
print(f'  Days tracked: {c.get("total_days",0)}')
for l in c.get('lessons', []):
    print(f'  Lesson: {l}')
for r in c.get('persistent_missed_reasons', [])[:3]:
    print(f'  Pattern: {r["reason"]} ({r["count"]}x)')
