from abc import ABC, abstractmethod
from typing import Optional, Any

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal


class PerpStrategy(ABC):
    def __init__(self):
        self._dynamic_thresholds: dict = {}

    def set_dynamic_thresholds(self, thresholds: dict) -> None:
        """Inject per-asset threshold adjustments from closed_loop.py"""
        self._dynamic_thresholds = thresholds or {}

    def _get_threshold(self, asset: str, param: str, default: Any):
        """Get a dynamic threshold for an asset+param, falling back to default."""
        raw = getattr(self, "_dynamic_thresholds", {}).get(asset, {}).get(param, default)
        if isinstance(raw, dict) and "value" in raw:
            return raw["value"]
        return raw

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def should_enter(
        self,
        asset: str,
        candles: list[PerpCandle],
        signals: list[Signal],
        regime: RegimeType,
        position: Optional[PerpPosition],
        funding_rate: float,
    ) -> Optional[tuple[Side, float, dict]]: ...

    @abstractmethod
    def should_exit(
        self,
        asset: str,
        position: PerpPosition,
        current_price: float,
        candles: list[PerpCandle],
        funding_rate: float,
    ) -> Optional[tuple[str, Optional[float]]]: ...
