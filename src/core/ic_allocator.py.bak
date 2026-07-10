#!/usr/bin/env python3
"""IC Allocator v2 — Stein-shrunk rolling Sharpe + partial transition."""
import sqlite3, json, statistics, time, sys
from dataclasses import dataclass
from typing import Optional

DB_PATH = "/opt/hermes-trading-bot/data/hermes.db"

WINDOWS = {"trend": 20, "mr": 5, "xs_momentum": 15}
ALL_STRATS = list(WINDOWS.keys())
MIN_TRADES = 6
MIN_WEIGHT = 0.15
MAX_WEIGHT = 0.50
DONCHIAN_STATIC_WEIGHT = 0.15  # static until 30 trades
STEIN_SHRINKAGE = 0.7  # weight toward long-term average (0=no shrinkage, 1=full)
TRANSITION_SMOOTHING = 0.025  # fraction toward target per refresh


@dataclass
class SleeveStats:
    trades: int = 0
    avg_r: float = 0.0
    sharpe: float = 0.0
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


def _load_previous_weights(db_path: str) -> dict:
    try:
        conn = sqlite3.connect(db_path)
        raw = conn.execute(
            "SELECT value FROM state WHERE key='strategy_budget'"
        ).fetchone()
        conn.close()
        if raw:
            data = json.loads(raw[0])
            last = data.get("weights", {})
            last.pop("donchian", None)
            return last
    except Exception:
        pass
    return {}


def compute_weights(db_path: str = DB_PATH) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Load previous weights for smoothing
    prev = _load_previous_weights(db_path)
    prev.pop("donchian", None)

    sleeves: dict[str, SleeveStats] = {}

    for strat in ALL_STRATS:
        window = WINDOWS.get(strat, 10)
        rows = conn.execute(
            "SELECT r_multiple FROM trades"
            " WHERE strategy=? AND r_multiple IS NOT NULL"
            " ORDER BY entry_time DESC LIMIT ?",
            (strat, window),
        ).fetchall()
        r_vals = [r["r_multiple"] for r in rows]

        # Also load all-time trades for Stein shrinkage
        all_rows = conn.execute(
            "SELECT r_multiple FROM trades"
            " WHERE strategy=? AND r_multiple IS NOT NULL"
            " ORDER BY entry_time DESC",
            (strat,),
        ).fetchall()
        all_r = [r["r_multiple"] for r in all_rows]

        if not r_vals:
            sleeves[strat] = SleeveStats()
            continue

        n = len(r_vals)
        sharp = _sharpe(r_vals)
        avg_r = sum(r_vals) / n

        # Stein shrinkage: blend short-term Sharpe with long-term
        if sharp is not None and len(all_r) > window:
            all_sharpe = _sharpe(all_r[window:])  # long-term only
            if all_sharpe is not None:
                # Shrink toward long-term mean
                shrunk_sharpe = (1 - STEIN_SHRINKAGE) * sharp + STEIN_SHRINKAGE * all_sharpe
            else:
                shrunk_sharpe = sharp
        else:
            shrunk_sharpe = sharp or 0.0

        # Score: no raw PnL, just shrunk Sharpe + avg_r for directionality
        trade_factor = min(total / 20.0, 1.0) if (total := n) else 1.0
        score = max(0.0, shrunk_sharpe * 0.6 * trade_factor + avg_r * 5.0 * trade_factor)

        sleeves[strat] = SleeveStats(trades=n, avg_r=avg_r, sharpe=shrunk_sharpe, score=score)

    conn.close()

    # Build weights from IC strats (excl donchian)
    weights = {s: sl.score for s, sl in sleeves.items()}
    total = sum(weights.values())
    if total <= 0:
        return {s: 1.0 / len(ALL_STRATS) for s in ALL_STRATS}

    # Normalize, excluding donchian from the pool
    norm: dict[str, float] = {s: w / total for s, w in weights.items()}

    # Apply smoothing toward previous weights
    for s in norm:
        if s in prev and prev[s] > 0:
            norm[s] = prev[s] + TRANSITION_SMOOTHING * (norm[s] - prev[s])

    # Caps and floor on IC strats
    for s in ALL_STRATS:
        v = norm.get(s, MIN_WEIGHT)
        if v < MIN_WEIGHT:
            norm[s] = MIN_WEIGHT
        elif v > MAX_WEIGHT:
            norm[s] = MAX_WEIGHT

    # Add donchian at static weight, scale everything else down
    remaining = 1.0 - DONCHIAN_STATIC_WEIGHT
    t2 = sum(norm.values())
    if t2 > 0:
        for s in list(norm.keys()):
            norm[s] = round(norm[s] / t2 * remaining, 4)
    norm["donchian"] = DONCHIAN_STATIC_WEIGHT

    # Ensure all strats present
    all_strats_all = list(WINDOWS.keys())
    for s in all_strats_all:
        if s not in norm:
            norm[s] = MIN_WEIGHT

    return norm


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    w = compute_weights(db_path)
    budget = {"weights": w, "source": "ic_rollingsharpe_v2", "timestamp": time.time()}
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        ("strategy_budget", json.dumps(budget)),
    )
    conn.commit()
    conn.close()
    print("IC Allocator Budget v2:")
    for k, v in sorted(w.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v*100:.1f}%")


if __name__ == "__main__":
    main()