"""
4-hour Trend Donchian Breakout strategy.

Phase 2: Simplified trend-following on 4h candles.
- 20-period Donchian breakout (long above upper, short below lower)
- 50 EMA bias (long only above, short only below, adapted for 4h data depth)
- Exit: ATR trailing stop (4.0x) or time exit at 120h
- No regime gate, no drift filter, no funding checks
"""

from typing import Optional
from datetime import datetime, timezone
import logging

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy

logger = logging.getLogger(__name__)


class Trend4h(PerpStrategy):
    def __init__(
        self,
        breakout_period: int = 20,
        atr_period: int = 22,
        atr_mult: float = 4.0,
        ema_bias_period: int = 50,
        time_exit_hours: float = 120.0,
        min_volume_usd: float = 0,
        cooldown_cycles: int = 60,
        majors: set | None = None,
        signal_tracker=None,
    ):
        self.breakout_period = breakout_period
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.ema_bias_period = ema_bias_period
        self.time_exit_hours = time_exit_hours
        self.min_volume_usd = min_volume_usd
        self.cooldown_cycles = cooldown_cycles
        self.majors = majors or {"BTC", "ETH"}
        self.signal_tracker = signal_tracker
        self._cooldowns: dict[str, int] = {}

    def name(self) -> str:
        return "trend_4h"

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
        if len(candles) < self.breakout_period + self.atr_period + 5:
            return None

        last = candles[-1]

        vol_min = self._get_threshold(asset, "volume_min_usd", self.min_volume_usd)
        if last.volume * last.close < vol_min:
            return None

        ema200 = self._ema(candles, self.ema_bias_period)
        if ema200 is None or ema200 <= 0:
            return None

        above_ema = last.close > ema200
        below_ema = last.close < ema200

        recent = candles[-(self.breakout_period + 1):-1]
        if len(recent) < self.breakout_period:
            return None
        upper = max(c.high for c in recent)
        lower = min(c.low for c in recent)

        is_long = last.close > upper and above_ema
        is_short = last.close < lower and below_ema

        if not (is_long or is_short):
            logger.info("TREND_4H %s: no breakout close=%.2f upper=%.2f lower=%.2f ema200=%.2f",
                        asset, last.close, upper, lower, ema200)
            return None

        if is_long:
            breakout_pct = (last.close - upper) / upper
        else:
            breakout_pct = (lower - last.close) / lower

        confidence = 0.60
        if breakout_pct > 0.01:
            confidence += 0.10
        if breakout_pct > 0.03:
            confidence += 0.10
        confidence = min(confidence, 0.90)

        side = Side.LONG if is_long else Side.SHORT
        entry_price = last.close

        confidence = self.blend_altfins_confidence(confidence, signals)
        return side, confidence, {
            "entry_price": entry_price,
            "donchian_upper": round(upper, 2),
            "donchian_lower": round(lower, 2),
            "breakout_pct": round(breakout_pct * 100, 3),
            "ema200": round(ema200, 2),
            "side": side.value,
            "sources": ["trend_4h_donchian", f"breakout_{breakout_pct:.3f}"],
        }

    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, Optional[float]]]:
        age_hours = (datetime.now(timezone.utc) - position.entry_time).total_seconds() / 3600
        if age_hours > self.time_exit_hours:
            return "time_exit", current_price

        atr = self._atr(candles)
        if atr <= 0:
            return None

        stop_dist = atr * self.atr_mult
        min_dist = 0.015 * current_price
        max_dist = 0.08 * current_price
        stop_dist = max(min_dist, min(max_dist, stop_dist))

        since_entry = [c for c in candles if c.timestamp >= position.entry_time.timestamp()]
        if not since_entry:
            since_entry = [candles[-1]]

        is_short = position.side == Side.SHORT
        if is_short:
            anchor = min(c.low for c in since_entry)
            chandelier = anchor + stop_dist
            if current_price >= chandelier:
                self._cooldowns[asset] = self.cooldown_cycles
                return "chandelier", current_price
        else:
            anchor = max(c.high for c in since_entry)
            chandelier = anchor - stop_dist
            if current_price <= chandelier:
                self._cooldowns[asset] = self.cooldown_cycles
                return "chandelier", current_price

        return None

    def on_exit(self, asset: str) -> None:
        self._cooldowns[asset] = self.cooldown_cycles

    def _ema(self, candles: list[PerpCandle], period: int) -> Optional[float]:
        if len(candles) < period:
            return None
        closes = [c.close for c in candles]
        multiplier = 2.0 / (period + 1)
        ema = sum(closes[:period]) / period
        for price in closes[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def _atr(self, candles: list[PerpCandle]) -> float:
        if len(candles) < self.atr_period + 1:
            return 0.0
        trs = []
        for i in range(-self.atr_period, 0):
            h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0
