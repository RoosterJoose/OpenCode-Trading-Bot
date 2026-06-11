#!/usr/bin/env python3
"""
Strategy budget allocator.
Reads historical backtest results and allocates risk capital per strategy.
Seeds live allocator with backtest Sharpe so MR isn't stuck at 2.6% forever.

Usage:
    python3 scripts/strategy_budget.py [--run-backtest]

Writes to state key "strategy_budget":
    {"weights": {"mr": 0.7, "trend": 0.1, "donchian": 0.1, "drift_momentum": 0.1}, "source": "backtest_1000_3pct"}
"""
import json, sys, subprocess, math
from pathlib import Path

DB_PATH = Path("/opt/hermes-trading-bot/data/hermes.db")
BACKTEST_RESULT = Path("/tmp/backtest_result.json")

# Default: equal split when no data
DEFAULT_WEIGHTS = {"mr": 0.20, "trend": 0.20, "donchian": 0.20, "drift_momentum": 0.20, "xs_momentum": 0.20}

STRATEGY_KEY_MAP = {
}

def run_backtest():
    """Run historical backtest with 1000-candle limit (proven fast)."""
    print("  Running backtest (1000 candles, 3% stop)...")
    result = subprocess.run(
        [sys.executable, "scripts/historical_backtest.py", "--limit", "1000"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"  Backtest failed (rc={result.returncode}): {result.stderr[:200]}")
        return None
    print(f"  Backtest output:\n{result.stdout[-500:]}")
    if BACKTEST_RESULT.exists():
        with open(BACKTEST_RESULT) as f:
            return json.load(f)
    return None

def compute_budget(backtest_data: dict) -> dict:
    """Compute strategy weights from backtest results.
    
    Weight = max(0, avg_r) * sqrt(min(trades, 30))
    Strategies with avg_r <= 0 get exploration budget.
    """
    if not backtest_data:
        return DEFAULT_WEIGHTS
    
    scores = {}
    for sname, sdata in backtest_data.items():
        avg_r = sdata.get("avg_r", 0)
        trades = sdata.get("trades", 0)
        if avg_r > 0 and trades >= 2:
            score = avg_r * math.sqrt(min(trades, 30))
        else:
            score = 0.0
        scores[sname] = max(0.0, score)
    
    total = sum(scores.values()) or 1.0
    weights = {k: v / total for k, v in scores.items()}
    
    # Min 5% exploration for each strategy
    all_strats = set(list(weights.keys()) + list(DEFAULT_WEIGHTS.keys()))
    for k in all_strats:
        if k not in weights or weights.get(k, 0) < 0.05:
            weights[k] = 0.05
    
    total2 = sum(weights.values())
    weights = {k: round(v / total2, 4) for k, v in weights.items()}
    
    return weights


def main():
    db_path = DB_PATH
    
    # Check if we should run backtest fresh
    use_cached = "--run-backtest" not in sys.argv
    backtest_data = None
    
    if use_cached and BACKTEST_RESULT.exists():
        with open(BACKTEST_RESULT) as f:
            backtest_data = json.load(f)
        print(f"  Using cached backtest result from {BACKTEST_RESULT}")
    else:
        backtest_data = run_backtest()
    
    weights = compute_budget(backtest_data) if backtest_data else DEFAULT_WEIGHTS
    
    budget = {
        "weights": weights,
        "source": "backtest" if backtest_data else "default",
        "timestamp": __import__("time").time(),
    }
    
    # Write to DB
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("strategy_budget", json.dumps(budget)),
    )
    conn.commit()
    conn.close()
    
    print(f"\nStrategy Budget:")
    print(f"  Source: {budget['source']}")
    for k, v in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v*100:.1f}%")
    if backtest_data:
        for sname, sdata in backtest_data.items():
            if sdata.get("trades", 0) > 0:
                print(f"    ({sname}: {sdata['trades']}t, avg_r {sdata.get('avg_r',0):.3f}, WR {sdata.get('wr',0):.0%})")


if __name__ == "__main__":
    main()
