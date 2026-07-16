#!/usr/bin/env python3
"""MR rolling 20-trade win rate monitor. Reports WR, PnL, alerts on bleeding."""
import json, sqlite3, sys, os
from datetime import datetime, timezone

CONS_DB = "/opt/hermes-trading-bot/data/hermes.db"
AGGR_DB = "/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db"

def check_mr(db_path: str, label: str) -> dict:
    c = sqlite3.connect(db_path)
    rows = c.execute("""
        SELECT pnl_dollars, exit_reason FROM trades
        WHERE strategy='mr' AND exit_reason IS NOT NULL
        ORDER BY entry_time DESC LIMIT 20
    """).fetchall()
    if not rows:
        return {"label": label, "trades": 0, "wr": 0, "pnl": 0, "status": "no_trades"}
    wins = sum(1 for r in rows if r[0] > 0)
    losses = sum(1 for r in rows if r[0] < 0)
    total_pnl = sum(r[0] for r in rows)
    wr = wins / len(rows)
    avg_pnl = total_pnl / len(rows)
    exit_counts = {}
    for r in rows:
        exit_counts[r[1]] = exit_counts.get(r[1], 0) + 1
    c.close()
    return {
        "label": label,
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "wr": round(wr, 3),
        "pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 2),
        "exits": exit_counts,
    }

def main():
    results = []
    for db_path, label in [(CONS_DB, "CONS"), (AGGR_DB, "AGGR")]:
        if os.path.exists(db_path):
            results.append(check_mr(db_path, label))
    
    alerts = []
    for r in results:
        print(f"[{r['label']}] MR last 20: {r['trades']}t WR={r['wr']*100:.0f}% PnL=${r['pnl']:.2f}")
        status = "OK"
        if r['trades'] >= 10:
            if r['wr'] < 0.50:
                status = "LOW_WR"
            if r['pnl'] < -50:
                status = "BLEEDING"
            if r['pnl'] > 100:
                status = "GREEN"
        r['status'] = status
        r['timestamp'] = datetime.now(timezone.utc).isoformat()
        
        # Write to CONS DB for dashboard
        try:
            db_path = CONS_DB
            store_db = sqlite3.connect(db_path)
            store_db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                           (f"mr_rolling_20_{r['label'].lower()}", json.dumps(r)))
            store_db.commit()
            store_db.close()
        except Exception:
            pass
    
    # Alert if bleeding
    for r in results:
        if r['status'] == 'BLEEDING':
            print(f"  ⚠️  MR BLEEDING: {r['label']} last 20 trades = ${r['pnl']}")
        elif r['status'] == 'LOW_WR':
            print(f"  ⚠️  MR LOW WR: {r['label']} last 20 = {r['wr']*100:.0f}%")
        elif r['status'] == 'GREEN':
            print(f"  ✅ MR GREEN: {r['label']} last 20 = ${r['pnl']}")

if __name__ == "__main__":
    main()
