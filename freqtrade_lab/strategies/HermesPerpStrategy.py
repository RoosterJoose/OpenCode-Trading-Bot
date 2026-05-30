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
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        high = dataframe["high"]
        low = dataframe["low"]
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

        dataframe["bb_mid"] = close.rolling(20).mean()
        dataframe["bb_std"] = close.rolling(20).std()
        dataframe["bb_lower"] = dataframe["bb_mid"] - 2.0 * dataframe["bb_std"]
        dataframe["volume_usd"] = dataframe["volume"] * close

        direction = (close - close.shift(14)).abs()
        volatility = close.diff().abs().rolling(14).sum()
        dataframe["er"] = direction / volatility.replace(0, np.nan)
        dataframe["hurst"] = self._rolling_hurst(close, 100)

        dataframe["dead_market"] = dataframe["atr_pct"] < 0.15
        dataframe["high_vol"] = dataframe["atr_pct"] > 3.0
        dataframe["strong_trend"] = (dataframe["hurst"] > 0.55) & (dataframe["er"] > 0.60)
        dataframe["mean_reverting"] = (dataframe["hurst"] < 0.45) & (dataframe["er"] < 0.30)

        trend_score = (
            0.20
            + (dataframe["ema_fast"] > dataframe["ema_slow"]).astype(float) * 0.15
            + (dataframe["adx"] >= 25).astype(float) * 0.15
            + (dataframe["rsi"].between(48, 65)).astype(float) * 0.15
            + (dataframe["ema_dist"].between(0, 0.015)).astype(float) * 0.15
            + (dataframe["volume_usd"] >= 5_000_000).astype(float) * 0.10
            + (~dataframe["mean_reverting"]).astype(float) * 0.10
        )
        mr_score = (
            0.20
            + (dataframe["rsi"] <= 28).astype(float) * 0.20
            + (close <= dataframe["bb_lower"]).astype(float) * 0.20
            + (dataframe["volume_usd"] >= 2_000_000).astype(float) * 0.10
            + (~dataframe["high_vol"]).astype(float) * 0.10
            + (dataframe["er"] < 0.60).astype(float) * 0.10
            + (dataframe["hurst"] < 0.55).astype(float) * 0.10
        )
        dataframe["hermes_confidence"] = pd.concat([trend_score, mr_score], axis=1).max(axis=1).clip(0, 1)
        dataframe["entry_kind"] = np.where(trend_score >= mr_score, "trend", "mr")

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        trend_entry = (
            (dataframe["volume_usd"] >= 5_000_000)
            & (~dataframe["dead_market"])
            & (~dataframe["high_vol"])
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["adx"] >= 25)
            & dataframe["rsi"].between(48, 65)
            & dataframe["ema_dist"].between(0, 0.015)
            & (~dataframe["mean_reverting"])
            & (dataframe["hermes_confidence"] >= 0.70)
        )
        mr_entry = (
            (dataframe["volume_usd"] >= 2_000_000)
            & (~dataframe["dead_market"])
            & (~dataframe["high_vol"])
            & (~dataframe["strong_trend"])
            & (dataframe["rsi"] <= 28)
            & (dataframe["close"] <= dataframe["bb_lower"])
            & (dataframe["er"] < 0.60)
            & (dataframe["hermes_confidence"] >= 0.70)
        )
        dataframe.loc[trend_entry, ["enter_long", "enter_tag"]] = (1, "trend_hurst_er_adx")
        dataframe.loc[mr_entry, ["enter_long", "enter_tag"]] = (1, "mr_rsi_bb_revert")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["volume"] > 0)
            & (dataframe["rsi"] > 72),
            ["exit_long", "exit_tag"],
        ] = (1, "long_signal_exit")
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None
        row = dataframe.iloc[-1]
        if trade.enter_tag == "mr_rsi_bb_revert" and current_profit > 0 and row.get("rsi", 0) >= 50:
            return "mr_mean_reverted"
        if trade.enter_tag == "trend_hurst_er_adx":
            if row.get("ema_fast", 0) < row.get("ema_slow", 0):
                return "trend_ema_break"
            if row.get("rsi", 0) > 72:
                return "trend_overheated"
        return None

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return -0.04
        atr_pct = float(dataframe.iloc[-1].get("atr_pct", 0) or 0)
        mult = 2.0 if pair.startswith(("BTC/", "ETH/")) else 3.0
        stop_pct = max(1.5, min(atr_pct * mult, 4.0))
        return -stop_pct / 100

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        atr_pct = 0.0 if dataframe.empty else float(dataframe.iloc[-1].get("atr_pct", 0) or 0)
        if atr_pct > 3.0:
            return 1.0
        return min(2.0, max_leverage)

    @staticmethod
    def _rolling_hurst(series: pd.Series, window: int) -> pd.Series:
        values = series.to_numpy(dtype=float)
        out = np.full(len(values), np.nan)
        for idx in range(window, len(values)):
            window_values = values[idx - window:idx]
            out[idx] = HermesPerpStrategy._hurst(window_values)
        return pd.Series(out, index=series.index).fillna(0.5)

    @staticmethod
    def _hurst(values: np.ndarray) -> float:
        lags = range(2, min(len(values) // 2, 40))
        tau = []
        for lag in lags:
            diff = values[lag:] - values[:-lag]
            std = np.std(diff)
            if std > 0:
                tau.append((lag, std))
        if len(tau) < 2:
            return 0.5
        x = np.log([item[0] for item in tau])
        y = np.log([item[1] for item in tau])
        slope = np.polyfit(x, y, 1)[0]
        return float(max(0.0, min(1.0, slope)))
