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
        magnitude_factor = min(abs(ret_7d) / 0.02, 2.0)
        confidence = 0.50 + 0.15 * rank_factor + 0.10 * min(magnitude_factor, 1.0)
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
        if position.entry_price <= 0:
            return None
        pnl_pct = (current_price - position.entry_price) / position.entry_price
        if position.side == Side.SHORT:
            pnl_pct = -pnl_pct

        if pnl_pct * position.leverage >= self.profit_target_pct / 100:
            return "xs_profit_target", current_price

        if pnl_pct * position.leverage <= -self.stop_loss_pct / 100:
            self._cooldowns[asset] = self.cooldown_cycles
            return "xs_stop_loss", current_price

        return None

    def on_exit(self, asset: str) -> None:
        self._cooldowns[asset] = self.cooldown_cycles
