#!/usr/bin/env python3
"""
Rolling 30-day Sharpe ratio tracker and auto-pause logic (hardwired).

Computes the ONE number that matters: Sharpe ratio over the last 30 days of
ACTIVE strategy trade PnL (dead legacy sleeves are excluded — their historical
losses polluted the aggregate and caused false whole-bot pauses).

PAUSE DESIGN (composite latches — a writer can never clobber another's latch):
  - manual_pause       : set by Telegram /pause, cleared by /resume
  - watchdog_pause     : set by scripts/status_report.py (drawdown / daily loss)
  - circuit_breaker_pause : set here, based on equity/risk ONLY (daily loss,
        weekly drawdown, 30d drawdown). Sharpe is NOT a global circuit breaker
        — edge is a per-strategy concern handled by paused_strategies.
The loop pauses if ANY latch is set. bot_paused is maintained as the aggregate
so legacy readers (dashboard, invariant_sweep, status_report display) stay
correct without clobbering the individual latches.

Per-strategy pause (paused_strategies): a strategy is paused when it has >= 20
trades AND either negative 30d Sharpe OR net 30d PnL below -5% of equity. The
net-PnL arm catches low-daily-return-count sleeves (e.g. sma200_perp) whose
Sharpe computes to 0.0 and were therefore immune to the old sharpe_30d<0 rule.
paused_strategies is ALWAYS written (even []) so stale pauses clear.

State keys written:
  - sharpe_30d           : full report dict
  - circuit_breaker_pause: "true"/"false" (JSON string) — this module's latch
  - bot_paused           : aggregate of manual/watchdog/circuit latches
  - pause_reasons        : persisted reason list (was never written before —
                           the Telegram alert always showed "[]")
  - paused_strategies    : ALWAYS written (even []) — plain strategy names
"""
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Strategies confirmed dead/unreachable in the running loop. Their historical
# PnL must never feed the aggregate or per-strategy pause. NOTE: an include-
# whitelist would silently drop live intent strategies (freqtrade / file
# intents stamp arbitrary names onto trades), so a blacklist is correct.
DEAD_STRATEGIES = {
    "mr", "donchian", "trend", "HermesPerpStrategy",
    "manual", "drift_momentum",
}


def _state_truthy(value) -> bool:
    """Accept every serialization form writers have used historically:
    unquoted JSON literal (`true` -> bool True), quoted JSON string
    (`"true"` -> str "true"), and raw `"true"`/`'true'`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().strip('"').lower() in ("true", "1")
    return False


def _read_state_bool(conn, key: str) -> bool:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    if row is None:
        return False
    raw = row["value"] if hasattr(row, "keys") else row[0]
    try:
        return _state_truthy(json.loads(raw))
    except Exception:
        return _state_truthy(raw)


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

    # ── 1. Daily PnL from trades (last 30 days) — ACTIVE strategies only ────
    _dead_ph = ",".join("?" * len(DEAD_STRATEGIES))
    daily_pnl_rows = list(conn.execute(
        f"""SELECT date(entry_time) as d, ROUND(SUM(pnl_dollars),4) as pnl
           FROM trades
           WHERE entry_time >= ?
             AND strategy IS NOT NULL AND strategy != ''
             AND strategy NOT IN ({_dead_ph})
           GROUP BY date(entry_time)
           ORDER BY d""",
        (thirty_days_ago, *sorted(DEAD_STRATEGIES)),
    ))

    # Daily returns as decimal of starting equity. Baseline = equity at the
    # start of the 30d window (first snapshot) rather than a hardcoded 10000 —
    # the account was reset to $5k and a wrong baseline corrupts every % metric.
    _first_eq = conn.execute(
        """SELECT equity FROM equity_snapshots
           WHERE timestamp >= ?
           ORDER BY timestamp ASC LIMIT 1""",
        (thirty_days_ago,),
    ).fetchone()
    starting_equity = float(_first_eq["equity"]) if _first_eq else 5000.0
    if starting_equity <= 0:
        starting_equity = 5000.0
    daily_returns = []
    daily_pnl_map = {}
    for r in daily_pnl_rows:
        ret = r["pnl"] / starting_equity
        daily_returns.append(ret)
        daily_pnl_map[r["d"]] = r["pnl"]

    # ── 2. Per-strategy Sharpe ───────────────────────────────────────────────
    strat_sharpe = {}
    for row in conn.execute(
        f"""SELECT strategy, date(entry_time) as d, ROUND(SUM(pnl_dollars),4) as pnl
           FROM trades
           WHERE entry_time >= ? AND strategy IS NOT NULL AND strategy != ''
             AND strategy NOT IN ({_dead_ph})
           GROUP BY strategy, date(entry_time)""",
        (thirty_days_ago, *sorted(DEAD_STRATEGIES)),
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
            f"""SELECT COUNT(*) FROM trades
               WHERE strategy=? AND entry_time >= ?
                 AND strategy NOT IN ({_dead_ph})""",
            (strat, thirty_days_ago, *sorted(DEAD_STRATEGIES)),
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
    today_pnl = sum((r["pnl"] or 0) for r in conn.execute(
        """SELECT ROUND(SUM(pnl_dollars),4) as pnl
           FROM trades WHERE date(entry_time) = ?""",
        (today,),
    ).fetchall())

    # Last 7 days PnL for weekly drawdown check
    week_pnl = sum((r["pnl"] or 0) for r in conn.execute(
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

    # Auto-pause: GLOBAL pause is equity/risk-ONLY. Sharpe is NOT a global
    # circuit breaker — it is a per-strategy concern (paused_strategies below).
    # A single bleeding sleeve must pause that sleeve, not the whole bot.
    pause_reasons = []
    if current_equity > 0 and (today_pnl / current_equity) < -0.02:
        pause_reasons.append(f"daily_loss_>{2}%: today_pnl=${today_pnl:.2f}")
    if current_equity > 0 and (week_pnl / current_equity) < -0.05:
        pause_reasons.append(f"weekly_dd_>{5}%: week_pnl=${week_pnl:.2f}")
    if max_dd_30d < -0.20:
        pause_reasons.append(f"30d_dd_>20%: {max_dd_30d*100:.1f}%")
    # Per-strategy pause: pause a strategy with >=20 trades AND (negative 30d
    # Sharpe OR net 30d PnL below -5% of equity). The net-PnL arm is the
    # definitive trigger for low-daily-return-count sleeves (e.g. sma200_perp)
    # whose Sharpe computes to 0.0 and was immune to the old sharpe_30d<0 rule.
    paused_strategies = []
    for strat, data in strat_sharpe.items():
        if data["trade_count"] >= 20 and (
            data["sharpe_30d"] < 0
            or data["total_pnl"] < -0.05 * current_equity
        ):
            paused_strategies.append(strat)

    # Composite latches. This module ONLY ever touches its own latch
    # (circuit_breaker_pause); it never clobbers manual_pause or watchdog_pause.
    circuit_breaker = bool(pause_reasons)
    manual_pause = _read_state_bool(conn, "manual_pause")
    watchdog_pause = _read_state_bool(conn, "watchdog_pause")
    aggregate_paused = circuit_breaker or manual_pause or watchdog_pause

    status = ("PAUSED" if aggregate_paused
              else ("WARNING" if paused_strategies else "HEALTHY"))

    result = {
        "date": today,
        "timestamp": now.isoformat(),
        "current_equity": round(current_equity, 2),
        "starting_equity": round(starting_equity, 2),
        "sharpe_30d": sharpe_total,
        "sortino_30d": sortino_total,
        "max_dd_30d": round(max_dd_30d, 4),
        "today_pnl": round(today_pnl, 2),
        "week_pnl": round(week_pnl, 2),
        "trading_days_with_pnl": len(daily_returns),
        "by_strategy": strat_sharpe,
        "pause_full_bot": aggregate_paused,
        "circuit_breaker_pause": circuit_breaker,
        "manual_pause": manual_pause,
        "watchdog_pause": watchdog_pause,
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

    # This module's latch. Written as a JSON string ("true"/"false") so
    # Store.get_state (json.loads) returns the string "true" and the loop's
    # `paused == "true"` comparison matches. The OLD bug wrote the bare literal
    # `true` -> json.loads -> bool True -> `True == "true"` -> never paused.
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("circuit_breaker_pause", json.dumps("true" if circuit_breaker else "false")),
    )
    # Aggregate latch for legacy readers (dashboard, invariant_sweep, display).
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("bot_paused", json.dumps("true" if aggregate_paused else "false")),
    )
    # Persist pause reasons so the Telegram alert shows real reasons (was never
    # written before — loop always read "[]").
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("pause_reasons", json.dumps(pause_reasons)),
    )
    # ALWAYS write paused_strategies (even []) so stale pauses clear.
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
    print(f"  Status: {status} | circuit_breaker={circuit_breaker} manual={manual_pause} watchdog={watchdog_pause}")
    if pause_reasons:
        print(f"  ⛔ PAUSE: {pause_reasons}")
    if paused_strategies:
        print(f"  ⚠️  PAUSED STRATEGIES: {paused_strategies}")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes-trading-bot/data/hermes.db"
    main(db)
