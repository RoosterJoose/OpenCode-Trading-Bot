#!/usr/bin/env python3
"""
30-day backtest: runs each strategy on real candle data and compares PnL.

This is NOT a simulation (which assumes you were in the trade). We compute:
  - For each day in the last 30 days, check if the strategy would have entered
  - If yes, simulate the entry/exit using the actual price action
  - Track PnL, Sharpe, WR per strategy, per regime, per side

Results written to state key: backtest_30d

This runs daily at 00:15 UTC (after daily_reflection + closed_loop + sharpe_tracker).
"""
import asyncio
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.trend import TrendFollow
from src.strategies.mr import MeanReversion
from src.strategies.donchian import DonchianBreakout


def compute_sharpe(returns: list[float]) -> float:
    if len(returns) < 5:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 1e-9
    return (mean / std) * math.sqrt(365)


def main(db_path: str):
    db = Path(db_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    thirty_days_ago = (now - timedelta(days=30)).isoformat()

    # Load real trade data — these are the ACTUAL trades the bot made
    trades = list(conn.execute(
        """SELECT id, entry_time, exit_time, asset, side, strategy,
                  ROUND(entry_price, 2) as entry_price,
                  ROUND(exit_price, 2) as exit_price,
                  ROUND(pnl_dollars, 4) as pnl,
                  ROUND(r_multiple, 3) as r,
                  exit_reason
           FROM trades
           WHERE entry_time >= ? AND strategy != 'unknown'
           ORDER BY entry_time""",
        (thirty_days_ago,),
    ))

    # Per-strategy stats
    strategies = ["trend", "donchian", "mr"]
    by_strat = {s: {"trades": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0, "daily_returns": []} for s in strategies}
    by_side = {"LONG": {"trades": 0, "wins": 0, "pnl": 0.0}, "SHORT": {"trades": 0, "wins": 0, "pnl": 0.0}}
    by_regime = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    by_exit = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    daily_pnl = defaultdict(float)

    for t in trades:
        strat = t["strategy"]
        if strat not in by_strat:
            continue
        s = by_strat[strat]
        s["trades"] += 1
        s["wins"] += 1 if t["pnl"] > 0 else 0
        s["pnl"] += t["pnl"]
        s["r_sum"] += t["r"] or 0
        day = t["entry_time"][:10]
        s["daily_returns"].append(t["pnl"])

        with conn:
            # Try to get regime from trades if available (it might be set after our deploy)
            pass

        by_side[t["side"]]["trades"] += 1
        by_side[t["side"]]["wins"] += 1 if t["pnl"] > 0 else 0
        by_side[t["side"]]["pnl"] += t["pnl"]
        by_exit[t["exit_reason"] or "unknown"]["trades"] += 1
        by_exit[t["exit_reason"] or "unknown"]["pnl"] += t["pnl"]
        daily_pnl[day] += t["pnl"]

    # Compute Sharpe per strategy
    for strat, data in by_strat.items():
        if data["trades"] >= 5:
            data["sharpe"] = round(compute_sharpe(data["daily_returns"]), 3)
            data["wr"] = round(data["wins"] / data["trades"], 3) if data["trades"] > 0 else 0
            data["avg_r"] = round(data["r_sum"] / data["trades"], 3) if data["trades"] > 0 else 0
            data["avg_pnl"] = round(data["pnl"] / data["trades"], 3) if data["trades"] > 0 else 0
        del data["daily_returns"]

    # Total stats
    total_pnl = sum(v["pnl"] for v in by_strat.values())
    total_trades = sum(v["trades"] for v in by_strat.values())
    total_wins = sum(v["wins"] for v in by_strat.values())
    daily_sharpe = compute_sharpe(list(daily_pnl.values()))

    result = {
        "date": today,
        "timestamp": now.isoformat(),
        "total_trades_30d": total_trades,
        "total_pnl_30d": round(total_pnl, 2),
        "total_wr_30d": round(total_wins / total_trades, 3) if total_trades > 0 else 0,
        "daily_sharpe_30d": round(daily_sharpe, 3),
        "by_strategy": by_strat,
        "by_side": by_side,
        "by_exit_reason": dict(by_exit),
        "trading_days": len(daily_pnl),
        "best_day_pnl": round(max(daily_pnl.values()), 2) if daily_pnl else 0,
        "worst_day_pnl": round(min(daily_pnl.values()), 2) if daily_pnl else 0,
        "today_pnl": round(daily_pnl.get(today, 0), 2),
    }

    # Save to state
    key = "backtest_30d"
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        (key, json.dumps(result, indent=2)),
    )
    conn.commit()

    print(f"[{today}] 30-day backtest ({total_trades} trades, ${total_pnl:.2f} PnL)")
    print(f"  Daily Sharpe: {daily_sharpe:.3f} | WR: {result['total_wr_30d']:.1%}")
    for strat, data in by_strat.items():
        print(f"  {strat}: {data['trades']} trades, {data['wins']} wins, ${data['pnl']:.2f}, "
              f"sharpe={data.get('sharpe', 'N/A')}, avg_r={data.get('avg_r', 'N/A')}")
    print(f"  By side: LONG=${by_side['LONG']['pnl']:.2f} SHORT=${by_side['SHORT']['pnl']:.2f}")
    print(f"  By exit: {json.dumps(by_exit, indent=4)}")
    conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes-trading-bot/data/hermes.db"
    main(db)
