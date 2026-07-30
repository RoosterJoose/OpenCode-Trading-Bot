"""IC Allocator v3 — strategy-agnostic, accepts strategy list from caller."""
import sqlite3, json, statistics, time, sys
from dataclasses import dataclass
from typing import Optional, Any

MIN_TRADES = 6
MIN_WEIGHT = 0.00
MAX_WEIGHT = 0.50
STEIN_SHRINKAGE = 0.7
TRANSITION_SMOOTHING = 0.025


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
            return data.get("weights", {})
    except Exception:
        pass
    return {}


def compute_weights(db_path: str = "", strategies: Optional[list[Any]] = None) -> dict:
    """Compute budget weights from trade history using rolling Sharpe.

    Accepts a list of strategy objects (with .name() method) to determine
    which strategies to allocate between. If no strategies provided, falls
    back to equal weights.
    """
    if not strategies or not db_path:
        return {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    prev = _load_previous_weights(db_path)
    strat_names = [s.name() for s in strategies]
    sleeves: dict[str, SleeveStats] = {}

    for strat in strat_names:
        window = 15
        rows = conn.execute(
            "SELECT r_multiple FROM trades"
            " WHERE strategy=? AND r_multiple IS NOT NULL"
            " ORDER BY entry_time DESC LIMIT ?",
            (strat, window),
        ).fetchall()
        r_vals = [r["r_multiple"] for r in rows]

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

        if sharp is not None and len(all_r) > window:
            all_sharpe = _sharpe(all_r[window:])
            if all_sharpe is not None:
                shrunk_sharpe = (1 - STEIN_SHRINKAGE) * sharp + STEIN_SHRINKAGE * all_sharpe
            else:
                shrunk_sharpe = sharp
        else:
            shrunk_sharpe = sharp or 0.0

        trade_factor = min(n / 20.0, 1.0)
        score = max(0.0, shrunk_sharpe * 0.6 * trade_factor + avg_r * 5.0 * trade_factor)

        sleeves[strat] = SleeveStats(trades=n, avg_r=avg_r, sharpe=shrunk_sharpe, score=score)

    conn.close()

    weights = {s: sl.score for s, sl in sleeves.items()}
    total = sum(weights.values())
    if total <= 0:
        return {s: 1.0 / len(strat_names) for s in strat_names}

    norm = {s: w / total for s, w in weights.items()}

    for s in norm:
        if s in prev and prev[s] > 0:
            norm[s] = prev[s] + TRANSITION_SMOOTHING * (norm[s] - prev[s])

    for s in strat_names:
        v = norm.get(s, MIN_WEIGHT)
        if v < MIN_WEIGHT:
            norm[s] = MIN_WEIGHT
        elif v > MAX_WEIGHT:
            norm[s] = MAX_WEIGHT

    return norm
