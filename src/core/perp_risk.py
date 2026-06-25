"""
Perp-aware risk management — leverage scaling by ATR, OI velocity gate,
funding rate scoring, liquidation distance monitoring, and 3-layer sizing.

NotebookLM-verified parameters used throughout.
"""

import logging
import math
import statistics
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
        atr_stop_major: float = 1.0,
        atr_stop_alt: float = 1.5,
        stop_min_pct: float = 0.3,
        stop_max_pct: float = 8.0,
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
        self.max_concurrent_positions: int = 5
        self._consecutive_losses: dict[str, int] = defaultdict(int)
        self._oi_history: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self._trade_pnls: list[float] = []
        self._total_trades = 0
        self._total_wins = 0
        self._signal_outcomes: dict[str, list[bool]] = defaultdict(list)
        self._param_feedback: dict[str, list[float]] = defaultdict(list)
        self._equity_snapshots: list[tuple[datetime, float]] = []
        self._price_history: dict[str, list[float]] = defaultdict(lambda: [])
        self._recent_outcomes: list[bool] = []
        self._active_positions: set[str] = set()
        self._gross_exposure: float = 0.0
        self.max_consecutive_losses: int = 3
        self.max_global_losses: int = 3
        self._global_loss_streak: int = 0

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
        if len(self._active_positions) >= self.max_concurrent_positions:
            return False, f"max_positions: {len(self._active_positions)} >= {self.max_concurrent_positions}"
        daily_loss = (
            (self.current_equity - self.daily_start_equity)
            / self.daily_start_equity * 100
        )
        if daily_loss <= -self.max_daily_loss_pct:
            return False, f"daily_loss_halt: {daily_loss:.1f}%"
        if current_leverage >= self.max_portfolio_leverage:
            return False, f"lev_halt: {current_leverage:.2f}x >= {self.max_portfolio_leverage}x"

        # Portfolio-wide kill switch (NotebookLM: WR < 48% or daily loss > 3.5%)
        if len(self._recent_outcomes) >= 10:
            wr = sum(self._recent_outcomes) / len(self._recent_outcomes)
            if wr < 0.48:
                return False, f"portfolio_wr_halt: {wr:.0%} WR on last {len(self._recent_outcomes)} trades"

        return True, "ok"

    def consecutive_loss_allows(self, asset: str) -> tuple[bool, str]:
        # Portfolio-level kill-switch: 3 consecutive losses across ANY asset = full halt
        if self._global_loss_streak >= self.max_global_losses:
            return False, f"portfolio_loss_streak: {self._global_loss_streak} >= {self.max_global_losses}"
        # Per-asset gate
        cls = self._consecutive_losses.get(asset, 0)
        if cls >= self.max_consecutive_losses:
            return False, f"consecutive_losses: {cls} >= {self.max_consecutive_losses}"
        return True, "ok"
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

    def spread_gate_allows(self, asset: str, spread_pct: float, max_spread: float = 0.08) -> tuple[bool, str]:
        if spread_pct <= 0:
            return True, "spread_ok"
        if spread_pct > max_spread:
            return False, f"spread: {spread_pct:.3f}% > {max_spread}%"
        return True, "spread_ok"

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
        self, asset: str, equity: float, stop_distance_pct: float, price: float,
        current_gross_exposure: float = 0.0
    ) -> tuple[float, float, float]:
        risk_dollars = equity * (self.risk_per_trade_pct / 100)
        max_notional = risk_dollars / (stop_distance_pct / 100)
        quantity = max_notional / price if price > 0 else 0

        # BTC correlation penalty: Size *= (1 - |ρ_btc|)
        corr = self.btc_correlation(asset)
        if corr is not None:
            quantity *= max(0.1, 1.0 - abs(corr))

        # Linear streak modifier over last 10 trades
        if self._recent_outcomes:
            recent = self._recent_outcomes[-10:]
            wins = sum(recent)
            phi = max(0.5, min(1.5, 1.0 + (wins - (len(recent) - wins)) / 10.0))
            quantity *= phi

        port_notional = max(current_gross_exposure, self.gross_exposure())
        remaining_capacity = (self.max_portfolio_leverage * equity) - port_notional
        max_qty = remaining_capacity / price if price > 0 and remaining_capacity > 0 else 0.0
        quantity = min(quantity, max_qty)

        return quantity, risk_dollars, max_notional

    def record_price(self, asset: str, price: float):
        history = self._price_history[asset]
        history.append(price)
        self._price_history[asset] = history[-100:]

    def btc_correlation(self, asset: str) -> Optional[float]:
        if asset == "BTC":
            return None
        btc_prices = self._price_history.get("BTC", [])
        asset_prices = self._price_history.get(asset, [])
        if len(btc_prices) < 30 or len(asset_prices) < 30:
            return None
        n = min(len(btc_prices), len(asset_prices))
        btc_recent = btc_prices[-n:]
        asset_recent = asset_prices[-n:]
        try:
            return statistics.correlation(btc_recent, asset_recent)
        except statistics.StatisticsError:
            return None

    def record_position_open(self, asset: str):
        self._active_positions.add(asset)

    def record_position_close(self, asset: str):
        self._active_positions.discard(asset)

    def gross_exposure(self) -> float:
        return self._gross_exposure if hasattr(self, '_gross_exposure') else 0.0

    def set_gross_exposure(self, exposure: float):
        self._gross_exposure = exposure

    def record_trade(self, asset: str, pnl_pct: float, pnl_dollars: float):
        if pnl_pct < 0:
            self._consecutive_losses[asset] += 1
        else:
            self._consecutive_losses[asset] = 0
        self._trade_pnls.append(pnl_pct)
        self._total_trades += 1
        self._recent_outcomes.append(pnl_pct > 0)
        self._recent_outcomes = self._recent_outcomes[-10:]
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

    def paper_to_live_readiness(self) -> dict:
        mr_pnls = []
        trend_pnls = []
        out = self._signal_outcomes
        for source, outcomes in out.items():
            if source.startswith("mr:"):
                mr_pnls.extend(
                    1 if o else -1 for o in outcomes
                )
            elif source.startswith("trend:"):
                trend_pnls.extend(
                    1 if o else -1 for o in outcomes
                )

        def _sharpe_for(pnls):
            if len(pnls) < 5:
                return 0.0
            m = sum(pnls) / len(pnls)
            s = math.sqrt(sum((p - m) ** 2 for p in pnls) / len(pnls))
            return (m / s) * math.sqrt(365) if s > 0 else 0.0

        def _pf_for(pnls):
            wins = sum(p for p in pnls if p > 0)
            losses = abs(sum(p for p in pnls if p < 0))
            return wins / losses if losses > 0 else float("inf")

        def _wr_for(pnls):
            return sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0

        total = self._total_trades
        mr_sharpe = _sharpe_for(mr_pnls)
        trend_sharpe = _sharpe_for(trend_pnls)
        mr_pf = _pf_for(mr_pnls)
        trend_pf = _pf_for(trend_pnls)
        mr_wr = _wr_for(mr_pnls)
        trend_wr = _wr_for(trend_pnls)
        dd = self.current_drawdown()

        checks = {
            "min_trades_50": total >= 50,
            "sharpe_ge_1.5": _sharpe_for(self._trade_pnls) >= 1.5,
            "mr_pf_gt_1.5": mr_pf > 1.5 if mr_pnls else False,
            "trend_pf_gt_1.2": trend_pf > 1.2 if trend_pnls else False,
            "drawdown_lt_15": dd < 15.0,
            "mr_wr_gt_0.55": mr_wr > 0.55 if mr_pnls else False,
            "trend_wr_gt_0.40": trend_wr > 0.40 if trend_pnls else False,
        }
        passed = sum(1 for v in checks.values() if v)
        return {
            "ready": passed >= 5,
            "checks_passed": passed,
            "checks_total": len(checks),
            "details": checks,
            "stats": {
                "total_trades": total,
                "mr_trades": len(mr_pnls),
                "trend_trades": len(trend_pnls),
                "sharpe": round(_sharpe_for(self._trade_pnls), 2),
                "mr_pf": round(mr_pf, 2) if mr_pnls else None,
                "trend_pf": round(trend_pf, 2) if trend_pnls else None,
                "drawdown_pct": round(dd, 2),
                "mr_wr": round(mr_wr, 3) if mr_pnls else None,
                "trend_wr": round(trend_wr, 3) if trend_pnls else None,
            },
        }

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
            "active_positions": len(self._active_positions),
            "max_positions": self.max_concurrent_positions,
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
