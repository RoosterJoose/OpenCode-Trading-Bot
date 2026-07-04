import sqlite3, os, asyncio, httpx, json, statistics
from datetime import datetime, timezone

def ac(values):
    if len(values) < 10: return None
    x = [float(v) for v in values[-50:]]
    n, m = len(x), sum(x)/len(x)
    return (sum((x[i]-m)*(x[i+1]-m) for i in range(n-1))/(sum((x[i]-m)**2 for i in range(n))+1e-9)) if n>10 else None

def r_hist(rv):
    vals = [float(r) for r in rv if r and float(r)!=0]
    if len(vals)<5: return None
    b = {"<-1R":0,"-1-0R":0,"0-1R":0,"1-2R":0,"2-3R":0,"3+R":0}
    for v in vals:
        if v<-1: b["<-1R"]+=1
        elif v<0: b["-1-0R"]+=1
        elif v<1: b["0-1R"]+=1
        elif v<2: b["1-2R"]+=1
        elif v<3: b["2-3R"]+=1
        else: b["3+R"]+=1
    t = sum(b.values())+1e-9
    return {k: round(v/t*100) for k,v in b.items()}

async def main():
    tok = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN","")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT_ID","")
    c = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
    eq = float(c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()[0].strip('"'))
    peak = float(c.execute("SELECT value FROM state WHERE key='paper_peak_equity'").fetchone()[0].strip('"'))
    gs = int(c.execute("SELECT value FROM state WHERE key='risk_global_loss_streak'").fetchone()[0].strip('"'))
    sh = c.execute("SELECT COUNT(*) FROM state WHERE key='self_heal'").fetchone()[0]
    
    all_r = [float(t[0]) for t in c.execute("SELECT r_multiple FROM trades WHERE r_multiple IS NOT NULL").fetchall()]
    base_r = statistics.mean(all_r) if all_r else 0
    
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"Daily Recs — {today_str} | ${eq:.0f} (pk ${peak:.0f}) GS={gs}"]
    
    for sleeve in ["mr","trend","xs_momentum","donchian"]:
        recent = c.execute("SELECT pnl_dollars,r_multiple,mae_pct,mfe_pct,entry_time,created_at FROM trades WHERE strategy=? ORDER BY id DESC LIMIT 20", (sleeve,)).fetchall()
        pnls = [float(r[0]) for r in recent if r[0]]
        n = len(pnls)
        total = c.execute("SELECT COUNT(*) FROM trades WHERE strategy=?", (sleeve,)).fetchone()[0]
        if n == 0:
            lines.append(f"\n[{sleeve.upper()}] No recent trades ({total} lifetime)")
            continue
        
        wr = sum(1 for p in pnls if p>0)/n*100
        avg = sum(pnls)/n
        rv = [float(r[1]) for r in recent if r[1]]
        rr = statistics.mean(rv) if rv else 0
        mom = rr - base_r
        
        hist = r_hist(rv)
        oc = [1 if float(r[0])>0 else 0 for r in recent]
        auto_c = ac(oc)
        maes = [float(r[2]) for r in recent if r[2] is not None]
        mfes = [float(r[3]) for r in recent if r[3] is not None]
        ama = sum(maes)/len(maes) if maes else 0
        amf = sum(mfes)/len(mfes) if mfes else 0
        
        # Duration
        de, dl = [], []
        for entry,ct,rv2 in [(r[4],r[5],r[1]) for r in recent]:
            if not entry or not ct or not rv2: continue
            try:
                e = datetime.strptime(entry[:19],"%Y-%m-%dT%H:%M:%S") if "T" in entry else datetime.strptime(entry[:19],"%Y-%m-%d %H:%M:%S")
                x = datetime.strptime(ct[:19],"%Y-%m-%d %H:%M:%S")
                h = (x-e).total_seconds()/3600
                rvv = float(rv2)
                if h<1: de.append(rvv)
                else: dl.append(rvv)
            except: pass
        
        diag = [f"\n[{sleeve.upper()}] {n}t WR={wr:.0f}% ${avg:.2f}/t | R={rr:.3f} mom={mom:+.4f}"]
        diag.append("  Q1 Edge: " + ("DECAYING" if mom < -0.05 else "STABLE" if abs(mom)<0.05 else "STRENGTHENING"))
        if ama and amf:
            ratio = amf/ama
            diag.append("  Q2 Stops: " + ("OK ({:.1f}x MFE/MAE)".format(ratio)) if ratio>=1.5 else "TIGHT ({:.1f}x)".format(ratio))
        if de and dl:
            er = sum(de)/len(de); lr = sum(dl)/len(dl)
            diag.append("  Q3 Hold: " + ("<1h beats >1h" if er>lr else ">1h beats <1h" if lr>er else "neutral"))
        if auto_c is not None:
            diag.append("  Q4 Clustering: " + ("YES ({:.2f})".format(auto_c) if auto_c>0.3 else "NO ({:.2f})".format(auto_c)))
        if hist:
            diag.append("  R: " + " ".join(f"{k}:{v}%" for k,v in hist.items()))
        
        tm = c.execute("SELECT value FROM state WHERE key=?", (f"{sleeve}_last_mod_cycle",)).fetchone()
        mod = total - int(tm[0]) if tm else 999
        if mod < 30: diag.append("  LOCKED (" + str(mod) + "/30)")
        elif mom < -0.05 and wr < 40: diag.append("  Rec: TIGHTEN")
        elif auto_c and auto_c > 0.3: diag.append("  Rec: REVIEW")
        else: diag.append("  Rec: Hold")
        lines.extend(diag)
    
    lines.append(f"\n[RISK] GS={gs} | Self-heal events: {sh}")
    msg = "\n".join(lines)
    
    if tok and chat:
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg})
    c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (f"daily_recs_{today_str}", json.dumps(lines)))
    c.commit()
    c.close()

asyncio.run(main())
