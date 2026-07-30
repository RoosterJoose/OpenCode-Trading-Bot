"""
Cross-sectional momentum strategy — long top performers, short bottom.

Stripped for Phase 1: no drift filter, no RSI check, no funding check,
no EMA50 divergence. Simple rank by 7-day return + confidence.
"""

from typing import Optional
import logging

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.base import PerpStrategy

logger = logging.getLogger(__name__)


class CrossSectionalMomentum(PerpStrategy):
    _asset_returns_7d: dict[str, float] = {}

    def __init__(
        self,
        lookback_candles: int = 168,
        top_n: int = 3,
        bottom_n: int = 3,
        min_volume_usd: float = 0,
        cooldown_cycles: int = 60,
        profit_target_pct: float = 5.0,
        atr_target_mult: float = 2.5,
        atr_stop_mult: float = 1.5,
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
        self.atr_target_mult = atr_target_mult
        self.atr_stop_mult = atr_stop_mult
        self.stop_loss_pct = stop_loss_pct
        self.majors = majors or {"BTC", "ETH"}
        self.signal_tracker = signal_tracker
        self._cooldowns: dict[str, int] = {}
        self._last_entry_candle: dict[str, float] = {}
        self.blocked_assets: set = {"ZEC", "AAVE", "ADA"}

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

        if candles and len(candles) >= 2:
            latest_candle_ts = candles[-1].timestamp
            if latest_candle_ts == self._last_entry_candle.get(asset, 0):
                return None

        if asset in self.blocked_assets:
            return None

        if position is not None:
            return None

        if len(candles) < self.lookback_candles + 5:
            return None

        last = candles[-1]
        vol_min = self._get_threshold(asset, "volume_min_usd", self.min_volume_usd)
        if last.volume * last.close < vol_min:
            return None

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

        if is_long_pick and ret_7d < -0.03:
            return None
        if is_short_pick and ret_7d > -0.01:
            return None

        logger.info("XS_MOMENTUM %s: side=%s ret_7d=%.2f%%",
                    asset, "LONG" if is_long_pick else "SHORT", ret_7d * 100)

        is_long = is_long_pick

        rank = top_assets.index(asset) + 1 if is_long else bottom_assets.index(asset) + 1
        rank_factor = 1.0 - (rank - 1) * 0.1

        # Z-score normalized confidence
        all_rets = [v for _, v in sorted_assets]
        if len(all_rets) > 2:
            mu = sum(all_rets) / len(all_rets)
            std = (sum((r - mu) ** 2 for r in all_rets) / len(all_rets)) ** 0.5
            if std > 0:
                z = (ret_7d - mu) / std
                z_confidence = 0.70 + z * 0.10
                z_confidence = min(max(z_confidence, 0.50), 0.95)
            else:
                z_confidence = 0.70
        else:
            z_confidence = 0.70

        confidence = z_confidence * 0.6 + (0.50 + 0.15 * rank_factor) * 0.4

        side = Side.LONG if is_long else Side.SHORT
        entry_price = last.close

        confidence = self.blend_altfins_confidence(confidence, signals)
        return side, confidence, {
            "entry_price": entry_price,
            "ret_7d": round(ret_7d * 100, 2),
            "rank": rank,
            "top_n": self.top_n,
            "bottom_n": self.bottom_n,
            "funding_rate": funding_rate,
            "side": side.value,
            "sources": ["xs_momentum", f"rank_{rank}", f"ret7d_{ret_7d:.2%}"],
        }

    def _compute_atr(self, candles: list, period: int = 14) -> float:
        if not candles or len(candles) < period + 1:
            return 0.0
        total = 0.0
        for i in range(-period, 0):
            h = candles[i].high
            lo = candles[i].low
            pc = candles[i - 1].close
            total += max(h - lo, abs(h - pc), abs(lo - pc))
        return total / period

    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, float]]:
        if position.entry_price <= 0:
            return None
        if position.leverage <= 0:
            return None

        pnl_pct = (current_price - position.entry_price) / position.entry_price
        if position.side == Side.SHORT:
            pnl_pct = -pnl_pct

        # ATR-scaled stop and target
        atr_val = self._compute_atr(candles, period=14) if candles else 0.0
        if atr_val > 0 and position.entry_price > 0:
            atr_pct_of_price = (atr_val / position.entry_price) * 100
            dynamic_target_pct = atr_pct_of_price * self.atr_target_mult
            dynamic_target_pct = max(1.0, min(dynamic_target_pct, 15.0))
            dynamic_stop_pct = atr_pct_of_price * self.atr_stop_mult
            dynamic_stop_pct = max(1.0, min(dynamic_stop_pct, 8.0))
        else:
            fallback_atr_pct = self.profit_target_pct / max(self.atr_target_mult, 0.01)
            dynamic_target_pct = fallback_atr_pct * self.atr_target_mult
            dynamic_target_pct = max(1.0, min(dynamic_target_pct, 15.0))
            dynamic_stop_pct = self.stop_loss_pct

        # tp1 at 1x ATR - close 50%, trail rest
        tp1_target_pct = dynamic_target_pct / self.atr_target_mult
        if pnl_pct >= tp1_target_pct / 100 and not getattr(position, "tp1_scaled", False):
            return "tp1", current_price

        if pnl_pct >= dynamic_target_pct / 100:
            return "xs_profit_target", current_price

        if pnl_pct <= -dynamic_stop_pct / 100:
            self._cooldowns[asset] = self.cooldown_cycles
            return "xs_stop_loss", current_price

        return None

    def on_exit(self, asset: str) -> None:
        self._cooldowns[asset] = self.cooldown_cycles
