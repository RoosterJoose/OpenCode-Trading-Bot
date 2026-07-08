#!/usr/bin/env python3
"""IC Allocator — rolling Sharpe-based strategy budget weighting."""
import sqlite3, json, statistics, time, sys
from dataclasses import dataclass
from typing import Optional

DB_PATH = "/opt/hermes-trading-bot/data/hermes.db"

WINDOWS = {"trend": 20, "mr": 5, "donchian": 20, "xs_momentum": 15}
ALL_STRATS = list(WINDOWS.keys())
MIN_TRADES = 6
MIN_WEIGHT = 0.05
MAX_WEIGHT = 0.50


@dataclass
class SleeveStats:
    trades: int = 0
    avg_r: float = 0.0
    sharpe: float = 0.0
    pnl: float = 0.0
    score: float = 0.0


def _sharpe(r_values: list[float]) -> Optional[float]:
    if len(r_values) < 2:
        return None
    mean_r = sum(r_values) / len(r_values)
    if abs(mean_r) < 1e-9:
        return None
    try:
        std = statistics.stdev(r_values)
    except statistics.StatisticsError:
        return None
    if std < 1e-9:
        return None
    return mean_r / std


def compute_weights(db_path: str = DB_PATH) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sleeves: dict[str, SleeveStats] = {}

    for strat in ALL_STRATS:
        window = WINDOWS.get(strat, 10)
        rows = conn.execute(
            "SELECT r_multiple, pnl_dollars FROM trades"
            " WHERE strategy=? AND r_multiple IS NOT NULL"
            " ORDER BY entry_time DESC LIMIT ?",
            (strat, window),
        ).fetchall()
        r_vals = [r["r_multiple"] for r in rows]
        pnl = [r["pnl_dollars"] for r in rows]
        if not r_vals:
            sleeves[strat] = SleeveStats()
            continue

        avg_r = sum(r_vals) / len(r_vals)
        sharp = _sharpe(r_vals)
        total_pnl = sum(pnl)
        total_trades = len(r_vals)
        trade_factor = min(total_trades / 20.0, 1.0)

        score = max(
            0.0,
            (sharp or 0.0) * 0.4 * trade_factor
            + avg_r * 8.0 * trade_factor
            + total_pnl * 0.0001 * trade_factor,
        )
        sleeves[strat] = SleeveStats(
            trades=total_trades, avg_r=avg_r,
            sharpe=sharp or 0.0, pnl=total_pnl, score=score,
        )
    conn.close()

    weights = {s: sl.score for s, sl in sleeves.items()}
    total = sum(weights.values())
    if total <= 0:
        return {s: 1.0 / len(ALL_STRATS) for s in ALL_STRATS}

    norm: dict[str, float] = {s: w / total for s, w in weights.items()}
    for s in ALL_STRATS:
        v = norm.get(s, MIN_WEIGHT)
        if v < MIN_WEIGHT:
            norm[s] = MIN_WEIGHT
        elif v > MAX_WEIGHT:
            norm[s] = MAX_WEIGHT

    t2 = sum(norm.values())
    if t2 > 0:
        norm = {s: round(v / t2, 4) for s, v in norm.items()}
    for s in ALL_STRATS:
        if s not in norm:
            norm[s] = MIN_WEIGHT
    return norm


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    w = compute_weights(db_path)
    budget = {"weights": w, "source": "ic_rollingsharpe", "timestamp": time.time()}
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("strategy_budget", json.dumps(budget)),
    )
    conn.commit()
    conn.close()
    print("IC Allocator Budget:")
    for k, v in sorted(w.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v*100:.1f}%")


if __name__ == "__main__":
    main()
