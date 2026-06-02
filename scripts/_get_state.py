import json, urllib.request, sys

def get(path):
    req = urllib.request.Request("http://localhost:8081" + path)
    return json.loads(urllib.request.urlopen(req, timeout=5).read())

pos = get("/api/positions")
print("=== OPEN POSITIONS ===")
for p in pos:
    entry = float(p["entry_price"])
    sz = float(p["size"])
    lev = float(p["leverage"])
    upnl = float(p["unrealized_pnl"])
    stop = float(p["stop_loss"])
    print(f'  {p["asset"]:<6} {p["side"]:<6} entry={entry:<10.2f} size={sz:.4f} lev={lev:.1f}x  upnl=${upnl:+.2f}  stop={stop:.2f}  strategy={p["strategy"]}')

s = get("/api/status")
print(f'\n=== STATUS ===')
eq = float(s["equity"])
print(f'  Equity: ${eq:,.2f}  Trades: {s["total_trades"]}  Win Rate: {float(s["win_rate"])*100:.1f}%')
print(f'  Exposure: ${float(s["gross_exposure"]):,.2f}  Leverage: {float(s["effective_leverage"]):.2f}x')

dr = get("/api/daily-reflection")
print(f'\n=== DAILY REFLECTION: {dr["date"]} ===')
print(f'  Bias: {dr["bias"]}  Significant: {dr["significant_moves"]}  Missed: {dr["missed_moves"]}  Catchable: {dr["potentially_catchable"]}')
print(f'  Top assets:')
for a in dr["assets"][:8]:
    arrow = "\u2191" if a["change_24h_pct"] > 0 else "\u2193"
    sig = " *" if a["significant"] else ""
    print(f'    {a["asset"]:<6} {arrow} {a["change_24h_pct"]:+7.2f}%  RSI={a["rsi"]:5.1f}  ADX={a["adx"]:5.1f}  signal={"yes" if a["had_signal"] else "no "}{sig}')
print(f'  ... {len(dr["assets"])} total')
for l in dr["learning"]:
    if l["type"] == "market_summary":
        continue
    print(f'  >> [{l["type"]}] {l["reason"]}: {l["count"]}x {l["action"]}')

# Signals
sigs = get("/api/signals")
print(f'\n=== SIGNALS (last 5) ===')
for s in sigs[-5:]:
    t = s.get("time", "")[11:19]
    print(f'  {t} {s.get("asset",""):<6} {s.get("side",""):<6} conf={float(s.get("confidence",0)):.2f} strat={s.get("strategy","")}')
EOF
