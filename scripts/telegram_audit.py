#!/usr/bin/env python3
import sqlite3, json, subprocess

def get(path, label):
    c = sqlite3.connect(path)
    state = dict(c.execute("SELECT key, value FROM state").fetchall())
    eq = float(state.get("paper_equity","0").strip('"'))
    peak = float(state.get("paper_peak_equity","0").strip('"'))
    gs = int(state.get("risk_global_loss_streak","0").strip('"'))
    pos = json.loads(state.get("positions","[]"))
    td = c.execute("SELECT COUNT(*) FROM trades WHERE created_at >= datetime('now', 'start of day')").fetchone()[0]
    nt = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    dd = (peak-eq)/peak*100 if peak>0 else 0
    c.close()
    
    lines = [f"{label}: ${eq:.0f} ({td}t today, {nt}t total, {len(pos)} pos, GS={gs}, DD {dd:.1f}%)"]
    for p in pos:
        lines.append(f"  {p['asset']:6s} {p['side']:5s} ${p['entry_price']:.0f} uP=${p.get('unrealized_pnl',0):.2f}")
    
    r = subprocess.run(["sudo","journalctl","-u","hermes-bot" if "aggre" not in path else "hermes-bot-aggressive","--since","5 minutes ago","--no-pager"],capture_output=True,text=True)
    errs = [l for l in r.stdout.split('\n') if 'ERROR' in l and '429' not in l and 'Unbound' not in l]
    if errs:
        for e in errs[:3]:
            lines.append(f"  ERROR: {e[80:150] if len(e)>80 else e}")
    
    lines.append("  Status: OK" if not errs else f"  Status: {len(errs)} errors")
    return "\n".join(lines)

msg = get("/opt/hermes-trading-bot/data/hermes.db", "CONS")
msg += "\n" + get("/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db", "AGGR")
print(msg)
