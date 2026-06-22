#!/usr/bin/env python3
"""Daily dashboard accuracy audit — verifies API responses match DB truth."""
import sqlite3, json, http.client, sys
from pathlib import Path

PRIMARY_DB = Path("/opt/hermes-trading-bot/data/hermes.db")
AGGRESSIVE_DB = Path("/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db")

def get_db_stats(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    equity = c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()
    peak = c.execute("SELECT value FROM state WHERE key='paper_peak_equity'").fetchone()
    trades = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    wins = c.execute("SELECT COUNT(*) FROM trades WHERE pnl_dollars > 0").fetchone()[0]
    total_pnl = c.execute("SELECT COALESCE(SUM(pnl_dollars), 0) FROM trades").fetchone()[0]
    pf_row = c.execute("""
        SELECT COALESCE(
            ABS(SUM(CASE WHEN pnl_dollars > 0 THEN pnl_dollars ELSE 0 END)) /
            NULLIF(ABS(SUM(CASE WHEN pnl_dollars < 0 THEN pnl_dollars ELSE 0 END)), 0), 0
        ) FROM trades
    """).fetchone()
    conn.close()
    return {
        "equity": round(float(equity[0].strip('"').strip("'")), 2) if equity else 0,
        "peak": round(float(peak[0].strip('"').strip("'")), 2) if peak else 0,
        "total_trades": trades,
        "win_rate": round(wins / trades, 4) if trades else 0,
        "profit_factor": round(pf_row[0], 2) if pf_row else 0,
        "total_pnl": round(total_pnl, 2),
    }

def fetch_api(path):
    try:
        conn = http.client.HTTPConnection("localhost", 8081, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        return json.loads(resp.read().decode()) if resp.status == 200 else None
    except Exception:
        return None

def audit(label, db_stats, api_data):
    errors = []
    checks = [
        ("equity", "equity", lambda a, b: abs(a - b) < 1),
        ("total_trades", "total_trades", lambda a, b: a == b),
        ("win_rate", "win_rate", lambda a, b: abs(a - b) < 0.02),
        ("profit_factor", "profit_factor", lambda a, b: abs(a - b) < 0.1),
    ]
    for db_key, api_key, check in checks:
        db_val = db_stats[db_key]
        api_val = api_data.get(api_key, 0) if api_data else None
        if api_val is None:
            errors.append(f"{api_key}: API unreachable")
        elif not check(db_val, api_val):
            errors.append(f"{api_key}: DB={db_val} API={api_val}")
    return errors

# Audit conservative (from /api/status)
db_con = get_db_stats(PRIMARY_DB)
api_con = fetch_api("/api/status")
err_con = audit("Conservative", db_con, api_con)

# Audit aggressive (from /api/compare) 
db_agg = get_db_stats(AGGRESSIVE_DB)
api_agg = fetch_api("/api/compare")
err_agg = audit("Aggressive", db_agg, api_agg)

result = {
    "conservative": {"db": db_con, "api": api_con, "errors": err_con},
    "aggressive": {"db": db_agg, "api": api_agg, "errors": err_agg},
    "healthy": len(err_con) == 0 and len(err_agg) == 0,
}
print(json.dumps(result, indent=2))
sys.exit(0 if result["healthy"] else 1)
