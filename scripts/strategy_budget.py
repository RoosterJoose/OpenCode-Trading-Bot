#!/usr/bin/env python3
"""
Strategy budget allocator — dual-window blend.
Runs backtests at 1000c (recent regime) and 4000c (long-term),
blends them 60/40, caps any strat at 50% max, floors at 5% min.

Usage:
    python3 scripts/strategy_budget.py [--run-backtest]
"""
import json, sys, subprocess, math
from pathlib import Path

DB_PATH = Path("/opt/hermes-trading-bot/data/hermes.db")
PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKTEST_SCRIPT = str(PROJECT_DIR / "scripts/historical_backtest.py")

DEFAULT_WEIGHTS = {"mr": 0.20, "trend": 0.20, "donchian": 0.20, "drift_momentum": 0.20, "xs_momentum": 0.20}

ALL_STRATS = ["trend", "mr", "donchian", "xs_momentum", "drift_momentum"]

def run_backtest(limit: int, stop_pct: float = 3.0) -> dict:
    """Run single-stop backtest and return results dict."""
    print(f"  Running backtest ({limit}c, {stop_pct:.0f}% stop)...")
    result = subprocess.run(
        [sys.executable, BACKTEST_SCRIPT, "--limit", str(limit)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"  FAILED (rc={result.returncode}): {result.stderr[:200]}")
        return {}
    
    # Parse stdout for key numbers
    lines = result.stdout.split("\n")
    print(f"  OK ({len(result.stdout)} chars)")
    for line in lines:
        line = line.strip()
        if any(line.startswith(s + ":") for s in ALL_STRATS):
            print(f"    {line}")
    
    # data is saved to /tmp/backtest_result.json
    result_path = Path("/tmp/backtest_result.json")
    if result_path.exists():
        with open(result_path) as f:
            return json.load(f)
    return {}

def compute_budget(recent: dict, longterm: dict) -> dict:
    """Blend recent (1000c) and long-term (4000c), cap at 50% per strat."""
    scores = {}
    for strat in ALL_STRATS:
        recent_avg_r = recent.get(strat, {}).get("avg_r", 0) if recent else 0
        recent_trades = recent.get(strat, {}).get("trades", 0) if recent else 0
        long_avg_r = longterm.get(strat, {}).get("avg_r", 0) if longterm else 0
        long_trades = longterm.get(strat, {}).get("trades", 0) if longterm else 0
        
        # Blend: 60% recent, 40% long-term
        recent_score = max(0, recent_avg_r) * math.sqrt(min(recent_trades, 30))
        long_score = max(0, long_avg_r) * math.sqrt(min(long_trades, 50))
        blended = 0.6 * recent_score + 0.4 * long_score
        scores[strat] = blended
    
    total = sum(scores.values()) or 1.0
    weights = {k: v / total for k, v in scores.items()}
    
    # Cap at 50%, floor at 5%
    for s in ALL_STRATS:
        if s not in weights or weights.get(s, 0) < 0.05:
            weights[s] = 0.05
        elif weights[s] > 0.50:
            weights[s] = 0.50
    
    # Renormalize
    total2 = sum(weights.values())
    weights = {k: round(v / total2, 4) for k, v in weights.items()}
    
    return weights


def main():
    run_fresh = "--run-backtest" in sys.argv
    
    recent = run_backtest(1000)
    longterm = run_backtest(4000)
    
    weights = compute_budget(recent, longterm)
    
    budget = {
        "weights": weights,
        "source": "blend_1000_4000",
        "timestamp": __import__("time").time(),
    }
    
    # Write to DB
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("strategy_budget", json.dumps(budget)),
    )
    conn.commit()
    conn.close()
    
    print(f"\nStrategy Budget (blend 60/40 recent/long-term):")
    for k, v in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v*100:.1f}%")
    if recent:
        for sname, sdata in recent.items():
            if sdata.get("trades", 0) > 0:
                print(f"    recent-{sname}: {sdata['trades']}t, avg_r {sdata.get('avg_r',0):.3f}")
    if longterm:
        for sname, sdata in longterm.items():
            if sdata.get("trades", 0) > 0:
                print(f"    long-{sname}: {sdata['trades']}t, avg_r {sdata.get('avg_r',0):.3f}")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
