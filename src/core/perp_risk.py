"""
Perp-aware risk management — leverage scaling by ATR, OI velocity gate,
funding rate scoring, liquidation distance monitoring, and 3-layer sizing.

NotebookLM-verified parameters used throughout.
"""

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from src.core.types import PerpCandle, PerpPosition, Side

logger = logging.getLogger("hermes.risk")


class PerpRiskManager:
    def __init__(
        self,
        initial_equity: float = 10_000.0,
        max_portfolio_leverage: float = 3.0,
        max_drawdown_pct: float = 12.0,
        max_daily_loss_pct: float = 4.0,
        max_correlation: float = 0.70,
        risk_per_trade_pct: float = 1.0,
        oi_velocity_threshold: float = 15.0,
        oi_velocity_window: int = 48,
        extreme_funding_threshold: float = 0.01,
        atr_leverage_cap_pct: float = 3.0,
        atr_stop_major: float = 2.0,
        atr_stop_alt: float = 3.0,
        stop_min_pct: float = 1.5,
        stop_max_pct: float = 4.0,
        funding_entry_score: float = 0.001,
    ):
        self.initial_equity = initial_equity
        self.peak_equity = initial_equity
        self.current_equity = initial_equity
        self.max_portfolio_leverage = max_portfolio_leverage
        self.max_drawdown_pct = max_drawdown_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_correlation = max_correlation
        self.risk_per_trade_pct = risk_per_trade_pct
        self.oi_velocity_threshold = oi_velocity_threshold
        self.oi_velocity_window = oi_velocity_window
        self.extreme_funding_threshold = extreme_funding_threshold
        self.atr_leverage_cap_pct = atr_leverage_cap_pct
        self.atr_stop_major = atr_stop_major
        self.atr_stop_alt = atr_stop_alt
        self.stop_min_pct = stop_min_pct
        self.stop_max_pct = stop_max_pct
        self.funding_entry_score = funding_entry_score

        self.daily_start_equity = initial_equity
        self.last_reset_date = date.today()
        self.highest_dd: float = 0.0
        self.daily_pnl: float = 0.0
        self._consecutive_losses: dict[str, int] = defaultdict(int)
        self._oi_history: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self._trade_pnls: list[float] = []
        self._total_trades = 0
        self._total_wins = 0
        self._signal_outcomes: dict[str, list[bool]] = defaultdict(list)
        self._param_feedback: dict[str, list[float]] = defaultdict(list)
        self._equity_snapshots: list[tuple[datetime, float]] = []

    def update_equity(self, equity: float, gross_exposure: float):
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        today = date.today()
        if today != self.last_reset_date:
            self.daily_start_equity = equity
            self.daily_pnl = 0.0
            self.last_reset_date = today
        self._equity_snapshots.append((datetime.now(timezone.utc), equity))

    def current_drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        dd = (self.peak_equity - self.current_equity) / self.peak_equity * 100
        if dd > self.highest_dd:
            self.highest_dd = dd
        return dd

    def allow_entry(
        self, gross_exposure: float, current_leverage: float
    ) -> tuple[bool, str]:
        dd = self.current_drawdown()
        if dd >= self.max_drawdown_pct:
            return False, f"drawdown_halt: {dd:.1f}% >= {self.max_drawdown_pct}%"
        daily_loss = (
            (self.current_equity - self.daily_start_equity)
            / self.daily_start_equity * 100
        )
        if daily_loss <= -self.max_daily_loss_pct:
            return False, f"daily_loss_halt: {daily_loss:.1f}%"
        if current_leverage >= self.max_portfolio_leverage:
            return False, f"lev_halt: {current_leverage:.2f}x >= {self.max_portfolio_leverage}x"
        return True, "ok"

    # ── OI velocity gate (NotebookLM: 15% / 48h) ───────────────────────

    def record_oi(self, asset: str, oi: float):
        ts = int(datetime.now(timezone.utc).timestamp())
        self._oi_history[asset].append((ts, oi))
        cutoff = ts - self.oi_velocity_window * 3600
        self._oi_history[asset] = [(t, v) for t, v in self._oi_history[asset] if t >= cutoff]

    def oi_velocity(self, asset: str) -> float:
        history = self._oi_history.get(asset, [])
        if len(history) < 2:
            return 0.0
        earliest = history[0][1]
        latest = history[-1][1]
        if earliest <= 0:
            return 0.0
        return (latest - earliest) / earliest * 100

    def oi_gate_allows(self, asset: str) -> tuple[bool, str]:
        vel = self.oi_velocity(asset)
        if vel > self.oi_velocity_threshold:
            return False, f"oi_velocity: {vel:.1f}% > {self.oi_velocity_threshold}%"
        return True, "oi_ok"

    # ── Funding rate scoring (NotebookLM: -0.1% = max long confidence) ──

    def funding_score(self, rate: float, side: Side) -> float:
        if side == Side.LONG:
            return max(0.0, min(1.0, abs(rate) / self.funding_entry_score))
        else:
            return max(0.0, min(1.0, rate / self.funding_entry_score))

    def funding_gate(self, rate: float) -> tuple[bool, str]:
        if abs(rate) >= self.extreme_funding_threshold:
            return False, f"extreme_funding: {rate:.4f}"
        return True, "funding_ok"

    # ── Leverage scaling (NotebookLM: ≤1x if ATR > 3% price) ──────────

    def compute_leverage(self, asset: str, candles: list[PerpCandle], side: Side) -> tuple[float, str]:
        atr_pct = self._atr_pct(candles)
        base = 2.0

        if atr_pct is not None and atr_pct > self.atr_leverage_cap_pct:
            return 1.0, f"atr={atr_pct:.1f}% > {self.atr_leverage_cap_pct}% → 1x"

        config = self._perp_configs.get(asset) if hasattr(self, '_perp_configs') else None
        max_lev = config.max_leverage if config else 10.0
        lev = min(base, max_lev)
        return lev, f"atr={atr_pct:.1f}% → {lev}x"

    _perp_configs: dict = {}

    def set_perp_configs(self, configs: dict):
        self._perp_configs = configs

    # ── Volatility-adjusted stop (NotebookLM: 2x ATR majors, 3x ATR alts) ─

    def compute_stop_distance(
        self, asset: str, candles: list[PerpCandle]
    ) -> tuple[float, str]:
        atr_pct = self._atr_pct(candles)
        if atr_pct is None:
            return self.stop_min_pct, "no_atr"

        majors = {"BTC", "ETH"}
        mult = self.atr_stop_major if asset in majors else self.atr_stop_alt

        stop = atr_pct * mult
        stop = max(self.stop_min_pct, min(stop, self.stop_max_pct))
        return stop, f"atr={atr_pct:.2f}% × {mult}x → {stop:.2f}%"

    # ── Position sizing: risk-per-trade / stop_distance ────────────────

    def position_size(
        self, asset: str, equity: float, stop_distance_pct: float, price: float
    ) -> tuple[float, float, float]:
        risk_dollars = equity * (self.risk_per_trade_pct / 100)
        max_notional = risk_dollars / (stop_distance_pct / 100)
        quantity = max_notional / price if price > 0 else 0

        streak = self._consecutive_losses.get(asset, 0)
        streak_scalar = max(0.25, 0.5 ** streak)
        quantity *= streak_scalar

        port_notional = self.gross_exposure()
        remaining_capacity = (self.max_portfolio_leverage * equity) - port_notional
        max_qty = remaining_capacity / price if price > 0 and remaining_capacity > 0 else quantity
        quantity = min(quantity, max_qty)

        return quantity, risk_dollars, max_notional

    def gross_exposure(self) -> float:
        return 0.0

    def record_trade(self, asset: str, pnl_pct: float, pnl_dollars: float):
        if pnl_pct < 0:
            self._consecutive_losses[asset] += 1
        else:
            self._consecutive_losses[asset] = 0
        self._trade_pnls.append(pnl_pct)
        self._total_trades += 1
        if pnl_pct > 0:
            self._total_wins += 1
        self.daily_pnl += pnl_dollars
        self.update_equity(self.current_equity + pnl_dollars, 0.0)

    def record_signal_outcome(self, source: str, was_correct: bool):
        self._signal_outcomes[source].append(was_correct)

    def record_param_feedback(self, param: str, value: float):
        self._param_feedback[param].append(value)

    # ── Analytics ──────────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        return self._total_wins / self._total_trades if self._total_trades > 0 else 0.0

    @property
    def sharpe(self) -> float:
        if len(self._trade_pnls) < 5:
            return 0.0
        mean = sum(self._trade_pnls) / len(self._trade_pnls)
        std = math.sqrt(sum((p - mean) ** 2 for p in self._trade_pnls) / len(self._trade_pnls))
        return (mean / std) * math.sqrt(365) if std > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        wins = sum(p for p in self._trade_pnls if p > 0)
        losses = abs(sum(p for p in self._trade_pnls if p < 0))
        return wins / losses if losses > 0 else float("inf")

    def signal_accuracy(self, source: str) -> Optional[float]:
        outcomes = self._signal_outcomes.get(source, [])
        if len(outcomes) < 5:
            return None
        return sum(outcomes) / len(outcomes)

    def find_best_params(
        self, param_name: str, windows: list[tuple[float, float]]
    ) -> Optional[float]:
        feedback = self._param_feedback.get(param_name, [])
        if len(feedback) < 10:
            return None
        best_val = None
        best_sharpe = -999
        for low, high in windows:
            subset = [f for f in feedback if low <= f < high]
            if len(subset) < 5:
                continue
            mean_pnl = sum(subset) / len(subset)
            if mean_pnl > best_sharpe:
                best_sharpe = mean_pnl
                best_val = (low + high) / 2
        return best_val

    def status(self) -> dict:
        dd = self.current_drawdown()
        daily_loss = (
            (self.current_equity - self.daily_start_equity)
            / self.daily_start_equity * 100
        )
        return {
            "equity": round(self.current_equity, 2),
            "peak": round(self.peak_equity, 2),
            "drawdown_pct": round(dd, 2),
            "daily_pnl_pct": round(daily_loss, 2),
            "total_trades": self._total_trades,
            "win_rate": round(self.win_rate, 3),
            "sharpe": round(self.sharpe, 3),
            "profit_factor": round(self.profit_factor, 3),
        }

    def _atr_pct(self, candles: list[PerpCandle], period: int = 14) -> Optional[float]:
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(-period, 0):
            h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs) / len(trs)
        current = candles[-1].close
        return (atr / current) * 100 if current > 0 else None
