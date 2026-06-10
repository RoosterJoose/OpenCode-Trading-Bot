"""
Trend-following strategy for perps — EMA crossover + ADX with perp-aware exits.

Only enters in trending regimes. Uses chandelier exit with ATR,
switching to Parabolic SAR for exits after 48h to lock in profits.
Cooldown between trend entries to avoid whipsaw.
"""

from typing import Optional
from datetime import datetime, timezone
import logging

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy

logger = logging.getLogger(__name__)


class TrendFollow(PerpStrategy):
    def __init__(
        self,
        fast_period: int = 9,
        slow_period: int = 21,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        atr_period: int = 22,
        atr_chandelier_mult_long_major: float = 3.0,
        atr_chandelier_mult_long_alt: float = 4.0,
        atr_chandelier_mult_short_major: float = 2.0,
        atr_chandelier_mult_short_alt: float = 2.5,
        psar_step: float = 0.015,
        psar_max_af: float = 0.18,
        psar_switch_hours: float = 48.0,
        min_volume_usd: float = 30_000_000,
        min_oi_usd: float = 5_000_000,
        cooldown_cycles: int = 30,
        majors: set | None = None,
        signal_tracker=None,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.atr_period = atr_period
        self.atr_chandelier_mult_long_major = atr_chandelier_mult_long_major
        self.atr_chandelier_mult_long_alt = atr_chandelier_mult_long_alt
        self.atr_chandelier_mult_short_major = atr_chandelier_mult_short_major
        self.atr_chandelier_mult_short_alt = atr_chandelier_mult_short_alt
        self.psar_step = psar_step
        self.psar_max_af = psar_max_af
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
        if len(candles) < self.slow_period + self.adx_period + 5:
            return None

        if regime not in (RegimeType.TRENDING, RegimeType.STRONGLY_TRENDING):
            logger.info("TREND %s: regime=%s", asset, regime.value)
            return None

        last = candles[-1]
        vol_min = self._get_threshold(asset, "volume_min_usd", self.min_volume_usd)
        if last.volume * last.close < vol_min:
            return None

        # OI proxy gate — check avg 24h volume as liquidity proxy
        if self.min_oi_usd > 0:
            avg_vol = sum(c.volume for c in candles[-24:]) / max(1, min(24, len(candles)))
            avg_pr = sum(c.close for c in candles[-24:]) / max(1, min(24, len(candles)))
            if avg_vol * avg_pr < self.min_oi_usd * 0.5:
                return None

        ema_fast = self._ema(candles, self.fast_period)
        ema_slow = self._ema(candles, self.slow_period)
        if ema_fast is None or ema_slow is None:
            return None

        prev_fast = self._ema(candles[:-1], self.fast_period)
        prev_slow = self._ema(candles[:-1], self.slow_period)
        if prev_fast is None or prev_slow is None:
            return None

        cross_above = prev_fast <= prev_slow and ema_fast > ema_slow
        cross_below = prev_fast >= prev_slow and ema_fast < ema_slow
        continuation_long = ema_fast > ema_slow and last.close > ema_fast
        continuation_short = ema_fast < ema_slow and last.close < ema_fast
        near_ema_pct = self._get_threshold(asset, "near_ema_pct", 0.04)
        near_ema = abs(last.close - ema_fast) / ema_fast <= near_ema_pct if ema_fast else False
        if not cross_above and not (continuation_long and near_ema):
            if not cross_below and not (continuation_short and near_ema):
                return None

        # Asset-specific drift regime (NotebookLM: >60% directional days)
        drift = self._asset_drift(candles)
        is_long_signal = cross_above or (continuation_long and near_ema)
        if drift == "bullish_drift" and not is_long_signal:
            return None
        if drift == "bearish_drift" and is_long_signal:
            return None

        # Price-vs-EMA50 divergence: if price diverges >3% from EMA50, only trade that direction
        ema50 = self._ema(candles, 50)
        if ema50 is not None and ema50 > 0:
            divergence = (last.close - ema50) / ema50
            if divergence > 0.03 and not is_long_signal:
                return None
            if divergence < -0.03 and is_long_signal:
                return None

        adx = self._adx(candles)
        adx_ok = adx is not None and adx >= self.adx_threshold
        hurst_override = regime == RegimeType.STRONGLY_TRENDING and (adx is None or adx < self.adx_threshold)
        if not adx_ok and not hurst_override:
            return None

        atr = self._atr(candles)
        entry_price = last.close

        is_long = cross_above or (continuation_long and near_ema)
        cross_type = "bull" if is_long else "bear"
        confidence = 0.5
        sources = [f"ema_{cross_type}_cross" if (cross_above if is_long else cross_below) else f"{cross_type}_continuation"]
        if adx_ok:
            sources.append("adx_confirmed")
        if hurst_override:
            sources.append("hurst_override")
        if regime == RegimeType.STRONGLY_TRENDING:
            confidence += 0.2
            sources.append("strong_trend")

        component_sources = [f"local:ema_{cross_type}_cross" if (cross_above if is_long else cross_below) else f"local:{cross_type}_continuation", "local:adx_confirmed"]

        # Altfins signal validation
        altfins_confirm = False
        for s in signals:
            if s.asset != asset:
                continue
            if is_long and s.direction != Side.LONG:
                continue
            if not is_long and s.direction != Side.SHORT:
                continue
            if s.source.startswith("altfins:"):
                source_l = s.source.lower()
                if any(kw in source_l for kw in ("momentum", "breakout", "uptrend", "downtrend", "cross", "trend", "channel_up", "channel_down")):
                    sig_weight = self.signal_tracker.weight(s.source) if self.signal_tracker else 0.5
                    has_history = self.signal_tracker.accuracy(s.source) is not None if self.signal_tracker else False
                    if sig_weight > 0 and has_history:
                        altfins_confirm = True
                        component_sources.append(s.source)
                        sources.append(s.source.replace("altfins:", "") + f"_{sig_weight:.2f}")

        if altfins_confirm:
            confidence = min(confidence * 1.2, 0.95)
            sources.append("altfins_validated")

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
            "fast_ema": round(ema_fast, 2),
            "slow_ema": round(ema_slow, 2),
            "adx": round(adx, 2) if adx is not None else None,
            "atr": round(atr, 4),
            "side": side.value,
            "sources": sources,
            "component_sources": component_sources,
        }

    # ── Asset-specific drift regime (NotebookLM: >60% directional days) ──

    @staticmethod
    def _asset_drift(candles: list[PerpCandle]) -> str:
        if len(candles) < 50:
            return "neutral"
        closes = [c.close for c in candles[-48:]]  # 2 days of 1h data
        if len(closes) < 48:
            return "neutral"

        # Deep oversold override: if RSI on multi-day window is < 22, allow contrarian entries
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

        atr = self._atr(candles)
        if atr <= 0:
            return None

        chandelier_mult = (
            self.atr_chandelier_mult_short_major if asset in self.majors else self.atr_chandelier_mult_short_alt
        ) if is_short else (
            self.atr_chandelier_mult_long_major if asset in self.majors else self.atr_chandelier_mult_long_alt
        )
        atr_dist = atr * chandelier_mult
        min_dist = 0.015 * current_price
        max_dist = 0.04 * current_price
        stop_dist = max(min_dist, min(max_dist, atr_dist))

        if is_short:
            chandelier = min(c.low for c in candles[-self.atr_period:]) + stop_dist
        else:
            chandelier = max(c.high for c in candles[-self.atr_period:]) - stop_dist

        # Switch to PSAR after position has been open > N hours
        age_hours = (datetime.now(timezone.utc) - position.entry_time).total_seconds() / 3600
        if age_hours > self.psar_switch_hours:
            psar = self._psar(candles)
            if psar is not None:
                if is_short and current_price >= psar:
                    return "psar", current_price
                elif not is_short and current_price <= psar:
                    return "psar", current_price

        if is_short:
            if current_price >= chandelier:
                self._cooldowns[asset] = self.cooldown_cycles
                return "chandelier", current_price
        else:
            if current_price <= chandelier:
                self._cooldowns[asset] = self.cooldown_cycles
                return "chandelier", current_price

        fast = self._ema(candles, self.fast_period)
        slow = self._ema(candles, self.slow_period)
        if fast is not None and slow is not None:
            if is_short:
                if fast > slow:
                    return "ema_golden_cross", current_price
            else:
                if fast < slow:
                    return "ema_death_cross", current_price

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
        if len(candles) < self.adx_period * 2 + 5:
            return None
        tr_vals, plus_dm, minus_dm = [], [], []
        for i in range(-self.adx_period * 2 + 1, 0):
            h, l, ph, pl = candles[i].high, candles[i].low, candles[i - 1].high, candles[i - 1].low
            tr_vals.append(max(h - l, abs(h - pl), abs(l - ph)))
            up = h - ph
            down = pl - l
            plus_dm.append(max(up, 0) if up > down else 0)
            minus_dm.append(max(down, 0) if down > up else 0)
        if not tr_vals:
            return None
        atr_p = sum(tr_vals[-self.adx_period:]) / self.adx_period
        if atr_p <= 0:
            return None
        pdi = (sum(plus_dm[-self.adx_period:]) / self.adx_period) / atr_p * 100
        ndi = (sum(minus_dm[-self.adx_period:]) / self.adx_period) / atr_p * 100
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
