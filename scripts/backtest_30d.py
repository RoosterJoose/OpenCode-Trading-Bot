#!/usr/bin/env python3
"""30-day backtest from trade DB only. No bot imports needed."""
import json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes-trading-bot/data/hermes.db"
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
trades = list(conn.execute(
    "SELECT strategy, side, pnl_dollars, r_multiple, exit_reason, entry_time "
    "FROM trades WHERE entry_time >= ? AND strategy NOT IN ('unknown', '')",
    (cutoff,),
))

strategies = ["trend", "donchian", "mr"]
by_strat = {s: {"trades": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0} for s in strategies}
by_side = {"LONG": {"trades": 0, "wins": 0, "pnl": 0.0},
           "SHORT": {"trades": 0, "wins": 0, "pnl": 0.0}}
by_exit: dict[str, dict] = {}
daily_pnl: dict[str, float] = {}

for t in trades:
    s = t["strategy"]
    if s not in by_strat:
        continue
    by_strat[s]["trades"] += 1
    by_strat[s]["wins"] += 1 if t["pnl_dollars"] > 0 else 0
    by_strat[s]["pnl"] += t["pnl_dollars"]
    by_strat[s]["r_sum"] += t["r_multiple"] or 0

    side = t["side"]
    if side in by_side:
        by_side[side]["trades"] += 1
        by_side[side]["wins"] += 1 if t["pnl_dollars"] > 0 else 0
        by_side[side]["pnl"] += t["pnl_dollars"]

    er = t["exit_reason"] or "unknown"
    if er not in by_exit:
        by_exit[er] = {"trades": 0, "pnl": 0.0, "avg": 0.0}
    by_exit[er]["trades"] += 1
    by_exit[er]["pnl"] += t["pnl_dollars"]

    day = t["entry_time"][:10]
    daily_pnl[day] = daily_pnl.get(day, 0) + t["pnl_dollars"]

for er in by_exit:
    by_exit[er]["avg"] = round(by_exit[er]["pnl"] / by_exit[er]["trades"], 3)

total_pnl = sum(v["pnl"] for v in by_strat.values())
total_trades = sum(v["trades"] for v in by_strat.values())
total_wins = sum(v["wins"] for v in by_strat.values())

# Sharpe from daily PnL
def sharpe(returns):
    n = len(returns)
    if n < 5:
        return 0.0
    m = sum(returns) / n
    v = sum((r - m) ** 2 for r in returns) / n
    s = math.sqrt(v) if v > 0 else 1e-9
    return (m / s) * math.sqrt(365)

ds = sharpe(list(daily_pnl.values()))
wr = total_wins / total_trades if total_trades else 0

result = {
    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "total_trades_30d": total_trades,
    "total_pnl_30d": round(total_pnl, 2),
    "total_wr_30d": round(wr, 3),
    "daily_sharpe_30d": round(ds, 3),
    "by_strategy": by_strat,
    "by_side": by_side,
    "by_exit_reason": by_exit,
    "trading_days": len(daily_pnl),
}
conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
             ("backtest_30d", json.dumps(result)))
conn.commit()

print(f"30d backtest: {total_trades} trades, ${total_pnl:.2f} PnL, Sharpe {ds:.3f}, WR {wr:.1%}")
for s, d in by_strat.items():
    avg = d["pnl"] / d["trades"] if d["trades"] else 0
    avg_r = d["r_sum"] / d["trades"] if d["trades"] else 0
    print(f"  {s}: {d['trades']}t, ${d['pnl']:.2f}, avg=${avg:.2f}, avg_r={avg_r:.3f}")
conn.close()
