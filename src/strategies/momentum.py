"""
Pure momentum strategy — bypasses regime classifier using drift filter.

NotebookLM round 10: when the regime classifier is broken, a pure momentum sleeve
using drift filter (not Hurst) can authorize entries for clear directional moves.

Entry:
- If bearish_drift: enter short on price weakness (close < EMA9)
- If bullish_drift: enter long on price strength (close > EMA9)
- Has own regime requirement: drift MUST be aligned with direction
- No TRENDING/STRONGLY_TRENDING regime required
- Capped at smaller size (this is a fallback sleeve)

Exit: ATR-based stop, breakeven after 1.5× risk captured
"""

from typing import Optional
from datetime import datetime, timezone
import logging

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy

logger = logging.getLogger(__name__)


class DriftMomentum(PerpStrategy):
    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        atr_period: int = 14,
        atr_stop_mult: float = 2.5,
        cooldown_cycles: int = 30,
        min_volume_usd: float = 2_000_000,
        majors: set | None = None,
        signal_tracker=None,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.cooldown_cycles = cooldown_cycles
        self.min_volume_usd = min_volume_usd
        self.majors = majors or {"BTC", "ETH"}
        self.signal_tracker = signal_tracker
        self._cooldowns: dict[str, int] = {}
        # Track whether we've captured enough profit to move stop to breakeven
        self._breakeven_set: dict[str, bool] = {}

    def name(self) -> str:
        return "drift_momentum"

    def should_enter(
        self,
        asset: str,
        candles: list[PerpCandle],
        signals: list[Signal],
        regime: RegimeType,
        position: Optional[PerpPosition],
        funding_rate: float,
    ) -> Optional[tuple[Side, float, dict]]:
        if self._cooldowns.get(asset, 0) > 0:
            self._cooldowns[asset] -= 1
            return None

        if position is not None:
            return None

        if len(candles) < 30:
            return None

        # Hard block: high volatility is dangerous
        if regime == RegimeType.HIGH_VOL:
            return None
        if regime == RegimeType.DEAD_MARKET:
            return None

        last = candles[-1]
        if last.volume * last.close < self.min_volume_usd:
            return None

        # Drift filter — REQUIRED for this strategy
        drift = self._asset_drift(candles)
        if drift == "neutral":
            return None

        is_long = drift == "bullish_drift"
        is_short = drift == "bearish_drift"

        if not (is_long or is_short):
            return None

        # EMA crossover confirmation
        ema_f = self._ema(candles, self.ema_fast)
        ema_s = self._ema(candles, self.ema_slow)
        if ema_f is None or ema_s is None:
            return None

        if is_long and ema_f <= ema_s:
            logger.info("DRIFT_MOMENTUM %s: long blocked, ema_f <= ema_s (%.2f <= %.2f)",
                        asset, ema_f, ema_s)
            return None
        if is_short and ema_f >= ema_s:
            logger.info("DRIFT_MOMENTUM %s: short blocked, ema_f >= ema_s (%.2f >= %.2f)",
                        asset, ema_f, ema_s)
            return None

        # RSI check
        rsi = self._rsi(candles, 14)
        if is_long and rsi > 75:
            return None
        if is_short and rsi < 25:
            return None

        # Funding rate check
        if is_long and funding_rate > 0.0005:
            return None
        if is_short and funding_rate < -0.0005:
            return None

        # Confidence: moderate base
        confidence = 0.55
        # Higher if EMA cross is fresh
        ema_diff_pct = abs(ema_f - ema_s) / ema_s if ema_s > 0 else 0
        if ema_diff_pct > 0.005:
            confidence += 0.05
        if ema_diff_pct > 0.015:
            confidence += 0.05
        # Boost if drift is strong
        recent = candles[-48:]
        up_ratio = sum(1 for c in recent if c.close >= c.open) / len(recent)
        if is_long and up_ratio > 0.70:
            confidence += 0.05
        if is_short and up_ratio < 0.30:
            confidence += 0.05

        confidence = min(confidence, 0.85)
        side = Side.LONG if is_long else Side.SHORT
        entry_price = last.close

        return side, confidence, {
            "entry_price": entry_price,
            "ema_fast": round(ema_f, 2),
            "ema_slow": round(ema_s, 2),
            "ema_diff_pct": round(ema_diff_pct * 100, 3),
            "drift": drift,
            "rsi": round(rsi, 2),
            "funding_rate": funding_rate,
            "side": side.value,
            "sources": ["drift_momentum", f"drift_{drift}"],
        }

    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, float]]:
        if len(candles) < 22:
            return None

        atr = self._atr(candles)
        if atr <= 0:
            return None

        # Stop loss
        if position.side == Side.LONG:
            stop = position.entry_price - atr * self.atr_stop_mult
            if current_price < stop:
                return "drift_stop", stop
            # Move to breakeven after 1.5x risk captured
            target = position.entry_price + atr * self.atr_stop_mult * 1.5
            if current_price >= target:
                self._breakeven_set[asset] = True
            if self._breakeven_set.get(asset) and current_price < position.entry_price:
                return "drift_breakeven", position.entry_price
        else:
            stop = position.entry_price + atr * self.atr_stop_mult
            if current_price > stop:
                return "drift_stop", stop
            target = position.entry_price - atr * self.atr_stop_mult * 1.5
            if current_price <= target:
                self._breakeven_set[asset] = True
            if self._breakeven_set.get(asset) and current_price > position.entry_price:
                return "drift_breakeven", position.entry_price

        # Trailing exit: if drift reverses, exit
        drift = self._asset_drift(candles)
        if position.side == Side.LONG and drift == "bearish_drift":
            return "drift_reversal", current_price
        if position.side == Side.SHORT and drift == "bullish_drift":
            return "drift_reversal", current_price

        return None

    def on_exit(self, asset: str) -> None:
        self._cooldowns[asset] = self.cooldown_cycles
        self._breakeven_set.pop(asset, None)

    @staticmethod
    def _ema(candles: list[PerpCandle], period: int) -> Optional[float]:
        if len(candles) < period:
            return None
        closes = [c.close for c in candles]
        k = 2.0 / (period + 1)
        ema = closes[0]
        for c in closes[1:]:
            ema = c * k + ema * (1 - k)
        return ema

    @staticmethod
    def _atr(candles: list[PerpCandle], period: int = 14) -> float:
        if len(candles) < period + 1:
            return 0.0
        trs = []
        for i in range(-period, 0):
            h = candles[i].high
            l = candles[i].low
            pc = candles[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0

    @staticmethod
    def _rsi(candles: list[PerpCandle], period: int = 14) -> float:
        if len(candles) < period + 1:
            return 50.0
        closes = [c.close for c in candles]
        gains = []
        losses = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(-diff)
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _asset_drift(candles: list[PerpCandle]) -> str:
        if len(candles) < 48:
            return "neutral"
        recent = candles[-48:]
        up_count = sum(1 for c in recent if c.close >= c.open)
        ratio = up_count / len(recent)
        if ratio > 0.60:
            return "bullish_drift"
        if ratio < 0.40:
            return "bearish_drift"
        return "neutral"
