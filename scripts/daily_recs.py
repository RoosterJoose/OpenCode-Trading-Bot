import sqlite3, os, asyncio, httpx, statistics
from datetime import datetime

def autocorrelation(values):
    if len(values) < 10: return None
    x = [float(v) for v in values[-50:]]
    n = len(x)
    mean = sum(x) / n
    num = sum((x[i] - mean) * (x[i+1] - mean) for i in range(n-1))
    den = sum((x[i] - mean) ** 2 for i in range(n))
    return num / den if den else 0

def r_hist(r_vals):
    vals = [float(r) for r in r_vals if r and float(r) != 0]
    if len(vals) < 5: return None
    b = {"<-1R":0,"-1-0R":0,"0-1R":0,"1-2R":0,"2-3R":0,"3+R":0}
    for v in vals:
        if v < -1: b["<-1R"]+=1
        elif v < 0: b["-1-0R"]+=1
        elif v < 1: b["0-1R"]+=1
        elif v < 2: b["1-2R"]+=1
        elif v < 3: b["2-3R"]+=1
        else: b["3+R"]+=1
    t = sum(b.values()) or 1
    return {k: round(v/t*100) for k,v in b.items()}

async def main():
    tok = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
    c = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
    eq = float(c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()[0].strip('"'))
    peak = float(c.execute("SELECT value FROM state WHERE key='paper_peak_equity'").fetchone()[0].strip('"'))
    gs = int(c.execute("SELECT value FROM state WHERE key='risk_global_loss_streak'").fetchone()[0].strip('"'))
    sh_count = c.execute("SELECT COUNT(*) FROM state WHERE key='self_heal'").fetchone()[0]
    
    all_r = [float(t[0]) for t in c.execute("SELECT r_multiple FROM trades WHERE r_multiple IS NOT NULL").fetchall()]
    all_time_r = statistics.mean(all_r) if all_r else 0
    
    sections = [f"Daily Recs — ${eq:.0f} (${peak:.0f} pk, DD {(peak-eq)/peak*100:.1f}%)"]
    
    for sleeve in ["mr","trend","xs_momentum","donchian"]:
        recent = c.execute("SELECT pnl_dollars,r_multiple,mae_pct,mfe_pct,entry_time,created_at FROM trades WHERE strategy=? ORDER BY id DESC LIMIT 20", (sleeve,)).fetchall()
        pnls_s = [float(r[0]) for r in recent if r[0]]
        n = len(pnls_s)
        total = c.execute("SELECT COUNT(*) FROM trades WHERE strategy=?", (sleeve,)).fetchone()[0]
        if n == 0:
            sections.append(f"\n[{sleeve.upper()}] 0t this window ({total} lifetime)")
            continue
        
        wr = sum(1 for p in pnls_s if p > 0) / n * 100
        avg_pnl = sum(pnls_s) / n
        r_vals = [float(r[1]) for r in recent if r[1]]
        rolling_r = statistics.mean(r_vals) if r_vals else 0
        exp_mom = rolling_r - all_time_r
        
        hist = r_hist(r_vals)
        outcomes = [1 if float(r[0]) > 0 else 0 for r in recent]
        ac = autocorrelation(outcomes)
        
        maes = [float(r[2]) for r in recent if r[2] is not None]
        mfes = [float(r[3]) for r in recent if r[3] is not None]
        avg_mae = sum(maes)/len(maes) if maes else 0
        avg_mfe = sum(mfes)/len(mfes) if mfes else 0
        
        dur_e, dur_l = [], []
        for r_val_str, e_str, c_str in [(r[1], r[4], r[5]) for r in recent]:
            if not r_val_str or not e_str or not c_str: continue
            try:
                e = datetime.strptime(e_str[:19], "%Y-%m-%dT%H:%M:%S") if "T" in e_str else datetime.strptime(e_str[:19], "%Y-%m-%d %H:%M:%S")
                x = datetime.strptime(c_str[:19], "%Y-%m-%d %H:%M:%S")
                h = (x - e).total_seconds() / 3600
                rv = float(r_val_str)
                if h < 1: dur_e.append(rv)
                else: dur_l.append(rv)
            except: pass
        
        # Diagnostics
        diag = []
        if exp_mom < -0.05:
            diag.append("EDGE DECAYING — R dropping vs baseline")
        elif abs(exp_mom) < 0.05:
            diag.append("Edge stable")
        else:
            diag.append("Edge strengthening")
        
        mae_mfe_ok = "Stops OK" if avg_mfe > avg_mae * 1.5 else "STOPS TIGHT? MFE/MAE={:.1f}".format(avg_mfe/avg_mae) if avg_mae else "No MAE data"
        diag.append(mae_mfe_ok)
        
        if dur_e and dur_l:
            er = sum(dur_e)/len(dur_e)
            lr = sum(dur_l)/len(dur_l)
            diag.append("Hold <1h beats >1h" if er > lr else "Hold longer pays" if lr > er else "Hold time neutral")
        
        if ac is not None:
            diag.append("Loss clustering" if ac > 0.3 else "Outcomes independent" if abs(ac) < 0.3 else "W/L alternating")
        
        lines = [f"\n[{sleeve.upper()}]"]
        lines.append(f"  {n}t WR={wr:.0f}% ${avg_pnl:.2f} R={rolling_r:.3f} Mom={exp_mom:+.4f}")
        if hist: lines.append("  R:" + " ".join(f"{k}:{v}%" for k,v in hist.items()))
        lines.append(f"  MAE={avg_mae:.1f}% MFE={avg_mfe:.1f}% " + " | ".join(diag))
        
        tm = c.execute("SELECT value FROM state WHERE key=?", (f"{sleeve}_last_mod_cycle",)).fetchone()
        mod = total - int(tm[0]) if tm else 999
        if mod < 30: lines.append(f"  LOCKED ({mod}/30)")
        elif exp_mom < -0.05 and wr < 40: lines.append("  Rec: TIGHTEN")
        elif ac and ac > 0.3: lines.append("  Rec: REVIEW")
        else: lines.append("  Rec: Hold")
        sections.append("\n".join(lines))
    
    sections.append(f"\n[RISK] GS={gs} | Self-heal: {sh_count}")
    c.close()
    
    msg = "\n".join(sections)
    if tok and chat:
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg})

asyncio.run(main())
