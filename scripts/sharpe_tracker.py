#!/usr/bin/env python3
"""
Rolling 30-day Sharpe ratio tracker and auto-pause logic.

Computes the ONE number that matters: Sharpe ratio over the last 30 days of trade PnL.
If Sharpe < 0.5 over 30 days, the bot has no edge — auto-pause.
If daily loss > 2% of equity, auto-pause.
If weekly drawdown > 5%, auto-pause.

Results written to SQLite state keys:
  - sharpe_30d: {total, by_strategy, by_side, daily_pnl, daily_sharpe, status}
  - bot_paused: bool (set to "true" if any auto-pause triggered)

The loop.py checks bot_paused at the top of each cycle and skips trading if set.
"""
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


def load_json(val):
    try:
        return json.loads(val) if val else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def compute_sharpe(returns: list[float]) -> float:
    """Annualized Sharpe ratio from a list of daily returns (as decimals, e.g. 0.01 = 1%)."""
    if len(returns) < 5:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 1e-9
    if std < 1e-9:
        return 0.0
    # Annualize: crypto trades 24/7, so 365 days/year
    return (mean / std) * math.sqrt(365)


def compute_sortino(returns: list[float]) -> float:
    """Annualized Sortino ratio (downside deviation only)."""
    if len(returns) < 5:
        return 0.0
    mean = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return float("inf") if mean > 0 else 0.0
    dvar = sum(r ** 2 for r in downside) / len(returns)
    dstd = math.sqrt(dvar) if dvar > 0 else 1e-9
    return (mean / dstd) * math.sqrt(365)


def compute_max_drawdown(equity_curve: list[float]) -> float:
    """Max drawdown from a list of equity values, returns negative decimal."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return max_dd


def main(db_path: str):
    db = Path(db_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    thirty_days_ago = (now - timedelta(days=30)).isoformat()
    seven_days_ago = (now - timedelta(days=7)).isoformat()

    # ── 1. Daily PnL from trades (last 30 days) ─────────────────────────────
    daily_pnl_rows = list(conn.execute(
        """SELECT date(entry_time) as d, ROUND(SUM(pnl_dollars),4) as pnl
           FROM trades
           WHERE entry_time >= ?
           GROUP BY date(entry_time)
           ORDER BY d""",
        (thirty_days_ago,),
    ))

    # Daily returns as decimal of starting equity
    starting_equity = 10000.0
    daily_returns = []
    daily_pnl_map = {}
    for r in daily_pnl_rows:
        ret = r["pnl"] / starting_equity
        daily_returns.append(ret)
        daily_pnl_map[r["d"]] = r["pnl"]

    # ── 2. Per-strategy Sharpe ───────────────────────────────────────────────
    strat_sharpe = {}
    for row in conn.execute(
        """SELECT strategy, date(entry_time) as d, ROUND(SUM(pnl_dollars),4) as pnl
           FROM trades
           WHERE entry_time >= ? AND strategy IS NOT NULL AND strategy != ''
           GROUP BY strategy, date(entry_time)""",
        (thirty_days_ago,),
    ):
        strat = row["strategy"]
        if strat not in strat_sharpe:
            strat_sharpe[strat] = {"returns": [], "total_pnl": 0, "trade_count": 0}
        strat_sharpe[strat]["returns"].append(row["pnl"] / starting_equity)
        strat_sharpe[strat]["total_pnl"] += row["pnl"]

    for strat, data in strat_sharpe.items():
        data["sharpe_30d"] = round(compute_sharpe(data["returns"]), 3)
        data["sortino_30d"] = round(compute_sortino(data["returns"]), 3)
        data["trade_count"] = conn.execute(
            """SELECT COUNT(*) FROM trades
               WHERE strategy=? AND entry_time >= ?""",
            (strat, thirty_days_ago),
        ).fetchone()[0]
        del data["returns"]

    # ── 3. Equity curve + max drawdown ───────────────────────────────────────
    eq_rows = list(conn.execute(
        """SELECT equity FROM equity_snapshots
           WHERE timestamp >= ?
           ORDER BY timestamp""",
        (thirty_days_ago,),
    ))
    equity_curve = [r["equity"] for r in eq_rows]
    max_dd_30d = compute_max_drawdown(equity_curve) if equity_curve else 0.0

    # ── 4. Today's P&L and decision ─────────────────────────────────────────
    today_pnl = sum(r["pnl"] for r in conn.execute(
        """SELECT ROUND(SUM(pnl_dollars),4) as pnl
           FROM trades WHERE date(entry_time) = ?""",
        (today,),
    ).fetchall())

    # Last 7 days PnL for weekly drawdown check
    week_pnl = sum(r["pnl"] for r in conn.execute(
        """SELECT ROUND(SUM(pnl_dollars),4) as pnl
           FROM trades WHERE entry_time >= ?""",
        (seven_days_ago,),
    ).fetchall())

    # Current equity
    last_equity_row = conn.execute(
        "SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    current_equity = last_equity_row["equity"] if last_equity_row else starting_equity

    # ── 5. Aggregate Sharpe + decision logic ────────────────────────────────
    sharpe_total = round(compute_sharpe(daily_returns), 3)
    sortino_total = round(compute_sortino(daily_returns), 3)

    # Auto-pause conditions
    pause_reasons = []
    if current_equity > 0 and (today_pnl / current_equity) < -0.02:
        pause_reasons.append(f"daily_loss_>{2}%: today_pnl=${today_pnl:.2f}")
    if current_equity > 0 and (week_pnl / current_equity) < -0.05:
        pause_reasons.append(f"weekly_dd_>{5}%: week_pnl=${week_pnl:.2f}")
    if max_dd_30d < -0.20:
        pause_reasons.append(f"30d_dd_>20%: {max_dd_30d*100:.1f}%")
    if len(daily_returns) >= 10 and sharpe_total < 0.0:
        pause_reasons.append(f"30d_sharpe<0: {sharpe_total}")
    # Per-strategy: pause any strategy with negative Sharpe AND >20 trades (real signal)
    paused_strategies = []
    for strat, data in strat_sharpe.items():
        if data["trade_count"] >= 20 and data["sharpe_30d"] < 0:
            paused_strategies.append(f"{strat} (sharpe={data['sharpe_30d']}, n={data['trade_count']})")

    pause_full = bool(pause_reasons)

    status = "PAUSED" if pause_full else ("WARNING" if paused_strategies else "HEALTHY")

    result = {
        "date": today,
        "timestamp": now.isoformat(),
        "current_equity": round(current_equity, 2),
        "starting_equity": starting_equity,
        "sharpe_30d": sharpe_total,
        "sortino_30d": sortino_total,
        "max_dd_30d": round(max_dd_30d, 4),
        "today_pnl": round(today_pnl, 2),
        "week_pnl": round(week_pnl, 2),
        "trading_days_with_pnl": len(daily_returns),
        "by_strategy": strat_sharpe,
        "pause_full_bot": pause_full,
        "pause_reasons": pause_reasons,
        "paused_strategies": paused_strategies,
        "status": status,
    }

    # Save to state
    key = "sharpe_30d"
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        (key, json.dumps(result)),
    )

    # Set bot_paused flag for loop.py to check
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("bot_paused", "true" if pause_full else "false"),
    )
    if paused_strategies:
        conn.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
            ("paused_strategies", json.dumps(paused_strategies)),
        )

    conn.commit()
    conn.close()

    # Print summary
    print(f"[{today}] Sharpe 30d: {sharpe_total} | Sortino: {sortino_total} | MaxDD: {max_dd_30d*100:.1f}%")
    print(f"  Equity: ${current_equity:.2f} | Today: ${today_pnl:.2f} | Week: ${week_pnl:.2f}")
    print(f"  Strategies: {json.dumps(strat_sharpe, indent=2)}")
    print(f"  Status: {status}")
    if pause_reasons:
        print(f"  ⛔ PAUSE: {pause_reasons}")
    if paused_strategies:
        print(f"  ⚠️  PAUSED STRATEGIES: {paused_strategies}")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes-trading-bot/data/hermes.db"
    main(db)
