from abc import ABC, abstractmethod
from typing import Optional

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal


class PerpStrategy(ABC):
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
