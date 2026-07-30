#!/usr/bin/env python3
"""Strategy-level edge audit — reads both DBs, computes per-strategy metrics."""
import sqlite3, json, statistics, sys
from collections import defaultdict

DB_PATHS = [
    ("CONS", "/opt/hermes-trading-bot/data/hermes.db"),
    ("AGGR", "/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db"),
]

def sharpe(r_vals):
    if len(r_vals) < 3:
        return None
    m = statistics.mean(r_vals) if len(r_vals) > 0 else 0
    if abs(m) < 1e-9:
        return None
    try:
        s = statistics.stdev(r_vals)
    except (statistics.StatisticsError, ValueError):
        return None
    if s < 1e-9:
        return None
    return m / s * (len(r_vals) ** 0.5)

def analyze():
    for label, path in DB_PATHS:
        print(f"\n{'='*60}")
        print(f"  {label} — {path}")
        print(f"{'='*60}")
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT strategy, pnl_dollars, pnl_pct, r_multiple FROM trades WHERE strategy IS NOT NULL AND strategy != ''"
            ).fetchall()
            conn.close()
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        by_strat = defaultdict(list)
        for r in rows:
            by_strat[r["strategy"]].append(r)

        if not by_strat:
            print("  No trades found")
            continue

        print(f"  {'Strategy':<20} {'Trades':>7} {'WR%':>7} {'AvgWin%':>9} {'AvgLoss%':>9} {'PF':>7} {'Sharpe':>8} {'TotPnL':>10} {'Status'}")
        print(f"  {'-'*20} {'-'*7} {'-'*7} {'-'*9} {'-'*9} {'-'*7} {'-'*8} {'-'*10} {'-'*15}")

        for strat in sorted(by_strat.keys()):
            trades = by_strat[strat]
            n = len(trades)
            pnls = [t["pnl_dollars"] or 0 for t in trades]
            pcts = [t["pnl_pct"] or 0 for t in trades]
            r_vals = [t["r_multiple"] or 0 for t in trades]

            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]

            wr = len(wins) / n * 100 if n > 0 else 0
            avg_win_pct = statistics.mean([p for p in pcts if p > 0]) * 100 if [p for p in pcts if p > 0] else 0.0
            avg_loss_pct = statistics.mean([p for p in pcts if p <= 0]) * 100 if [p for p in pcts if p <= 0] else 0.0
            pf = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else float('inf')
            shp = sharpe(r_vals)
            tot = sum(pnls)
            shp_str = f'{shp:.2f}' if shp is not None else 'N/A'

            if n < 30:
                status = "INSUFFICIENT DATA"
            elif pf < 1.0 and n >= 100:
                status = "LIKELY NEGATIVE — consider disabling"
            elif pf < 1.0:
                status = "negative PF (<100 trades)"
            else:
                status = "OK"

            print(f"  {strat:<20} {n:>7} {wr:>6.1f}% {avg_win_pct:>8.2f}% {avg_loss_pct:>8.2f}% {pf:>6.2f} {shp_str:>7} {tot:>8.2f}  {status}")

if __name__ == "__main__":
    analyze()
