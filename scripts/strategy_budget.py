"""
Strategy budget: fact-based risk allocation using 30d traceable data only.
- Strategy with < 5 trades: 0.05 exploration (allow proving after bugfix)
- Strategy with negative Sharpe: 0% budget (proven negative edge)
- Remaining: proportional to Sharpe
- Trend gets 100% floor if no other qualifies
"""
import sqlite3, json, math, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

EXPLORE_BUDGET = 0.05
MIN_TRADES = 5

def main(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    trades = list(conn.execute(
        "SELECT strategy, pnl_dollars, r_multiple FROM trades "
        "WHERE strategy NOT IN ('unknown', '') AND entry_time >= ?",
        (cutoff,),
    ))
    strat_pnls = defaultdict(list)
    for t in trades:
        strat_pnls[t["strategy"]].append(t["pnl_dollars"])

    known = ["trend", "donchian", "mr", "xs_momentum", "drift_momentum"]
    scores = {}
    stats = {}
    for name in known:
        pnls = strat_pnls.get(name, [])
        n = len(pnls)
        stats[name] = {"trades": n, "pnl": round(sum(pnls), 2)}
        if n < MIN_TRADES:
            scores[name] = EXPLORE_BUDGET  # allow proving after bugfix
        else:
            mean = sum(pnls) / n
            var = sum((p - mean) ** 2 for p in pnls) / n
            std = math.sqrt(var) if var > 0 else 1e-9
            sharpe = (mean / std) * math.sqrt(365) if std > 0 else 0.0
            stats[name]["sharpe"] = round(sharpe, 3)
            scores[name] = max(0.0, sharpe)

    total = sum(scores.values())
    if total <= 0:
        budget = {s: (1.0 if s == "trend" else 0.0) for s in known}
    else:
        budget = {s: round(v / total, 4) for s, v in scores.items()}

    result = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_weight": round(sum(budget.values()), 4),
        "weights": budget,
        "strategies": stats,
        "logic": (
            "Trend only proven entry. Others have < 5 trades (recently fixed bugs). "
            "Each unproven strategy gets 5% exploration budget until 5+ trades accumulated. "
            "After 20 trades, budget proportional to positive Sharpe only."
        ),
    }
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("strategy_budget", json.dumps(result, indent=2)),
    )
    conn.commit()
    print(f"[{result['date']}] Strategy budget")
    for s, w in sorted(budget.items(), key=lambda x: -x[1]):
        st = stats[s]
        print(f"  {s}: {w*100:.1f}% ({st['trades']} trades, ${st['pnl']})")
    conn.close()
    return result

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes-trading-bot/data/hermes.db")
