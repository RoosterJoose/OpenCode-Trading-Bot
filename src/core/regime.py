"""
Regime detection module — Hurst + ADX + ATR percentile composite score.
Uses attribute-access Candle objects (candle.high, .low, .close).
"""
import math

class RegimeDetector:
    def __init__(self, atr_period: int = 14, adx_period: int = 14,
                 hurst_min_bars: int = 100, atr_lookback: int = 50):
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.hurst_min_bars = hurst_min_bars
        self.atr_lookback = atr_lookback

    def hurst_exponent(self, closes: list[float]) -> float:
        if len(closes) < self.hurst_min_bars:
            return 0.5
        n = len(closes)
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
        m = len(returns)
        mean_r = sum(returns) / m
        profile = [0.0]
        for r in returns:
            profile.append(profile[-1] + (r - mean_r))
        max_lag = min(64, m // 4)
        lags = [int(round(2 ** (i * 0.5))) for i in range(int(math.log2(max_lag)) * 2 + 1)]
        lags = [l for l in lags if l >= 4 and l <= max_lag]
        if len(lags) < 2:
            return 0.5
        flucts = []
        for lag in lags:
            segments = len(profile) // lag
            if segments < 1:
                continue
            f2_sum = 0.0
            for i in range(segments):
                seg = profile[i * lag:(i + 1) * lag]
                x_vals = list(range(lag))
                sx = sum(x_vals)
                sy = sum(seg)
                sxx = sum(x * x for x in x_vals)
                sxy = sum(x * y for x, y in zip(x_vals, seg))
                b = (lag * sxy - sx * sy) / max(lag * sxx - sx * sx, 1e-10)
                a = (sy - b * sx) / lag
                resid = sum((seg[j] - (a + b * x_vals[j])) ** 2 for j in range(lag))
                f2_sum += math.sqrt(resid / lag)
            flucts.append(f2_sum / segments)
        if len(flucts) < 2:
            return 0.5
        log_lags = [math.log(l) for l in lags]
        log_flu = [math.log(f) for f in flucts]
        n_pts = len(log_lags)
        sx = sum(log_lags)
        sy = sum(log_flu)
        sxx = sum(x * x for x in log_lags)
        sxy = sum(x * y for x, y in zip(log_lags, log_flu))
        slope = (n_pts * sxy - sx * sy) / max(n_pts * sxx - sx * sx, 1e-10)
        return max(0.0, min(1.0, slope))

    def _true_range(self, high: float, low: float, prev_close: float) -> float:
        return max(high - low, abs(high - prev_close), abs(low - prev_close))

    def _atr(self, candles: list) -> float:
        if len(candles) < self.atr_period + 1:
            return 0.0
        tr_sum = 0.0
        for i in range(-self.atr_period, 0):
            tr = self._true_range(candles[i].high, candles[i].low, candles[i - 1].close)
            tr_sum += tr
        return tr_sum / self.atr_period

    def _adx(self, candles: list) -> float:
        if len(candles) < self.adx_period + 2:
            return 25.0
        tr_sum, up_sum, down_sum = 0.0, 0.0, 0.0
        for i in range(-self.adx_period, 0):
            tr = self._true_range(candles[i].high, candles[i].low, candles[i - 1].close)
            tr_sum += tr
            up = candles[i].high - candles[i - 1].high
            down = candles[i - 1].low - candles[i].low
            up_sum += max(up, 0) if up > down else 0
            down_sum += max(down, 0) if down > up else 0
        if tr_sum == 0:
            return 25.0
        pdi = (up_sum / tr_sum) * 100
        ndi = (down_sum / tr_sum) * 100
        dx = abs(pdi - ndi) / max(pdi + ndi, 0.001) * 100
        return dx

    def atr_percentile(self, candles: list) -> float:
        if len(candles) < self.atr_lookback + self.atr_period:
            return 0.5
        current_atr = self._atr(candles)
        if current_atr <= 0:
            return 0.5
        atr_vals = []
        for i in range(self.atr_lookback + self.atr_period, len(candles) + 1):
            window = candles[i - self.atr_period:i]
            tr_sum = 0.0
            for j in range(1, self.atr_period):
                tr = self._true_range(window[j].high, window[j].low, window[j - 1].close)
                tr_sum += tr
            atr_vals.append(tr_sum / self.atr_period)
        if not atr_vals:
            return 0.5
        sorted_a = sorted(atr_vals)
        rank = sum(1 for a in sorted_a if a < current_atr)
        return rank / len(sorted_a)

    def score(self, asset: str, candles: list) -> tuple[int, float, float, float]:
        closes = [c.close for c in candles]
        hurst = self.hurst_exponent(closes)
        adx_val = self._adx(candles)
        atr_pct = self.atr_percentile(candles)
        score = 0
        if hurst > 0.55:
            score += 1
        if adx_val > 25:
            score += 1
        if atr_pct > 0.5:
            score += 1
        return score, hurst, adx_val, atr_pct

    def label(self, score: int) -> str:
        if score >= 2:
            return "TRENDING"
        elif score == 1:
            return "MIXED"
        return "RANGING"
