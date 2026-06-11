#!/usr/bin/env python3
"""
Daily 30-day backtest wrapper.
Runs the historical backtest at 1000-candle limit and updates strategy budget.
"""
import subprocess, sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKTEST_SCRIPT = str(PROJECT_DIR / "scripts/historical_backtest.py")
BUDGET_SCRIPT = str(PROJECT_DIR / "scripts/strategy_budget.py")

def main():
    print("=== 30-Day Backtest ===")
    result = subprocess.run(
        [sys.executable, BACKTEST_SCRIPT, "--sweep", "--limit", "1000"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"  FAILED (rc={result.returncode}): {result.stderr[:200]}")
        print(f"  stdout: {result.stdout[-300:]}")
        return
    print(f"  OK ({len(result.stdout)} chars)")

    for line in result.stdout.split("\n"):
        if line.strip().startswith("mr:") or line.strip().startswith("=== Best"):
            print(f"  {line.strip()}")

    print("\n=== Strategy Budget ===")
    result2 = subprocess.run(
        [sys.executable, BUDGET_SCRIPT],
        capture_output=True, text=True, timeout=30,
    )
    if result2.returncode != 0:
        print(f"  FAILED: {result2.stderr[:200]}")
    else:
        for line in result2.stdout.split("\n"):
            print(f"  {line}")

if __name__ == "__main__":
    main()
