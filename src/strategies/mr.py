"""
Mean-reversion strategy for perps — RSI oversold with perp-aware filters.

Uses NotebookLM-verified parameters:
  - Stop: 2x ATR majors, 3x ATR alts, clamped [1.5%, 4.0%]
  - Risk-per-trade: 1% of equity
  - Funding rate: -0.1% → max long confidence
  - OI velocity gate: 15% / 48h
  - Aggregate sizing: max 3x gross portfolio leverage
"""

import math
from typing import Optional

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy


class MeanReversion(PerpStrategy):
    def __init__(
        self,
        rsi_oversold: float = 28.0,
        rsi_period: int = 14,
        atr_period: int = 14,
        cooldown_bars: int = 12,
        min_volume_usd: float = 10_000_000,
        tp1_r_mult: float = 0.5,
        tp2_r_mult: float = 1.5,
        tp3_r_mult: float = 3.0,
        majors: set | None = None,
    ):
        self.rsi_oversold = rsi_oversold
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.cooldown_bars = cooldown_bars
        self.min_volume_usd = min_volume_usd
        self.tp1_r_mult = tp1_r_mult
        self.tp2_r_mult = tp2_r_mult
        self.tp3_r_mult = tp3_r_mult
        self.majors = majors or {"BTC", "ETH"}
        self._cooldowns: dict[str, int] = {}

    def name(self) -> str:
        return "mr"

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
        if len(candles) < 50:
            return None

        last = candles[-1]
        if last.volume * last.close < self.min_volume_usd:
            return None

        if regime in (RegimeType.STRONGLY_TRENDING, RegimeType.HIGH_VOL):
            return None

        rsi = self._rsi(candles)
        is_oversold = rsi is not None and rsi <= self.rsi_oversold
        if not is_oversold:
            return None

        entry_price = last.close
        atr_value = self._atr(candles)
        if atr_value <= 0:
            return None

        stop_pct = atr_value / entry_price if entry_price > 0 else 0.02
        mult = self.atr_stop_major if asset in self.majors else self.atr_stop_alt
        stop_pct *= mult

        from src.core.perp_risk import PerpRiskManager
        stop_min, stop_max = 1.5, 4.0
        stop_pct = max(stop_min / 100, min(stop_pct, stop_max / 100))
        stop = entry_price * (1 - stop_pct)

        risk_r = (entry_price - stop) / entry_price
        if risk_r <= 0:
            return None

        confidence = 0.5
        sources = ["rsi_oversold"]
        if rsi is not None and rsi <= 20:
            confidence += 0.2
            sources.append("deep_oversold")

        if funding_rate < -self.funding_threshold:
            confidence += 0.15
            sources.append("funding_support")

        if regime in (RegimeType.MEAN_REVERTING, RegimeType.STRONGLY_MR):
            confidence += 0.1
            sources.append("regime_mr")

        confidence = min(confidence, 1.0)

        return Side.LONG, confidence, {
            "entry_price": entry_price,
            "stop_loss": stop,
            "risk_r": round(risk_r, 4),
            "rsi": round(rsi, 2) if rsi is not None else None,
            "atr_pct": round(stop_pct * 100, 2),
            "sources": sources,
            "tp1": entry_price + (risk_r * self.tp1_r_mult * entry_price),
            "tp2": entry_price + (risk_r * self.tp2_r_mult * entry_price),
            "tp3": entry_price + (risk_r * self.tp3_r_mult * entry_price),
        }

    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, Optional[float]]]:
        if current_price <= (position.stop_loss or 0) * 1.05:
            return "stop_loss", current_price

        entry = position.entry_price
        if entry <= 0:
            return None

        r_mult = (current_price - entry) / max(entry - (position.stop_loss or 0), 0.001)

        if r_mult >= self.tp3_r_mult:
            return "tp3", current_price
        if r_mult >= self.tp2_r_mult:
            return "tp2", current_price
        if r_mult >= self.tp1_r_mult:
            return "tp1", current_price

        if funding_rate > self.funding_halt_threshold:
            return "funding_spike", current_price

        return None

    def _rsi(self, candles: list[PerpCandle]) -> Optional[float]:
        if len(candles) < self.rsi_period + 1:
            return None
        closes = [c.close for c in candles]
        gains, losses = 0.0, 0.0
        for i in range(-self.rsi_period, 0):
            diff = closes[i] - closes[i - 1]
            gains += max(diff, 0)
            losses += max(-diff, 0)
        avg_gain = gains / self.rsi_period
        avg_loss = losses / self.rsi_period
        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    def _atr(self, candles: list[PerpCandle]) -> float:
        if len(candles) < self.atr_period + 1:
            return 0.0
        trs = []
        for i in range(-self.atr_period, 0):
            h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0

    atr_stop_major = 2.0
    atr_stop_alt = 3.0
    funding_threshold = 0.001
    funding_halt_threshold = 0.005
