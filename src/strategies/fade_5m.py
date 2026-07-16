"""
5-minute Fade-the-Wick strategy.

Entry: price deviates >3σ from 20-period SMA on 5min data → fade it
- 3.0σ threshold for majors (BTC/ETH), 3.5σ for altcoins
- Exit: 1.0x ATR stop, 2.0x ATR profit target
- Cooldown: 12 cycles after exit
"""

from typing import Optional
from datetime import datetime, timezone
import math
import logging

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy

logger = logging.getLogger(__name__)


class Fade5m(PerpStrategy):
    def __init__(
        self,
        sma_period: int = 20,
        z_entry_major: float = 3.0,
        z_entry_alt: float = 3.5,
        z_min_major: float = 1.5,
        z_min_alt: float = 2.0,
        atr_period: int = 14,
        stop_atr_mult: float = 1.0,
        target_atr_mult: float = 2.0,
        min_volume_usd: float = 100_000,
        cooldown_cycles: int = 12,
        majors: set | None = None,
        signal_tracker=None,
    ):
        self.sma_period = sma_period
        self.z_entry_major = z_entry_major
        self.z_entry_alt = z_entry_alt
        self.z_min_major = z_min_major
        self.z_min_alt = z_min_alt
        self.atr_period = atr_period
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self.min_volume_usd = min_volume_usd
        self.cooldown_cycles = cooldown_cycles
        self.majors = majors or {"BTC", "ETH"}
        self.signal_tracker = signal_tracker
        self._cooldowns: dict[str, int] = {}

    def name(self) -> str:
        return "fade_5m"

    def _z_score(self, candles: list[PerpCandle]) -> Optional[float]:
        """Z-score of last close relative to SMA."""
        if len(candles) < self.sma_period + 1:
            return None
        closes = [c.close for c in candles[-self.sma_period - 1:]]
        sma = sum(closes[:-1]) / self.sma_period
        variance = sum((c - sma) ** 2 for c in closes[:-1]) / self.sma_period
        std = math.sqrt(variance) if variance > 0 else 0.0001
        return (closes[-1] - sma) / std

    def _atr(self, candles: list[PerpCandle]) -> float:
        if len(candles) < self.atr_period + 1:
            return 0.0
        trs = []
        for i in range(-self.atr_period, 0):
            h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0

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
        if len(candles) < self.sma_period + self.atr_period + 2:
            return None

        last = candles[-1]
        vol_min = self._get_threshold(asset, "volume_min_usd", self.min_volume_usd)
        if last.volume * last.close < vol_min:
            return None

        z = self._z_score(candles)
        if z is None:
            return None

        is_major = asset in self.majors
        entry_threshold = self.z_entry_major if is_major else self.z_entry_alt

        is_overbought = z > entry_threshold
        is_oversold = z < -entry_threshold

        if not (is_overbought or is_oversold):
            return None

        is_short = is_overbought
        side = Side.SHORT if is_short else Side.LONG

        atr = self._atr(candles)
        if atr <= 0:
            return None

        entry_price = last.close

        # Confidence scales with deviation strength
        z_min = self.z_min_major if is_major else self.z_min_alt
        if is_short:
            confidence = min((z - z_min) / (entry_threshold - z_min) * 0.55, 0.85)
        else:
            confidence = min((abs(z) - z_min) / (entry_threshold - z_min) * 0.55, 0.85)
        confidence = max(confidence, 0.50)

        stop_price = (
            entry_price + atr * self.stop_atr_mult
            if is_short
            else entry_price - atr * self.stop_atr_mult
        )
        target_price = (
            entry_price - atr * self.target_atr_mult
            if is_short
            else entry_price + atr * self.target_atr_mult
        )

        confidence = self.blend_altfins_confidence(confidence, signals)
        return side, confidence, {
            "entry_price": entry_price,
            "z_score": round(z, 2),
            "threshold": entry_threshold,
            "atr": round(atr, 4),
            "stop_price": round(stop_price, 2),
            "target_price": round(target_price, 2),
            "side": side.value,
            "sources": [f"fade_z{z:.1f}", f"threshold_{entry_threshold}"],
        }

    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, Optional[float]]]:
        is_short = position.side == Side.SHORT
        entry = position.entry_price

        pnl_pct = (current_price - entry) / entry
        if is_short:
            pnl_pct = -pnl_pct

        # Profit target
        atr = self._atr(candles)
        if atr > 0:
            if is_short:
                if current_price <= entry - atr * self.target_atr_mult:
                    self._cooldowns[asset] = self.cooldown_cycles
                    return "fade_target", current_price
                if current_price >= entry + atr * self.stop_atr_mult:
                    self._cooldowns[asset] = self.cooldown_cycles
                    return "fade_stop", current_price
            else:
                if current_price >= entry + atr * self.target_atr_mult:
                    self._cooldowns[asset] = self.cooldown_cycles
                    return "fade_target", current_price
                if current_price <= entry - atr * self.stop_atr_mult:
                    self._cooldowns[asset] = self.cooldown_cycles
                    return "fade_stop", current_price

        # Fallback: time exit after 2h
        age_hours = (datetime.now(timezone.utc) - position.entry_time).total_seconds() / 3600
        if age_hours > 2.0:
            self._cooldowns[asset] = self.cooldown_cycles
            return "fade_time_exit", current_price

        return None

    def on_exit(self, asset: str) -> None:
        self._cooldowns[asset] = self.cooldown_cycles
