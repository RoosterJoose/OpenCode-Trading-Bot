#!/usr/bin/env python3
"""
Closed-loop learning: turns lessons table insights into runtime strategy parameter adjustments.

Every day the daily_reflection.py script inserts lessons into the `lessons` table
(e.g. 'no_bull_cross' for the same asset 3 days in a row). This script:
  1. Aggregates lessons by asset + pattern + reason
  2. Identifies patterns that have repeated 3+ days for the same asset
  3. Writes auto-adjustments to a `dynamic_thresholds` state key
  4. Strategies read this state key in should_enter() to widen their entry conditions

This is the "feedback loop" — the bot learns from its misses without human intervention.

Writes to state:
  - dynamic_thresholds: {asset: {param_name: adjusted_value, reason: str, applied_at: iso}}
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


def main(db_path: str):
    db = Path(db_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    # ── 1. Aggregate lessons by (asset, reason) over last 7 days ─────────────
    rows = list(conn.execute(
        """SELECT asset, pattern_category, pattern_detail, COUNT(*) as cnt,
                  GROUP_CONCAT(DISTINCT date) as dates
           FROM lessons
           WHERE date >= ?
           GROUP BY asset, pattern_category, pattern_detail
           HAVING cnt >= 3""",
        (seven_days_ago,),
    ))

    # ── 2. Map lesson patterns to strategy parameter adjustments ────────────
    # This is the "institutional knowledge" of which parameter to relax when.
    adjustment_map = {
        ("strategy_limitation", "no_bull_cross"): {
            "param": "near_ema_pct",
            "current_default": 0.04,
            "widen_step": 0.02,   # Add 2% per repeat
            "max_widen": 0.10,    # Cap at 10% to prevent garbage
        },
        ("strategy_limitation", "no_bear_cross"): {
            "param": "near_ema_pct",
            "current_default": 0.04,
            "widen_step": 0.02,
            "max_widen": 0.10,
        },
        ("strategy_limitation", "multiple_gates"): {
            "param": "volume_min_usd",
            "current_default": 2_000_000,
            "widen_step": -1_000_000,  # Lower the volume gate
            "max_widen": 500_000,       # Cap at $500k minimum
        },
        ("strategy_limitation", "overbought_rsi_72"): {
            "param": "rsi_overbought",
            "current_default": 70,
            "widen_step": 2,  # Raise the overbought threshold
            "max_widen": 80,
        },
        ("strategy_limitation", "overbought_rsi_75"): {
            "param": "rsi_overbought",
            "current_default": 70,
            "widen_step": 2,
            "max_widen": 80,
        },
        ("strategy_limitation", "oversold_rsi_28"): {
            "param": "rsi_oversold",
            "current_default": 30,
            "widen_step": -2,
            "max_widen": 20,
        },
    }

    # ── 3. Build per-asset adjustments ──────────────────────────────────────
    adjustments = {}
    for row in rows:
        key = (row["pattern_category"], row["pattern_detail"])
        if key not in adjustment_map:
            continue
        rule = adjustment_map[key]
        cnt = row["cnt"]
        asset = row["asset"]

        # Compute adjusted value: 1 widen_step per repeat beyond 2 (so 3rd day triggers 1 widen)
        widen_steps = min(cnt - 2, 6)  # max 6 widen steps = 6 days
        new_value = rule["current_default"] + (rule["widen_step"] * widen_steps)
        # Clamp
        if rule["widen_step"] > 0:
            new_value = min(new_value, rule["max_widen"])
        else:
            new_value = max(new_value, rule["max_widen"])

        if asset not in adjustments:
            adjustments[asset] = {}
        adjustments[asset][rule["param"]] = {
            "value": new_value,
            "reason": f"{row['pattern_category']}/{row['pattern_detail']} x{cnt} (last 7d)",
            "applied_at": now.isoformat(),
            "recurrence_count": cnt,
        }

    # ── 4. Persist to state ─────────────────────────────────────────────────
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("dynamic_thresholds", json.dumps(adjustments, indent=2)),
    )

    # Also log a human-readable summary
    summary_lines = [f"[{today}] Closed-loop learning applied {len(adjustments)} asset adjustments:"]
    for asset, params in adjustments.items():
        for param_name, info in params.items():
            summary_lines.append(
                f"  {asset}.{param_name} = {info['value']:.4f} "
                f"(reason: {info['reason']})"
            )
    summary = "\n".join(summary_lines)
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("closed_loop_log", summary),
    )
    conn.commit()
    conn.close()

    print(summary)
    if not adjustments:
        print("  (no patterns met the 3+ day threshold)")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes-trading-bot/data/hermes.db"
    main(db)
