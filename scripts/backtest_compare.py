#!/usr/bin/env python3
"""Compare old vs new logic — replay last 30 days of trade data through both PnL models."""
import sqlite3, json, statistics, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DB_PATHS = [
    ("CONS", "/opt/hermes-trading-bot/data/hermes.db"),
    ("AGGR", "/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db"),
]

def sharpe(daily_pnls):
    if len(daily_pnls) < 3:
        return None
    m = statistics.mean(daily_pnls)
    try:
        s = statistics.stdev(daily_pnls)
    except (statistics.StatisticsError, ValueError):
        return None
    if s < 1e-9:
        return None
    return m / s * (len(daily_pnls) ** 0.5) if s > 0 else 0

def compute():
    for label, path in DB_PATHS:
        print(f"\n{'='*60}")
        print(f"  {label} — {path}")
        print(f"{'='*60}")
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            # Get last 30 days of trades
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            rows = conn.execute(
                "SELECT exit_time, pnl_dollars, entry_price, exit_price, strategy, side, size, fees, funding_paid, r_multiple "
                "FROM trades WHERE exit_time >= ? ORDER BY exit_time",
                (cutoff,)
            ).fetchall()
            equity_rows = conn.execute(
                "SELECT equity, timestamp FROM equity_snapshots ORDER BY timestamp DESC LIMIT 2"
            ).fetchall()
            conn.close()
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        if not rows:
            print("  No trades in last 30 days")
            continue

        # Old logic: raw PnL without fees/funding (approximate pre-fix model)
        old_daily = defaultdict(float)
        # New logic: PnL with fees+funding deducted (post-fix model)
        new_daily = defaultdict(float)
        current_pnl = 0.0
        total_fees = 0.0
        total_funding = 0.0

        for r in rows:
            exit_time = r["exit_time"]
            if not exit_time:
                continue
            day = exit_time[:10] if isinstance(exit_time, str) else str(exit_time)[:10]
            pnl = r["pnl_dollars"] or 0.0
            fees = abs(r["fees"] or 0.0)
            funding = abs(r["funding_paid"] or 0.0)

            # Old: just raw pnl (no fees/funding)
            old_daily[day] += pnl
            # New: pnl minus costs
            new_daily[day] += pnl - fees - funding
            total_fees += fees
            total_funding += funding

        if not old_daily:
            print("  No daily data")
            continue

        days = sorted(set(list(old_daily.keys()) + list(new_daily.keys())))
        old_equity = [0.0]
        new_equity = [0.0]

        for d in days:
            old_equity.append(old_equity[-1] + old_daily.get(d, 0))
            new_equity.append(new_equity[-1] + new_daily.get(d, 0))

        old_final = old_equity[-1]
        new_final = new_equity[-1]

        if len(days) < 2:
            print(f"  Only {len(days)} days of data — insufficient for comparison")
            continue

        old_daily_vals = [old_daily.get(d, 0) for d in days]
        new_daily_vals = [new_daily.get(d, 0) for d in days]

        old_shp = sharpe(old_daily_vals)
        new_shp = sharpe(new_daily_vals)

        # Max drawdown
        def max_dd(eq_curve):
            peak = eq_curve[0]
            dd = 0
            for v in eq_curve:
                if v > peak:
                    peak = v
                dd_val = (peak - v) / peak * 100 if peak > 0 else 0
                if dd_val > dd:
                    dd = dd_val
            return dd

        old_dd = max_dd(old_equity)
        new_dd = max_dd(new_equity)

        # Profit factor
        old_pos = sum(v for v in old_daily_vals if v > 0) or 1
        old_neg = abs(sum(v for v in old_daily_vals if v < 0)) or 1
        new_pos = sum(v for v in new_daily_vals if v > 0) or 1
        new_neg = abs(sum(v for v in new_daily_vals if v < 0)) or 1
        old_pf = old_pos / old_neg
        new_pf = new_pos / new_neg

        print(f"  {'Metric':<20} {'Old Logic':>12} {'New Logic':>12} {'Change':>12}")
        print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*12}")
        print(f"  {'Ending PnL':<20} {old_final:>10.2f}  {new_final:>10.2f}  {new_final - old_final:>+10.2f}")
        print(f"  {'Sharpe (daily)':<20} {old_shp:>10.2f}  {new_shp:>10.2f}  {'' if old_shp is None or new_shp is None else f'{new_shp - old_shp:+>+10.2f}'}")
        print(f"  {'Max DD %':<20} {old_dd:>10.2f}%  {new_dd:>10.2f}%  {new_dd - old_dd:>+10.2f}%")
        print(f"  {'Profit Factor':<20} {old_pf:>10.2f}  {new_pf:>10.2f}  {new_pf - old_pf:>+10.2f}")
        print(f"  {'Total Fees':<20} {'':>12} {total_fees:>10.2f}")
        print(f"  {'Total Funding':<20} {'':>12} {total_funding:>10.2f}")
        print(f"  {'Days':<20} {len(days):>10}")
        print(f"  {'Trades':<20} {len(rows):>10}")

        # Verdict
        if new_final > old_final:
            print(f"\n  ✓ New logic outperforms old logic (fees+funding are smaller than edge improvement)")
        elif new_final < old_final:
            print(f"\n  ✗ New logic does NOT outperform old logic by {abs(new_final - old_final):.2f} — fees+funding exceed benefit")
        else:
            print(f"\n  → No change")

if __name__ == "__main__":
    compute()
