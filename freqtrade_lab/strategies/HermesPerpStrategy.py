from datetime import datetime
import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy


class HermesPerpStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    startup_candle_count = 150
    minimal_roi = {}
    stoploss = -0.04
    use_custom_stoploss = True
    use_exit_signal = True
    process_only_new_candles = True

    mr_rsi_oversold = 28.0
    mr_rsi_period = 14
    mr_cooldown_bars = 12
    mr_min_volume_usd = 2_000_000
    mr_tp1_r_mult = 0.5
    mr_tp2_r_mult = 1.5
    mr_tp3_r_mult = 3.0
    mr_funding_halt = 0.005

    trend_fast_period = 9
    trend_slow_period = 21
    trend_adx_period = 14
    trend_adx_threshold = 25.0
    trend_atr_period = 22
    trend_chandelier_mult = 3.5
    trend_psar_step = 0.015
    trend_psar_max_af = 0.18
    trend_psar_switch_hours = 48.0
    trend_min_volume_usd = 5_000_000
    trend_cooldown_cycles = 60
    trend_near_ema_pct = 0.012
    trend_funding_drag = 0.003
    trend_funding_tailwind = -0.0005

    atr_stop_major = 2.0
    atr_stop_alt = 3.0
    stop_min_pct = 1.5
    stop_max_pct = 4.0
    atr_leverage_cap_pct = 3.0
    base_leverage = 2.0

    regime_hurst_trend = 0.55
    regime_hurst_mr = 0.45
    regime_er_trend = 0.60
    regime_er_mr = 0.30
    regime_high_vol = 0.03
    regime_dead_market = 0.0015

    confidence_min_entry = 0.70
    mr_base_conf = 0.5
    mr_deep_oversold_threshold = 20.0
    mr_deep_oversold_boost = 0.2
    mr_funding_boost = 0.15
    mr_funding_threshold = -0.001
    mr_regime_boost = 0.1
    trend_base_conf = 0.5
    trend_strong_regime_boost = 0.2
    trend_funding_boost = 0.1

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        high = dataframe["high"]
        low = dataframe["low"]
        volume = dataframe["volume"]

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        dataframe["atr"] = tr.rolling(14).mean()
        dataframe["atr_pct"] = dataframe["atr"] / close * 100

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        dataframe["rsi"] = 100 - (100 / (1 + rs))

        dataframe["ema_fast"] = close.ewm(span=9, adjust=False).mean()
        dataframe["ema_slow"] = close.ewm(span=21, adjust=False).mean()
        dataframe["ema_dist"] = (close - dataframe["ema_fast"]) / dataframe["ema_fast"]

        up = high.diff()
        down = -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        atr_adx = tr.rolling(14).mean()
        plus_di = 100 * pd.Series(plus_dm, index=dataframe.index).rolling(14).mean() / atr_adx.replace(0, np.nan)
        minus_di = 100 * pd.Series(minus_dm, index=dataframe.index).rolling(14).mean() / atr_adx.replace(0, np.nan)
        dataframe["adx"] = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).rolling(14).mean()

        dataframe["hurst"] = self._rolling_hurst(close, 100)

        direction = (close - close.shift(14)).abs()
        volatility = close.diff().abs().rolling(14).sum()
        dataframe["er"] = direction / volatility.replace(0, np.nan)

        norm_vol = dataframe["atr"] / close
        dataframe["regime_dead"] = norm_vol < self.regime_dead_market
        dataframe["regime_high_vol"] = norm_vol > self.regime_high_vol
        dataframe["regime_strong_trend"] = (dataframe["hurst"] > self.regime_hurst_trend) & (dataframe["er"] > self.regime_er_trend)
        dataframe["regime_trend"] = dataframe["hurst"] > self.regime_hurst_trend
        dataframe["regime_mr"] = (dataframe["hurst"] < self.regime_hurst_mr) & (dataframe["er"] < self.regime_er_mr)

        dataframe["volume_usd"] = volume * close

        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        dataframe["bb_lower"] = bb_mid - 2.0 * bb_std
        dataframe["bb_upper"] = bb_mid + 2.0 * bb_std

        mr_conf = pd.Series(self.mr_base_conf, index=dataframe.index)
        mr_conf += (dataframe["rsi"] <= self.mr_deep_oversold_threshold).astype(float) * self.mr_deep_oversold_boost
        mr_conf += dataframe["regime_mr"].astype(float) * self.mr_regime_boost
        mr_conf = mr_conf.clip(upper=1.0)
        dataframe["mr_confidence"] = mr_conf

        trend_conf = pd.Series(self.trend_base_conf, index=dataframe.index)
        trend_conf += dataframe["regime_strong_trend"].astype(float) * self.trend_strong_regime_boost
        trend_conf = trend_conf.clip(upper=1.0)
        dataframe["trend_confidence"] = trend_conf

        dataframe["hermes_confidence"] = pd.concat([trend_conf, mr_conf], axis=1).max(axis=1)
        dataframe["entry_kind"] = np.where(trend_conf >= mr_conf, "trend", "mr")

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        mr_entry = (
            (dataframe["volume_usd"] >= self.mr_min_volume_usd)
            & (~dataframe["regime_dead"])
            & (~dataframe["regime_high_vol"])
            & (~dataframe["regime_strong_trend"])
            & (dataframe["rsi"] <= self.mr_rsi_oversold)
            & (dataframe["mr_confidence"] >= self.confidence_min_entry)
        )

        ema_cross = (dataframe["ema_fast"].shift(1) <= dataframe["ema_slow"].shift(1)) & (dataframe["ema_fast"] > dataframe["ema_slow"])
        continuation = (dataframe["ema_fast"] > dataframe["ema_slow"]) & (dataframe["close"] > dataframe["ema_fast"])
        near_ema = dataframe["ema_dist"].between(0, self.trend_near_ema_pct)
        trend_setup = ema_cross | (continuation & near_ema)

        trend_entry = (
            (dataframe["volume_usd"] >= self.trend_min_volume_usd)
            & (~dataframe["regime_dead"])
            & (~dataframe["regime_high_vol"])
            & (dataframe["regime_trend"])
            & trend_setup
            & (dataframe["adx"] >= self.trend_adx_threshold)
            & (dataframe["trend_confidence"] >= self.confidence_min_entry)
        )

        dataframe.loc[mr_entry, ["enter_long", "enter_tag"]] = (1, "mr_rsi_oversold")
        dataframe.loc[trend_entry, ["enter_long", "enter_tag"]] = (1, "trend_ema_adx")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["volume"] > 0) & (dataframe["rsi"] > 72),
            ["exit_long", "exit_tag"],
        ] = (1, "rsi_overheated")
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs) -> str | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None
        row = dataframe.iloc[-1]

        if trade.enter_tag is not None and trade.enter_tag.startswith("mr"):
            if trade.stop_loss and current_rate <= trade.stop_loss:
                return "stop_loss"
            entry = trade.open_rate
            stop = trade.stop_loss or (entry * 0.985)
            risk_r = (entry - stop) / entry if stop < entry else 0.01
            r_mult = (current_rate - entry) / max(entry - stop, 0.001) if risk_r > 0 else 0
            if r_mult >= self.mr_tp3_r_mult:
                return "mr_tp3"
            if r_mult >= self.mr_tp2_r_mult:
                return "mr_tp2"
            if r_mult >= self.mr_tp1_r_mult:
                return "mr_tp1"

        if trade.enter_tag is not None and trade.enter_tag.startswith("trend"):
            if trade.stop_loss and current_rate <= trade.stop_loss:
                return "stop_loss"

            atr_val = float(row.get("atr", 0))
            if atr_val > 0:
                atr_dist = atr_val * self.trend_chandelier_mult
                min_dist = self.stop_min_pct / 100 * current_rate
                max_dist = self.stop_max_pct / 100 * current_rate
                stop_dist = max(min_dist, min(max_dist, atr_dist))

                highs = dataframe["high"].iloc[-self.trend_atr_period:]
                chandelier = highs.max() - stop_dist
                if current_rate <= chandelier:
                    return "chandelier"

                age_hours = (current_time - trade.open_date_utc).total_seconds() / 3600
                if age_hours > self.trend_psar_switch_hours:
                    psar = self._psar(dataframe)
                    if psar is not None and current_rate <= psar:
                        return "psar"

            fast = row.get("ema_fast", 0)
            slow = row.get("ema_slow", 0)
            if fast > 0 and slow > 0 and fast < slow:
                return "ema_death_cross"

        return None

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool,
                        **kwargs) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return -0.04
        atr_pct = float(dataframe.iloc[-1].get("atr_pct", 0) or 0)
        mult = self.atr_stop_major if pair.startswith(("BTC/", "ETH/")) else self.atr_stop_alt
        stop_pct = max(self.stop_min_pct, min(atr_pct * mult, self.stop_max_pct))
        return -stop_pct / 100

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        atr_pct = 0.0 if dataframe.empty else float(dataframe.iloc[-1].get("atr_pct", 0) or 0)
        if atr_pct > self.atr_leverage_cap_pct:
            return 1.0
        return min(self.base_leverage, max_leverage)

    @staticmethod
    def _rolling_hurst(series: pd.Series, window: int) -> pd.Series:
        values = series.to_numpy(dtype=float)
        out = np.full(len(values), np.nan)
        for idx in range(window, len(values)):
            out[idx] = HermesPerpStrategy._hurst(values[idx - window:idx])
        return pd.Series(out, index=series.index).fillna(0.5)

    @staticmethod
    def _hurst(values: np.ndarray) -> float:
        n = len(values)
        if n < 10:
            return 0.5
        max_lag = min(n // 2, 100)
        log_lags = []
        log_tau = []
        for lag in range(2, max_lag):
            diffs = values[lag:] - values[:-lag]
            if len(diffs) == 0:
                continue
            var = np.mean(diffs ** 2)
            if var <= 0:
                continue
            std = np.sqrt(var)
            log_lags.append(np.log(lag))
            log_tau.append(np.log(std))
        if len(log_lags) < 3:
            return 0.5
        n_pts = len(log_lags)
        sum_x = sum(log_lags)
        sum_y = sum(log_tau)
        sum_xy = sum(x * y for x, y in zip(log_lags, log_tau))
        sum_xx = sum(x * x for x in log_lags)
        denom = n_pts * sum_xx - sum_x * sum_x
        if denom == 0:
            return 0.5
        slope = (n_pts * sum_xy - sum_x * sum_y) / denom
        return max(0.0, min(1.0, slope / 2))

    @staticmethod
    def _psar(dataframe: DataFrame) -> float | None:
        highs = dataframe["high"].values[-50:]
        lows = dataframe["low"].values[-50:]
        if len(highs) < 5:
            return None
        step = 0.015
        max_af = 0.18
        sar = lows[0]
        ep = highs[0]
        af = step
        is_up = True
        for i in range(1, len(highs)):
            if is_up:
                sar = sar + af * (ep - sar)
                sar = min(sar, lows[i - 1])
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + step, max_af)
                if lows[i] < sar:
                    is_up = False
                    sar = ep
                    ep = lows[i]
                    af = step
            else:
                sar = sar - af * (sar - ep)
                sar = max(sar, highs[i - 1])
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + step, max_af)
                if highs[i] > sar:
                    is_up = True
                    sar = ep
                    ep = highs[i]
                    af = step
        return sar if is_up else None
