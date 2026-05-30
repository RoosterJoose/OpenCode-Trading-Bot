"""
Signal tracking with decay-weighted accuracy and automated parameter adaptation.

Two-tier learning:
  1. Signal-level: Each signal source tracked for accuracy. Retired at <48%.
     Weight = (edge × 2)² — quadratic amplification of strong signals.
  2. Parameter-level: After enough trades, evaluates parameter performance
     and suggests adjustments to the strategy config.

Weekly reflection generates a report with specific parameter change recommendations.
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.core.types import ParameterSuggestion, Side, TradeRecord


class SignalTracker:
    DECAY_WINDOW = 100
    RETIRE_ACCURACY = 0.48
    REACTIVATE_ACCURACY = 0.52
    MIN_PREDICTIONS = 10

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self._outcomes: dict[str, list[bool]] = {}
        self._retired: set[str] = set()
        self._param_history: dict[str, list[float]] = {}
        self._load()

    def record(self, source: str, was_correct: bool):
        if source not in self._outcomes:
            self._outcomes[source] = []
        self._outcomes[source].append(was_correct)
        window = self._outcomes[source][-self.DECAY_WINDOW:]
        self._outcomes[source] = window
        self._prune(source)
        self._save()

    def accuracy(self, source: str) -> Optional[float]:
        window = self._window(source)
        if len(window) < self.MIN_PREDICTIONS:
            return None
        return sum(window) / len(window)

    def weight(self, source: str) -> float:
        if source in self._retired:
            return 0.0
        acc = self.accuracy(source)
        if acc is None:
            return 0.5
        edge = acc - 0.5
        return max(0.0, (edge * 2) ** 2)

    def retired(self, source: str) -> bool:
        return source in self._retired

    def record_param_value(self, param: str, value: float):
        if param not in self._param_history:
            self._param_history[param] = []
        self._param_history[param].append(value)
        self._param_history[param] = self._param_history[param][-500:]
        self._save()

    def _window(self, source: str) -> list[bool]:
        return [o for o in self._outcomes.get(source, [])][-self.DECAY_WINDOW:]

    def _prune(self, source: str):
        acc = self.accuracy(source)
        if acc is not None:
            if acc < self.RETIRE_ACCURACY:
                self._retired.add(source)
            elif acc >= self.REACTIVATE_ACCURACY:
                self._retired.discard(source)

    def status(self) -> dict:
        result = {}
        for src in list(self._outcomes.keys()):
            acc = self.accuracy(src)
            result[src] = {
                "accuracy": round(acc, 4) if acc is not None else None,
                "weight": round(self.weight(src), 4),
                "sample": len(self._window(src)),
                "retired": src in self._retired,
            }
        return result

    def _save(self):
        data = {
            "outcomes": {k: v for k, v in self._outcomes.items()},
            "retired": list(self._retired),
            "param_history": {k: v for k, v in self._param_history.items()},
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(self.state_path)

    def _load(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            self._outcomes = {k: list(v) for k, v in data.get("outcomes", {}).items()}
            self._retired = set(data.get("retired", []))
            self._param_history = {k: list(v) for k, v in data.get("param_history", {}).items()}
        except (json.JSONDecodeError, KeyError):
            pass


class WeeklyReflector:
    """Analyzes trade data weekly and generates parameter change suggestions."""

    MIN_TRADES_FOR_ANALYSIS = 20

    def __init__(self, tracker: SignalTracker):
        self.tracker = tracker

    def reflect(self, trades: list[TradeRecord], current_params: dict) -> dict:
        suggestions: list[ParameterSuggestion] = []
        metrics = self._compute_metrics(trades)
        bucket_analysis = self._bucket_params(trades, current_params)
        decay_analysis = self._detect_decay(trades)

        suggestions.extend(bucket_analysis)
        suggestions.extend(decay_analysis)

        if len(trades) >= self.MIN_TRADES_FOR_ANALYSIS:
            sharpe_sugg = self._analyze_sharpe(trades, current_params)
            if sharpe_sugg:
                suggestions.append(sharpe_sugg)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_trades": len(trades),
            "metrics": metrics,
            "suggestions": [s.__dict__ for s in suggestions],
            "signal_status": self.tracker.status(),
            "needs_human_review": any(
                s.confidence < 0.6 for s in suggestions
            ),
        }

    def _compute_metrics(self, trades: list[TradeRecord]) -> dict:
        if not trades:
            return {}
        r_values = [t.r_multiple for t in trades if t.r_multiple != 0]
        pnls = [t.pnl_pct for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        return {
            "total_trades": len(trades),
            "win_rate": round(len(wins) / len(pnls), 3) if pnls else 0,
            "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0,
            "profit_factor": round(abs(sum(wins) / sum(losses)), 3) if losses and sum(losses) != 0 else float("inf"),
            "avg_r": round(sum(r_values) / len(r_values), 3) if r_values else 0,
            "sharpe": round(self._sharpe(pnls), 3),
            "max_consecutive_losses": self._max_consecutive(pnls),
        }

    def _bucket_params(self, trades: list[TradeRecord], params: dict) -> list[ParameterSuggestion]:
        suggestions = []

        rsi_trades = [t for t in trades if t.strategy == "mr"]
        if len(rsi_trades) >= 10:
            rsi_val = params.get("rsi_oversold", 28.0)
            r_pnls = [t.r_multiple for t in rsi_trades]
            good_trades = [t for t in rsi_trades if t.r_multiple > 0.5]
            bad_trades = [t for t in rsi_trades if t.r_multiple < -0.5]

            if len(good_trades) < len(bad_trades) and len(bad_trades) >= 5:
                new_rsi = rsi_val - 2.0
                suggestions.append(ParameterSuggestion(
                    parameter="strategies.mean_reversion.rsi_oversold",
                    current_value=rsi_val,
                    suggested_value=max(20.0, new_rsi),
                    reason=f"MR trades underperforming: {len(good_trades)} good vs {len(bad_trades)} bad. Tightening RSI threshold.",
                    confidence=0.7,
                ))

        return suggestions

    def _detect_decay(self, trades: list[TradeRecord]) -> list[ParameterSuggestion]:
        suggestions = []
        if len(trades) < 30:
            return suggestions

        recent = trades[-15:]
        older = trades[:15]

        recent_r = [t.r_multiple for t in recent]
        older_r = [t.r_multiple for t in older]

        recent_avg = sum(recent_r) / len(recent_r) if recent_r else 0
        older_avg = sum(older_r) / len(older_r) if older_r else 0

        if recent_avg < older_avg * 0.5 and len(recent) >= 10:
            suggestions.append(ParameterSuggestion(
                parameter="risk.risk_per_trade_pct",
                current_value=1.0,
                suggested_value=0.75,
                reason=f"Recent avg R ({recent_avg:.2f}) vs older avg R ({older_avg:.2f}) — decay detected. Reducing risk per trade.",
                confidence=0.65,
            ))

        return suggestions

    def _analyze_sharpe(self, trades: list[TradeRecord], params: dict) -> Optional[ParameterSuggestion]:
        pnls = [t.pnl_pct for t in trades]
        sharpe = self._sharpe(pnls)
        if sharpe < 0.5:
            return ParameterSuggestion(
                parameter="strategies.mean_reversion.cooldown_bars",
                current_value=params.get("cooldown_bars", 12),
                suggested_value=params.get("cooldown_bars", 12) + 6,
                reason=f"Sharpe {sharpe:.2f} < 0.5 — increasing MR cooldown to reduce noise entries.",
                confidence=0.6,
            )
        return None

    def _sharpe(self, pnls: list[float]) -> float:
        if len(pnls) < 5:
            return 0.0
        mean = sum(pnls) / len(pnls)
        std = math.sqrt(sum((p - mean) ** 2 for p in pnls) / len(pnls))
        return (mean / std) * math.sqrt(365) if std > 0 else 0.0

    def _max_consecutive(self, pnls: list[float]) -> int:
        streak = 0
        max_streak = 0
        for p in pnls:
            if p < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        return max_streak
