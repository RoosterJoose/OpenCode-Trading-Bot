"""
Trend-following strategy using Donchian 20-bar breakout for entry.
Replaced EMA9/21 crossover (was structurally too slow per NotebookLM round 10).

Entry: price breaks above 20-bar high (long) or below 20-bar low (short)
Exit:  Chandelier ATR trail, PSAR after 48h, EMA death/golden cross, funding drag
Regime gate: only enters in TRENDING / STRONGLY_TRENDING regimes
Cooldown: 30 cycles between trend entries
"""

from typing import Optional
from datetime import datetime, timezone
import logging

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy

logger = logging.getLogger(__name__)

# EMA cross exit uses fixed 9/21 (only for trend-reversal detection, not entry)
_FAST_EXIT_PERIOD = 9
_SLOW_EXIT_PERIOD = 21


class TrendFollow(PerpStrategy):
    def __init__(
        self,
        breakout_period: int = 20,
        atr_period: int = 22,
        atr_chandelier_mult_long_major: float = 4.0,
        atr_chandelier_mult_long_alt: float = 5.0,
        atr_chandelier_mult_short_major: float = 4.0,
        atr_chandelier_mult_short_alt: float = 5.0,
        psar_step: float = 0.015,
        psar_max_af: float = 0.18,
        psar_switch_hours: float = 48.0,
        min_volume_usd: float = 0,
        min_oi_usd: float = 5_000_000,
        cooldown_cycles: int = 30,
        majors: set | None = None,
        signal_tracker=None,
    ):
        self.breakout_period = breakout_period
        self.atr_period = atr_period
        self.atr_chandelier_mult_long_major = atr_chandelier_mult_long_major
        self.atr_chandelier_mult_long_alt = atr_chandelier_mult_long_alt
        self.atr_chandelier_mult_short_major = atr_chandelier_mult_short_major
        self.atr_chandelier_mult_short_alt = atr_chandelier_mult_short_alt
        self.min_volume_usd = min_volume_usd
        self.min_oi_usd = min_oi_usd
        self.psar_step = psar_step
        self.psar_max_af = psar_max_af
        self.psar_switch_hours = psar_switch_hours
        self.cooldown_cycles = cooldown_cycles
        self.majors = majors or {"BTC", "ETH"}
        self.signal_tracker = signal_tracker
        self._cooldowns: dict[str, int] = {}

    def name(self) -> str:
        return "trend"

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

        # Regime gate: only enter in trending regimes
        if regime not in (RegimeType.TRENDING, RegimeType.STRONGLY_TRENDING):
            logger.info("TREND %s: regime=%s", asset, regime.value)
            return None

        last = candles[-1]
        regime_str = regime.value
        logger.info("TREND %s: PASSED regime=%s — checking breakout", asset, regime_str)
        vol_min = self._get_threshold(asset, "volume_min_usd", self.min_volume_usd)
        if last.volume * last.close < vol_min:
            return None

        # OI proxy gate — check avg 24h volume as liquidity proxy
        if self.min_oi_usd > 0:
            avg_vol = sum(c.volume for c in candles[-24:]) / max(1, min(24, len(candles)))
            avg_pr = sum(c.close for c in candles[-24:]) / max(1, min(24, len(candles)))
            if avg_vol * avg_pr < self.min_oi_usd * 0.5:
                return None

        # Donchian channel: highest high / lowest low over breakout_period (excluding last bar)
        recent = candles[-(self.breakout_period + 1) : -1]
        if len(recent) < self.breakout_period:
            return None
        upper = max(c.high for c in recent)
        lower = min(c.low for c in recent)

        is_long_breakout = last.close > upper
        is_short_breakout = last.close < lower
        if not (is_long_breakout or is_short_breakout):
            logger.info("TREND %s: no breakout close=%.2f upper=%.2f lower=%.2f", asset, last.close, upper, lower)
            return None

        # Asset-specific drift regime
        drift = self._asset_drift(candles)
        is_long = is_long_breakout
        if drift == "bullish_drift" and not is_long:
            return None
        if drift == "bearish_drift" and is_long:
            return None

        # Price-vs-EMA50 divergence: if price diverges >3% from EMA50, only trade that direction
        ema50 = self._ema(candles, 50)
        if ema50 is not None and ema50 > 0:
            divergence = (last.close - ema50) / ema50
            if divergence > 0.03 and not is_long:
                return None
            if divergence < -0.03 and is_long:
                return None

        atr = self._atr(candles)
        entry_price = last.close

        # Breakout strength
        if is_long:
            breakout_pct = (last.close - upper) / upper
        else:
            breakout_pct = (lower - last.close) / lower

        # Confidence scoring
        confidence = 0.55
        sources = ["donchian_breakout", f"breakout_{breakout_pct:.3f}"]
        component_sources = ["local:donchian_breakout"]

        if breakout_pct > 0.005:
            confidence += 0.05
        if breakout_pct > 0.015:
            confidence += 0.05
        if regime == RegimeType.STRONGLY_TRENDING:
            confidence += 0.10
            sources.append("strong_trend")
            component_sources.append("local:strong_trend")

        # ADX as informational confirmation (not a gate)
        adx_val = self._adx(candles)
        if adx_val is not None and adx_val >= 25:
            sources.append(f"adx_{adx_val:.0f}")
            component_sources.append("local:adx_confirmed")
            confidence += 0.05


        # Kalshi OI surge confirmation
        oi_surge = any(s.asset == asset and s.source == "kalshi:oi_surge" for s in signals)
        if oi_surge:
            confidence = min(confidence * 1.05, 1.0)
            sources.append("kalshi_oi_surge")
            component_sources.append("kalshi:oi_surge")


        # Funding rate boost
        if is_long and funding_rate < -0.0005:
            confidence += 0.15
            sources.append("funding_tailwind")
        elif not is_long and funding_rate > 0.0005:
            confidence += 0.15
            sources.append("funding_tailwind")
        if funding_rate > 0.003:
            confidence += 0.1 if is_long else 0.05
            sources.append("funding_drag_boost" if is_long else "funding_crowded_long")
        if funding_rate < -0.003:
            confidence += 0.05 if is_long else 0.1
            sources.append("funding_neg_short_boost" if not is_long else "funding_discount_long")

        confidence = min(confidence, 1.0)
        side = Side.LONG if is_long else Side.SHORT

        return side, confidence, {
            "entry_price": entry_price,
            "donchian_upper": round(upper, 2),
            "donchian_lower": round(lower, 2),
            "breakout_pct": round(breakout_pct * 100, 3),
            "atr": round(atr, 4),
            "side": side.value,
            "sources": sources,
            "component_sources": component_sources,
        }

    # ── Asset-specific drift regime ──

    @staticmethod
    def _asset_drift(candles: list[PerpCandle]) -> str:
        if len(candles) < 50:
            return "neutral"
        closes = [c.close for c in candles[-48:]]
        if len(closes) < 48:
            return "neutral"

        # Deep oversold override
        long_closes = [c.close for c in candles[-48:]]
        if len(long_closes) >= 15:
            gains = losses = 0.0
            for i in range(-14, 0):
                d = long_closes[i] - long_closes[i-1]
                if d >= 0: gains += d
                else: losses -= d
            if losses > 0:
                rsi = 100 - 100 / (1 + gains/14 / (losses/14))
                if rsi < 22:
                    return "neutral"

        up_days = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        ratio = up_days / (len(closes) - 1)
        if ratio > 0.60:
            return "bullish_drift"
        if ratio < 0.40:
            return "bearish_drift"
        return "neutral"

    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, Optional[float]]]:
        is_short = position.side == Side.SHORT

        # Chandelier is the primary stop for trend strategy (volatility-adjusted)
        atr = self._atr(candles)
        if atr > 0:
            chandelier_mult = (
                self.atr_chandelier_mult_short_major if asset in self.majors else self.atr_chandelier_mult_short_alt
            ) if is_short else (
                self.atr_chandelier_mult_long_major if asset in self.majors else self.atr_chandelier_mult_long_alt
            )
            atr_dist = atr * chandelier_mult
            min_dist = 0.015 * current_price
            max_dist = 0.04 * current_price
            stop_dist = max(min_dist, min(max_dist, atr_dist))

            # Chandelier anchor: highest high / lowest low SINCE entry
            since_entry = [c for c in candles if c.timestamp >= position.entry_time.timestamp()]
            if not since_entry:
                since_entry = [candles[-1]]

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

        # Fallback if chandelier couldn't compute: use static stop
        sl = position.stop_loss or 0
        if sl > 0:
            if is_short:
                if current_price >= sl:
                    self._cooldowns[asset] = self.cooldown_cycles
                    return "stop_loss", current_price
            else:
                if current_price <= sl:
                    self._cooldowns[asset] = self.cooldown_cycles
                    return "stop_loss", current_price

        if atr <= 0:
            return None

        # Switch to PSAR after position has been open > N hours
        age_hours = (datetime.now(timezone.utc) - position.entry_time).total_seconds() / 3600
        if age_hours > self.psar_switch_hours:
            psar = self._psar(candles)
            if psar is not None:
                if is_short and current_price >= psar:
                    return "psar", current_price
                elif not is_short and current_price <= psar:
                    return "psar", current_price

        # EMA trend-reversal exit (only for exit, not entry)
        fast = self._ema(candles, _FAST_EXIT_PERIOD)
        slow = self._ema(candles, _SLOW_EXIT_PERIOD)
        if fast is not None and slow is not None:
            if is_short:
                if fast > slow:
                    return "ema_golden_cross", current_price
            else:
                if fast < slow:
                    return "ema_death_cross", current_price

        # Funding drag exit
        if funding_rate > 0.003:
            if not is_short:
                return "funding_drag", current_price
        elif funding_rate < -0.003:
            if is_short:
                return "funding_drag", current_price

        return None

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

    def _adx(self, candles: list[PerpCandle]) -> Optional[float]:
        if len(candles) < 33:
            return None
        tr_vals, plus_dm, minus_dm = [], [], []
        for i in range(-28, 0):
            h, l, ph, pl = candles[i].high, candles[i].low, candles[i - 1].high, candles[i - 1].low
            tr_vals.append(max(h - l, abs(h - pl), abs(l - ph)))
            up = h - ph
            down = pl - l
            plus_dm.append(max(up, 0) if up > down else 0)
            minus_dm.append(max(down, 0) if down > up else 0)
        if not tr_vals:
            return None
        atr_p = sum(tr_vals[-14:]) / 14
        if atr_p <= 0:
            return None
        pdi = (sum(plus_dm[-14:]) / 14) / atr_p * 100
        ndi = (sum(minus_dm[-14:]) / 14) / atr_p * 100
        dx = abs(pdi - ndi) / (pdi + ndi) * 100 if (pdi + ndi) > 0 else 0
        return dx

    def _psar(self, candles: list[PerpCandle]) -> Optional[float]:
        if len(candles) < 5:
            return None
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        sar = lows[0]
        ep = highs[0]
        af = self.psar_step
        is_up = True
        for i in range(1, len(candles)):
            if is_up:
                sar = sar + af * (ep - sar)
                sar = min(sar, lows[i - 1])
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + self.psar_step, self.psar_max_af)
                if lows[i] < sar:
                    is_up = False
                    sar = ep
                    ep = lows[i]
                    af = self.psar_step
            else:
                sar = sar - af * (sar - ep)
                sar = max(sar, highs[i - 1])
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + self.psar_step, self.psar_max_af)
                if highs[i] > sar:
                    is_up = True
                    sar = ep
                    ep = highs[i]
                    af = self.psar_step
        return sar
