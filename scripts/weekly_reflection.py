import sqlite3, os, asyncio, httpx, json, statistics
from datetime import datetime, timezone, timedelta
from collections import defaultdict

def autocorr(values):
    if len(values) < 10: return None
    x = [float(v) for v in values[-50:]]
    n = len(x)
    m = sum(x)/n
    num = sum((x[i]-m)*(x[i+1]-m) for i in range(n-1))
    den = sum((x[i]-m)**2 for i in range(n))
    return num/den if den else 0

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
    t = sum(b.values()) or 1
    return {k: round(v/t*100) for k,v in b.items()}

async def main():
    tok = os.environ.get("HERMES_TELEGRAM_BOT_TOKEN","")
    chat = os.environ.get("HERMES_TELEGRAM_CHAT_ID","")
    c = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
    
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=7)).isoformat()[:19]
    eq = float(c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()[0].strip('"'))
    peak = float(c.execute("SELECT value FROM state WHERE key='paper_peak_equity'").fetchone()[0].strip('"'))
    gs = int(c.execute("SELECT value FROM state WHERE key='risk_global_loss_streak'").fetchone()[0].strip('"'))
    sh = c.execute("SELECT COUNT(*) FROM state WHERE key='self_heal'").fetchone()[0]
    total_t = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    week_t = c.execute("SELECT COUNT(*) FROM trades WHERE created_at >= ?", (start,)).fetchone()[0]
    
    all_r = [float(t[0]) for t in c.execute("SELECT r_multiple FROM trades WHERE r_multiple IS NOT NULL").fetchall()]
    all_time_r = statistics.mean(all_r) if all_r else 0
    
    today_str = now.strftime("%Y-%m-%d")
    lines = [f"Weekly Review — {today_str} | ${eq:.0f} (peak ${peak:.0f}, DD {(peak-eq)/peak*100:.1f}%)"]
    lines.append(f"Week: {week_t}t / Lifetime: {total_t}t | Global streak: {gs} | Self-heal events: {sh}")
    
    for sleeve in ["mr","trend","xs_momentum","donchian"]:
        week = c.execute("SELECT pnl_dollars,r_multiple,mae_pct,mfe_pct,entry_time,created_at FROM trades WHERE strategy=? AND created_at >= ? ORDER BY created_at", (sleeve, start)).fetchall()
        pnls_w = [float(r[0]) for r in week if r[0]]
        n_w = len(pnls_w)
        total_s = c.execute("SELECT COUNT(*) FROM trades WHERE strategy=?", (sleeve,)).fetchone()[0]
        if n_w == 0:
            lines.append(f"\n[{sleeve.upper()}] 0t this week")
            continue
        
        wr = sum(1 for p in pnls_w if p>0)/n_w*100
        avg = sum(pnls_w)/n_w
        total_w = sum(pnls_w)
        rv = [float(r[1]) for r in week if r[1]]
        rr = statistics.mean(rv) if rv else 0
        mom = rr - all_time_r
        
        hist = r_hist(rv)
        oc = [1 if float(r[0])>0 else 0 for r in week]
        ac = autocorr(oc)
        
        maes = [float(r[2]) for r in week if r[2] is not None]
        mfes = [float(r[3]) for r in week if r[3] is not None]
        ama = sum(maes)/len(maes) if maes else 0
        amf = sum(mfes)/len(mfes) if mfes else 0
        
        # Daily PnL trend
        pd = defaultdict(float)
        for p_str, day in [(r[0],r[5][:10]) for r in week if r[0]]:
            pd[day] += float(p_str)
        tr = " | ".join(f"{d[-5:]}:${pnl:+.0f}" for d,pnl in sorted(pd.items()))
        
        # Duration
        de, dl = [], []
        for entry,ctime,rv2 in [(r[4],r[5],r[1]) for r in week]:
            if not entry or not ctime or not rv2: continue
            try:
                e = datetime.strptime(entry[:19],"%Y-%m-%dT%H:%M:%S") if "T" in entry else datetime.strptime(entry[:19],"%Y-%m-%d %H:%M:%S")
                x = datetime.strptime(ctime[:19],"%Y-%m-%d %H:%M:%S")
                h = (x-e).total_seconds()/3600
                rvv = float(rv2)
                if h<1: de.append(rvv)
                else: dl.append(rvv)
            except: pass
        
        # ── Four diagnostic answers ──
        diag_lines = [f"\n[{sleeve.upper()}] {n_w}t WR={wr:.0f}% | ${avg:.2f}/t | Total ${total_w:+.0f}"]
        
        # Q1: Edge decay?
        diag_lines.append("  Q1: Edge decaying?" if mom < -0.05 else "  Q1: Edge stable or strengthening")
        if mom < -0.05:
            diag_lines.append(f"    Expectancy momentum {mom:.4f} — avg R dropping vs baseline {all_time_r:.4f}")
        else:
            diag_lines.append(f"    Expectancy momentum {mom:.4f} (within normal range)")
        
        # Q2: Stops too tight?
        if ama and amf:
            ratio = amf/ama
            diag_lines.append("  Q2: Stops too tight?" if ratio < 1.5 else "  Q2: Stop distance adequate")
            diag_lines.append(f"    MAE={ama:.1f}% MFE={amf:.1f}% ratio={ratio:.1f}x {'(MFE > 1.5x MAE = OK)' if ratio >= 1.5 else '(MFE < 1.5x MAE = stops tight)'}")
        
        # Q3: Holding dead trades?
        if de and dl:
            er = sum(de)/len(de)
            lr = sum(dl)/len(dl)
            diag_lines.append("  Q3: Holding too long?" if lr < er else "  Q3: Hold time appropriate")
            diag_lines.append(f"    Trades <1h: R={er:.3f} | >1h: R={lr:.3f} {'(hold <1h outperforms)' if er > lr else '(hold >1h outperforms)'}")
        
        # Q4: Loss clustering?
        if ac is not None:
            diag_lines.append("  Q4: Loss clustering?" if ac > 0.3 else "  Q4: Outcomes independent")
            diag_lines.append(f"    Lag-1 autocorr={ac:.2f} {'( >0.3 = losses cluster)' if ac > 0.3 else '( <0.3 = normal variance)'}")
        
        if hist:
            h_str = " ".join(f"{k}:{v}%" for k,v in hist.items())
            diag_lines.append(f"  R-multiple dist: {h_str}")
        
        # Time-of-Day heatmap
        hours = [0]*24
        for p_str, ct in [(r[0], r[5]) for r in week if r[0] and r[5]]:
            try:
                h = int(ct[11:13])
                hours[h] += float(p_str) if float(p_str) != 0 else 0
            except: pass
        tod = " ".join(f"{i:02d}:${h:+.0f}" for i, h in enumerate(hours) if h != 0)
        if tod:
            diag_lines.append(f"  Time: {tod[:80]}")
        
        # Drawdown duration (portfolio level)
        snapshots = c.execute("SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 100").fetchall()
        if len(snapshots) >= 10:
            peak = max(float(s[0]) for s in snapshots)
            cur = float(snapshots[0][0])
            depth = 0
            for s in snapshots:
                if float(s[0]) >= peak * 0.99: break
                depth += 1
            diag_lines.append(f"  Port DD: {(peak-cur)/peak*100:.1f}% ({depth*5}min recovery)")
        
        diag_lines.append(f"  Day trend: {tr}")
        lines.extend(diag_lines)
    
    lines.append(f"\n[RISK] GS={gs} | Self-heal: {sh}")
    msg = "\n".join(lines)
    
    if tok and chat:
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg})
    
    c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (f"weekly_recs_{today_str}", json.dumps(lines)))
    c.commit()
    c.close()

asyncio.run(main())
