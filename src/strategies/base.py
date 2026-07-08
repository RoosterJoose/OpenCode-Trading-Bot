from abc import ABC, abstractmethod
from typing import Optional, Any

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal


class PerpStrategy(ABC):
    ALTFINS_WEIGHT = 0.15

    @staticmethod
    def blend_altfins_confidence(local_confidence: float, altfins_signals: list) -> float:
        """Blend local TA confidence with altFINS signals using a weighted composite.
        
        altFINS acts as a confidence modifier (0.15 weight), not a standalone trigger.
        If altFINS agrees with direction, confidence gets a boost.
        If altFINS disagrees or is absent, confidence gets a small penalty.
        """
        if not altfins_signals:
            # No altFINS data: slight penalty
            return local_confidence * 0.95
        
        # Find the altFINS signal for this direction
        aligned = any(s.confidence >= 0.6 for s in altfins_signals)
        if aligned:
            return local_confidence * (1.0 + PerpStrategy.ALTFINS_WEIGHT)
        else:
            return local_confidence * (1.0 - PerpStrategy.ALTFINS_WEIGHT * 0.5)
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
