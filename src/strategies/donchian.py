"""
Donchian breakout strategy for perps — 20-bar high/low breakout with ATR trail exit.

NotebookLM round 10: EMA crossovers are mathematically lagging for intraday crypto.
Donchian channel breakouts capture momentum shifts earlier with fewer false signals.

Entry: price breaks above N-bar high (long) or below N-bar low (short)
Exit:  ATR(14) × 2.0x trailing stop from highest high / lowest low
Cooldown: 30 cycles after exit to prevent whipsaw
"""

from typing import Optional
from datetime import datetime, timezone
import logging

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy

logger = logging.getLogger(__name__)


class DonchianBreakout(PerpStrategy):
    def __init__(
        self,
        breakout_period: int = 20,
        atr_period: int = 14,
        atr_trail_major: float = 2.0,
        atr_trail_alt: float = 3.0,
        cooldown_cycles: int = 30,
        min_volume_usd: float = 2_000_000,
        majors: set | None = None,
        signal_tracker=None,
    ):
        self.breakout_period = breakout_period
        self.atr_period = atr_period
        self.atr_trail_major = atr_trail_major
        self.atr_trail_alt = atr_trail_alt
        self.cooldown_cycles = cooldown_cycles
        self.min_volume_usd = min_volume_usd
        self.majors = majors or {"BTC", "ETH"}
        self.signal_tracker = signal_tracker
        self._cooldowns: dict[str, int] = {}

    def name(self) -> str:
        return "donchian"

    def should_enter(
        self,
        asset: str,
        candles: list[PerpCandle],
        signals: list[Signal],
        regime: RegimeType,
        position: Optional[PerpPosition],
        funding_rate: float,
    ) -> Optional[tuple[Side, float, dict]]:
        # Cooldown check
        if self._cooldowns.get(asset, 0) > 0:
            self._cooldowns[asset] -= 1
            return None

        # Already in position
        if position is not None:
            return None

        # Need enough candles
        if len(candles) < self.breakout_period + self.atr_period + 5:
            return None

        # Don't enter in dead market
        if regime == RegimeType.DEAD_MARKET:
            return None

        last = candles[-1]
        prev = candles[-2] if len(candles) >= 2 else last

        # Volume gate
        vol_min = self._get_threshold(asset, "volume_min_usd", self.min_volume_usd)
        if last.volume * last.close < vol_min:
            return None

        # Donchian channel: highest high / lowest low over breakout period (excluding last bar)
        # Standard Donchian uses the prior N bars, then today's close breaks out
        recent = candles[-(self.breakout_period + 1) : -1]
        if len(recent) < self.breakout_period:
            return None
        upper = max(c.high for c in recent)
        lower = min(c.low for c in recent)

        # ATR for stop distance
        atr = self._atr(candles)
        if atr <= 0:
            return None

        # Entry signals
        is_long_breakout = last.close > upper
        is_short_breakout = last.close < lower

        if not (is_long_breakout or is_short_breakout):
            return None

        # Drift filter: block opposite direction
        # bullish_drift blocks shorts, bearish_drift blocks longs
        drift = self._asset_drift(candles)
        if is_long_breakout and drift == "bearish_drift":
            logger.info("DONCHIAN %s: bearish drift blocks long", asset)
            return None
        if is_short_breakout and drift == "bullish_drift":
            logger.info("DONCHIAN %s: bullish drift blocks short", asset)
            return None

        is_long = is_long_breakout

        # EMA50 divergence: if price is far from EMA50, only trade in that direction
        ema50 = self._ema(candles, 50)
        if ema50 is not None and ema50 > 0:
            divergence = (last.close - ema50) / ema50
            if divergence > 0.03 and not is_long:
                logger.info("DONCHIAN %s: price %.1f%% above EMA50, short blocked",
                            asset, divergence * 100)
                return None
            if divergence < -0.03 and is_long:
                logger.info("DONCHIAN %s: price %.1f%% below EMA50, long blocked",
                            asset, divergence * 100)
                return None

        # Debug: log when entry conditions met
        logger.info("DONCHIAN %s: side=%s drift=%s regime=%s",
                    asset, "LONG" if is_long else "SHORT", drift, regime.value)

        # Base confidence
        # Higher when breakout is decisive (close far beyond channel)
        if is_long:
            breakout_pct = (last.close - upper) / upper
        else:
            breakout_pct = (lower - last.close) / lower

        confidence = 0.55
        if breakout_pct > 0.005:  # >0.5% beyond channel
            confidence += 0.05
        if breakout_pct > 0.015:  # >1.5% beyond channel
            confidence += 0.05
        if atr > 0:
            vol_signal = atr / last.close  # normalized volatility
            if vol_signal > 0.01:  # 1% ATR — moderate vol
                confidence += 0.03
            if vol_signal > 0.025:  # 2.5% ATR — high vol
                confidence += 0.02

        # Funding rate boost
        if is_long and funding_rate < -0.0005:
            confidence += 0.05  # shorts paying → short squeeze fuel
        if not is_long and funding_rate > 0.0005:
            confidence += 0.05  # longs paying → long squeeze fuel

        # RSI check: don't enter long if RSI is way overbought, etc.
        # (We use simple RSI to avoid chasing blowoffs)
        rsi = self._rsi(candles, 14)
        if is_long and rsi > 80:
            logger.info("DONCHIAN %s: long blocked, RSI %.1f > 80", asset, rsi)
            return None
        if not is_long and rsi < 20:
            logger.info("DONCHIAN %s: short blocked, RSI %.1f < 20", asset, rsi)
            return None

        confidence = min(confidence, 1.0)
        side = Side.LONG if is_long else Side.SHORT

        entry_price = last.close
        sources = ["donchian_breakout", f"vol_signal_{vol_signal:.3f}"]
        if funding_rate < -0.0005:
            sources.append("funding_short_squeeze")
        elif funding_rate > 0.0005:
            sources.append("funding_long_squeeze")

        return side, confidence, {
            "entry_price": entry_price,
            "donchian_upper": round(upper, 2),
            "donchian_lower": round(lower, 2),
            "breakout_pct": round(breakout_pct * 100, 3),
            "atr": round(atr, 4),
            "rsi": round(rsi, 2),
            "funding_rate": funding_rate,
            "side": side.value,
            "sources": sources,
        }

    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, float]]:
        """Chandelier exit: highest high/lowest low - ATR × multiplier"""
        if len(candles) < 22:
            return None

        last = candles[-1]
        atr = self._atr(candles)
        if atr <= 0:
            return None

        mult = self.atr_trail_major if asset in self.majors else self.atr_trail_alt

        if position.side == Side.LONG:
            highest_high = max(c.high for c in candles[-22:])
            chandelier_stop = highest_high - atr * mult
            if current_price < chandelier_stop:
                return "chandelier_long", chandelier_stop
        else:
            lowest_low = min(c.low for c in candles[-22:])
            chandelier_stop = lowest_low + atr * mult
            if current_price > chandelier_stop:
                return "chandelier_short", chandelier_stop

        return None

    def on_exit(self, asset: str) -> None:
        """Set cooldown after exit"""
        self._cooldowns[asset] = self.cooldown_cycles

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
        """48h up-close ratio (NotebookLM round 6)"""
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
