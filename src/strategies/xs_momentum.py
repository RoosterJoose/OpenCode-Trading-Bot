"""
Cross-sectional momentum strategy — long top performers, short bottom.

NotebookLM round 10: cross-sectional momentum performs well in crypto when combined
with drift regime gates. Instead of trading isolated signals, rank all assets by recent
return and trade the strongest against the weakest.

Entry conditions:
- Asset is in top 3 (long) or bottom 3 (short) by 7-day return
- Drift filter aligns with direction (bullish_drift for longs, bearish_drift for shorts)
- RSI not at extreme (avoid chasing blowoffs / catching falling knives)
- Funding rate not at extreme (avoid crowded trades)

Exit: 5% profit target or 3% stop loss (simple; momentum sleeve shouldn't overstay)
"""

from typing import Optional
from datetime import datetime, timezone
import logging

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy

logger = logging.getLogger(__name__)


class CrossSectionalMomentum(PerpStrategy):
    # Class-level state shared across instances to enable ranking
    _downtrend_override: bool = False
    _asset_returns_7d: dict[str, float] = {}  # {asset: 7d return}

    def __init__(
        self,
        lookback_candles: int = 168,  # 7d × 24h
        top_n: int = 3,
        bottom_n: int = 3,
        min_volume_usd: float = 0,
        cooldown_cycles: int = 60,
        profit_target_pct: float = 5.0,
        stop_loss_pct: float = 3.0,
        majors: set | None = None,
        signal_tracker=None,
    ):
        self.lookback_candles = lookback_candles
        self.top_n = top_n
        self.bottom_n = bottom_n
        self.min_volume_usd = min_volume_usd
        self.cooldown_cycles = cooldown_cycles
        self.profit_target_pct = profit_target_pct
        self.stop_loss_pct = stop_loss_pct
        self.majors = majors or {"BTC", "ETH"}
        self.signal_tracker = signal_tracker
        self._cooldowns: dict[str, int] = {}

    def name(self) -> str:
        return "xs_momentum"

    @classmethod
    def set_returns(cls, returns: dict[str, float]) -> None:
        cls._asset_returns_7d = returns

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

        if len(candles) < self.lookback_candles + 5:
            return None

        last = candles[-1]
        vol_min = self._get_threshold(asset, "volume_min_usd", self.min_volume_usd)
        if last.volume * last.close < vol_min:
            return None

        # Check if this asset is in top_n or bottom_n
        ret_7d = self._asset_returns_7d.get(asset, 0.0)
        if not self._asset_returns_7d:
            return None

        sorted_assets = sorted(
            self._asset_returns_7d.items(), key=lambda x: x[1], reverse=True
        )
        top_assets = [a for a, _ in sorted_assets[: self.top_n]]
        bottom_assets = [a for a, _ in sorted_assets[-self.bottom_n :]]

        is_long_pick = asset in top_assets
        is_short_pick = asset in bottom_assets
        if not (is_long_pick or is_short_pick):
            return None

        # Verify return is meaningful (avoid noise)
        if is_long_pick and ret_7d < 0.01:  # <1% over 7d
            return None
        if is_short_pick and ret_7d > -0.01:  # >-1% over 7d (less than 1% drop)
            return None

        logger.info("XS_MOMENTUM %s: side=%s ret_7d=%.2f%%",
                    asset, "LONG" if is_long_pick else "SHORT", ret_7d * 100)

        is_long = is_long_pick

        # Drift filter
        drift = self._asset_drift(candles)
        if is_long and drift == "bearish_drift":
            return None
        if not is_long and drift == "bullish_drift" and not CrossSectionalMomentum._downtrend_override:
            return None

        # RSI check
        rsi = self._rsi(candles, 14)
        if is_long and rsi > 75:
            return None
        if not is_long and rsi < 25 and not CrossSectionalMomentum._downtrend_override:
            return None

        # EMA50 divergence
        ema50 = self._ema(candles, 50)
        if ema50 is not None and ema50 > 0:
            divergence = (last.close - ema50) / ema50
            if divergence > 0.05 and not is_long:
                    return None
            if divergence < -0.05 and is_long:
                return None

        # Funding extreme — avoid crowded trades
        if is_long and funding_rate > 0.001:  # +0.1% funding = crowded long
            return None
        if not is_long and funding_rate < -0.001:  # -0.1% funding = crowded short
            return None

        # Confidence: scale by rank extremity and return magnitude
        rank = top_assets.index(asset) + 1 if is_long else bottom_assets.index(asset) + 1
        rank_factor = 1.0 - (rank - 1) * 0.1  # rank 1 = 1.0, rank 3 = 0.8
        magnitude_factor = min(abs(ret_7d) / 0.05, 2.0)  # 5% move = 1.0, 10% = 2.0
        confidence = 0.55 * rank_factor * magnitude_factor
        confidence = min(max(confidence, 0.50), 0.90)

        side = Side.LONG if is_long else Side.SHORT
        entry_price = last.close

        confidence = self.blend_altfins_confidence(confidence, signals)
        return side, confidence, {
            "entry_price": entry_price,
            "ret_7d": round(ret_7d * 100, 2),
            "rank": rank,
            "top_n": self.top_n,
            "bottom_n": self.bottom_n,
            "rsi": round(rsi, 2),
            "funding_rate": funding_rate,
            "side": side.value,
            "sources": ["xs_momentum", f"rank_{rank}", f"ret7d_{ret_7d:.2%}"],
        }

    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, float]]:
        """Simple profit target / stop loss for momentum sleeve"""
        if position.entry_price <= 0:
            return None
        pnl_pct = (current_price - position.entry_price) / position.entry_price
        if position.side == Side.SHORT:
            pnl_pct = -pnl_pct

        # Profit target (scaled by leverage)
        if pnl_pct * position.leverage >= self.profit_target_pct / 100:
            return "xs_profit_target", current_price

        # Stop loss
        if pnl_pct * position.leverage <= -self.stop_loss_pct / 100:
            self._cooldowns[asset] = self.cooldown_cycles
            return "xs_stop_loss", current_price

        return None

    def on_exit(self, asset: str) -> None:
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
