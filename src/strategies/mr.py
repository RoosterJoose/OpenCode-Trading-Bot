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
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy


class MeanReversion(PerpStrategy):
    def __init__(
        self,
        rsi_oversold: float = 40.0,
        rsi_period: int = 14,
        atr_period: int = 14,
        cooldown_bars: int = 12,
        min_volume_usd: float = 0,
        tp1_r_mult: float = 0.5,
        tp2_r_mult: float = 1.5,
        tp3_r_mult: float = 3.0,
        max_hold_hours: float = 48.0,
        majors: set | None = None,
        signal_tracker=None,
    ):
        self.rsi_oversold = rsi_oversold
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.cooldown_bars = cooldown_bars
        self.min_volume_usd = min_volume_usd
        self.tp1_r_mult = tp1_r_mult
        self.tp2_r_mult = tp2_r_mult
        self.tp3_r_mult = tp3_r_mult
        self.max_hold_hours = max_hold_hours
        self.majors = majors or {"BTC", "ETH"}
        self.signal_tracker = signal_tracker
        self._cooldowns: dict[str, int] = {}
        self._scaled_out: dict[str, bool] = {}
        self._rsi_history: dict[str, list[float]] = defaultdict(lambda: [])

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
        vol_min = self._get_threshold(asset, "volume_min_usd", self.min_volume_usd)
        if last.volume * last.close < vol_min:
            return None

        if regime in (RegimeType.STRONGLY_TRENDING, RegimeType.TRENDING, RegimeType.HIGH_VOL):
            return None

        # Microstructure filters (NotebookLM: Hurst stability, VR test, autocorrelation)
        if len(candles) >= 200:
            if not self._microstructure_ok(candles):
                return None

        rsi = self._rsi(candles)
        if rsi is not None:
            history = self._rsi_history[asset]
            history.append(rsi)
            if len(history) > 336:
                history.pop(0)

        oversold_th, overbought_th = self._dynamic_rsi_thresholds(asset)
        is_oversold = rsi is not None and rsi <= oversold_th
        is_overbought = rsi is not None and rsi >= overbought_th

        # NotebookLM round 10: Stochastic RSI hits 0/100 even in slow-bleed regimes
        stoch_k = self._stoch_rsi(candles)
        is_stoch_oversold = stoch_k is not None and stoch_k <= 0.20
        is_stoch_overbought = stoch_k is not None and stoch_k >= 0.80

        # Bollinger Band touch — adaptive to current volatility
        bb_pos = self._bb_touch(candles)
        is_bb_oversold = bb_pos is not None and bb_pos <= -0.90
        is_bb_overbought = bb_pos is not None and bb_pos >= 0.90

        is_oversold = is_oversold or is_stoch_oversold or is_bb_oversold
        is_overbought = is_overbought or is_stoch_overbought or is_bb_overbought
        if not is_oversold and not is_overbought:
            return None
        is_long = is_oversold

        # Asset-specific drift regime (NotebookLM: >60% directional days)
        drift = self._asset_drift(candles)
        if drift == "bullish_drift" and not is_long:
            return None
        if drift == "bearish_drift" and is_long:
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

        # Structural stop anchor (NotebookLM): place initial stop at 5-bar swing low/high
        if len(candles) >= 5:
            if is_long:
                swing_low = min(c.low for c in candles[-5:])
                stop = min(swing_low, entry_price * (1 - stop_pct))
            else:
                swing_high = max(c.high for c in candles[-5:])
                stop = max(swing_high, entry_price * (1 + stop_pct))
        else:
            if is_long:
                stop = entry_price * (1 - stop_pct)
            else:
                stop = entry_price * (1 + stop_pct)

        # Clamp stop distance: never closer than stop_min, never farther than stop_max
        if is_long:
            stop = min(stop, entry_price * (1 - stop_min / 100))
            stop = max(stop, entry_price * (1 - stop_max / 100))
            risk_r = (entry_price - stop) / entry_price
        else:
            stop = max(stop, entry_price * (1 + stop_min / 100))
            stop = min(stop, entry_price * (1 + stop_max / 100))
            risk_r = (stop - entry_price) / entry_price

        if risk_r <= 0:
            return None

        tag = "rsi_oversold" if is_long else "rsi_overbought"
        sources = [tag]
        confidence = 0.5
        if rsi is not None:
            if is_long:
                rsi_score = max(0.0, min(1.0, (50.0 - rsi) / (50.0 - oversold_th)))
            else:
                rsi_score = max(0.0, min(1.0, (rsi - 50.0) / (overbought_th - 50.0)))
            smooth_contribution = rsi_score * 0.22
            confidence += smooth_contribution
            sources.append(f"rsi_contrib_{smooth_contribution:.2f}")

        # NotebookLM round 10: confidence boost from stochastic RSI / BB touch
        if is_stoch_oversold or is_stoch_overbought:
            sources.append("stoch_rsi_extreme")
            confidence += 0.05
        if is_bb_oversold or is_bb_overbought:
            sources.append("bb_touch")
            confidence += 0.05

        component_sources = [f"local:{tag}"]



        if is_long and funding_rate < -self.funding_threshold:
            confidence += 0.15
            sources.append("funding_support")
        elif not is_long and funding_rate > self.funding_threshold:
            confidence += 0.15
            sources.append("funding_support")

        if regime in (RegimeType.MEAN_REVERTING, RegimeType.STRONGLY_MR):
            confidence += 0.1
            sources.append("regime_mr")

        confidence = min(confidence, 1.0)

        side = Side.LONG if is_long else Side.SHORT

        return side, confidence, {
            "entry_price": entry_price,
            "stop_loss": stop,
            "risk_r": round(risk_r, 4),
            "rsi": round(rsi, 2) if rsi is not None else None,
            "atr_pct": round(stop_pct * 100, 2),
            "sources": sources,
            "component_sources": component_sources,
        }

    def _sma(self, candles: list, period: int = 20) -> Optional[float]:
        if len(candles) < period:
            return None
        return sum(c.close for c in candles[-period:]) / period

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
            if is_short and current_price >= sl:
                self._cooldowns[asset] = self.cooldown_bars
                return "stop_loss", current_price
            elif not is_short and current_price <= sl:
                self._cooldowns[asset] = self.cooldown_bars
                return "stop_loss", current_price

        if candles and position.entry_time:
            now = datetime.now() if position.entry_time.tzinfo is None else datetime.now(timezone.utc)
            age_hours = (now - position.entry_time).total_seconds() / 3600
            if age_hours > self.max_hold_hours:
                self._cooldowns[asset] = self.cooldown_bars
                return "time_exit", current_price

        entry = position.entry_price
        if entry <= 0:
            return None

        stop_dist = abs(entry - sl) if sl > 0 else entry * 0.02
        if is_short:
            r_mult = (entry - current_price) / max(stop_dist, 0.001)
        else:
            r_mult = (current_price - entry) / max(stop_dist, 0.001)

        # NotebookLM scale-out: dynamic MA-based exit, then 2.0R trailing
        if not self._scaled_out.get(asset, False):
            sma_20 = self._sma(candles)
            if sma_20 is not None:
                if (not is_short and current_price >= sma_20) or (is_short and current_price <= sma_20):
                    self._scaled_out[asset] = True
                    return "tp1", current_price
            # Fallback: if no SMA data, use fixed R-multiple
            if r_mult >= self.tp1_r_mult:
                self._scaled_out[asset] = True
                return "tp1", current_price
        else:
            # After TP1: chandelier trailing stop replaces fixed stop
            atr = self._atr(candles)
            if atr > 0:
                chandelier_mult = 3.0
                atr_dist = atr * chandelier_mult
                min_dist = 0.015 * current_price
                max_dist = 0.04 * current_price
                stop_dist = max(min_dist, min(max_dist, atr_dist))
                since_entry = [c for c in candles if isinstance(c.timestamp, (int, float)) or hasattr(c, 'timestamp')]
                if isinstance(candles[0].timestamp, (int, float)):
                    since_entry = [c for c in candles if c.timestamp >= position.entry_time.timestamp()]
                if not since_entry:
                    since_entry = [candles[-1]]
                if is_short:
                    anchor = min(c.low for c in since_entry)
                    chandelier = anchor + stop_dist
                    if current_price >= chandelier:
                        self._cooldowns[asset] = self.cooldown_bars
                        return "chandelier", current_price
                else:
                    anchor = max(c.high for c in since_entry)
                    chandelier = anchor - stop_dist
                    if current_price <= chandelier:
                        self._cooldowns[asset] = self.cooldown_bars
                        return "chandelier", current_price
            if r_mult >= self.tp2_r_mult:
                self._cooldowns[asset] = self.cooldown_bars
                return "tp2", current_price

        if funding_rate > self.funding_halt_threshold:
            if not is_short:
                self._cooldowns[asset] = self.cooldown_bars
                return "funding_spike", current_price
        elif funding_rate < -self.funding_halt_threshold:
            if is_short:
                self._cooldowns[asset] = self.cooldown_bars
                return "funding_spike", current_price

        return None

    def on_exit(self, asset: str) -> None:
        self._cooldowns[asset] = self.cooldown_bars

    # ── Dynamic RSI thresholds (NotebookLM: per-asset rolling percentiles) ──

    def _dynamic_rsi_thresholds(self, asset: str) -> tuple[float, float]:
        history = self._rsi_history.get(asset, [])
        if len(history) < 20:
            return self.rsi_oversold, 100 - self.rsi_oversold
        sorted_vals = sorted(history)
        n = len(sorted_vals)
        p5 = sorted_vals[int(n * 0.05)]
        p95 = sorted_vals[int(n * 0.95)]
        oversold = max(15.0, min(35.0, p5))
        overbought = min(85.0, max(65.0, p95))
        return oversold, overbought

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

    @staticmethod
    def _stoch_rsi(candles: list[PerpCandle], rsi_period: int = 14, stoch_period: int = 14) -> Optional[float]:
        """Stochastic RSI: normalize RSI to its own trailing range (0-1).
        Hits 0/100 extremes even in slow-bleed regimes where RSI stays mid-range."""
        needed = rsi_period + stoch_period + 1
        if len(candles) < needed:
            return None
        closes = [c.close for c in candles]

        # Compute RSI for each window in the trailing stoch_period + 1 positions
        rsi_values: list[float] = []
        for k in range(stoch_period + 1):
            start = -(needed - k)
            end = start + rsi_period
            if start < 0 and end <= 0:
                g_sum, l_sum = 0.0, 0.0
                for j in range(start, end):
                    if abs(j + 1) < len(closes) and abs(j) < len(closes):
                        d = closes[j + 1] - closes[j]
                        g_sum += max(d, 0.0)
                        l_sum += max(-d, 0.0)
                if g_sum + l_sum == 0:
                    rsi_values.append(50.0)
                elif l_sum == 0:
                    rsi_values.append(100.0)
                else:
                    rs = g_sum / l_sum
                    rsi_values.append(100.0 - 100.0 / (1.0 + rs))

        if len(rsi_values) < 2:
            return None
        min_rsi = min(rsi_values)
        max_rsi = max(rsi_values)
        if max_rsi - min_rsi < 0.1:
            return None  # No range — can't compute
        latest_rsi = rsi_values[-1]
        stoch = (latest_rsi - min_rsi) / (max_rsi - min_rsi)
        return round(stoch, 4)

    @staticmethod
    def _bb_touch(candles: list[PerpCandle], period: int = 20, std_mult: float = 2.0) -> Optional[float]:
        """Bollinger Band position. Returns -1.5 to +1.5: -1 = lower band, 0 = mid, +1 = upper."""
        if len(candles) < period:
            return None
        closes = [c.close for c in candles[-period:]]
        mid = sum(closes) / period
        variance = sum((c - mid) ** 2 for c in closes) / period
        sd = variance ** 0.5
        if sd == 0:
            return 0.0
        upper = mid + std_mult * sd
        lower = mid - std_mult * sd
        last = closes[-1]
        if upper == lower:
            return 0.0
        pos = (last - mid) / (upper - mid)
        return max(-1.5, min(1.5, pos))

    def _atr(self, candles: list[PerpCandle]) -> float:
        if len(candles) < self.atr_period + 1:
            return 0.0
        trs = []
        for i in range(-self.atr_period, 0):
            h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0

    # ── Microstructure filters (NotebookLM: Hurst stability, VR test, autocorrelation) ──

    def _hurst(self, closes: list[float], lag: int = 10) -> float:
        n = len(closes)
        if n < lag * 2:
            return 0.5
        ts = [math.log(closes[i] / closes[i-1]) for i in range(1, n) if closes[i-1] > 0]
        if len(ts) < lag * 2:
            return 0.5
        lags = range(2, lag + 1)
        n_ts = len(ts)
        tau = [math.sqrt(sum((ts[t] - ts[t - lag]) ** 2 / (n_ts - lag) for t in range(lag, n_ts)) if n_ts > lag else 1) for lag in lags]
        if any(t <= 0 for t in tau):
            return 0.5
        log_rs = [math.log(t) for t in tau]
        log_lags = [math.log(l) for l in lags]
        n_l = len(log_lags)
        sx = sum(log_lags)
        sy = sum(log_rs)
        sxx = sum(x * x for x in log_lags)
        sxy = sum(x * y for x, y in zip(log_lags, log_rs))
        slope = (n_l * sxy - sx * sy) / (n_l * sxx - sx * sx) if (n_l * sxx - sx * sx) != 0 else 0
        return slope

    def _microstructure_ok(self, candles: list[PerpCandle]) -> bool:
        closes = [c.close for c in candles]
        n = len(closes)
        if n < 200:
            return True

        # 1. Hurst stability: compute H on 5 overlapping sub-windows of 200 bars
        h_vals = []
        step = max(1, (n - 200) // 5)
        for i in range(5):
            start = min(i * step, n - 200)
            sub = closes[start:start + 200]
            h_vals.append(self._hurst(sub))
        if len(h_vals) >= 3:
            mean_h = sum(h_vals) / len(h_vals)
            variance = sum((h - mean_h) ** 2 for h in h_vals) / len(h_vals)
            h_std = math.sqrt(variance)
            if h_std > 0.08:
                return False

        # 2. Variance Ratio test VR(2) and VR(5) both < 1.0
        returns = [math.log(closes[i] / closes[i-1]) for i in range(1, n) if closes[i-1] > 0]
        if len(returns) >= 10:
            n_ret = len(returns)
            var_1 = sum((r - sum(returns) / n_ret) ** 2 for r in returns) / n_ret if n_ret > 0 else 0
            for q in (2, 5):
                if len(returns) >= q * 2:
                    agg = [sum(returns[i - q:i]) for i in range(q, len(returns) + 1, q)]
                    n_agg = len(agg)
                    if n_agg > 1:
                        var_q = sum((a - sum(agg) / n_agg) ** 2 for a in agg) / n_agg if n_agg > 0 else 0
                        vr = var_q / (q * var_1) if var_1 > 0 else 1.0
                        if vr >= 1.0:
                            return False

        # 3. Targeted autocorrelation: require > 0.05 at dominant lag (1-5)
        if len(returns) >= 10:
            n_ret = len(returns)
            mean_r = sum(returns) / n_ret
            var_r = sum((r - mean_r) ** 2 for r in returns) / n_ret if n_ret > 0 else 0
            if var_r > 0:
                max_ac = 0.0
                for lag in range(1, 6):
                    cov = sum((returns[i] - mean_r) * (returns[i - lag] - mean_r) for i in range(lag, n_ret)) / (n_ret - lag) if n_ret > lag else 0
                    ac = cov / var_r if var_r > 0 else 0
                    max_ac = max(max_ac, abs(ac))
                if max_ac <= 0.05:
                    return False

        return True

    atr_stop_major = 2.0
    atr_stop_alt = 3.0

    # ── Asset-specific drift regime (NotebookLM) ──

    @staticmethod
    def _asset_drift(candles: list[PerpCandle]) -> str:
        if len(candles) < 50:
            return "neutral"
        closes = [c.close for c in candles[-48:]]
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

        up = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        r = up / (len(closes) - 1)
        if r > 0.60:
            return "bullish_drift"
        if r < 0.40:
            return "bearish_drift"
        return "neutral"

    funding_threshold = 0.001
    funding_halt_threshold = 0.01  # mirrors PerpRisk extreme_funding_threshold
