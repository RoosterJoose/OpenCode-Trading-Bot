#!/usr/bin/env python3
"""
Daily backtest + budget runner.
Delegates to strategy_budget.py which runs dual-window (1000c + 4000c) backtests.
"""
import subprocess, sys
from pathlib import Path

BUDGET_SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts/strategy_budget.py")

def main():
    print("=== Daily Budget Allocation ===")
    result = subprocess.run(
        [sys.executable, BUDGET_SCRIPT, "--run-backtest"],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        print(f"  FAILED (rc={result.returncode}):")
        print(f"    stderr: {result.stderr[:500]}")
        print(f"    stdout: {result.stdout[-500:]}")
    else:
        for line in result.stdout.split("\n"):
            print(f"  {line}")

if __name__ == "__main__":
    main()
