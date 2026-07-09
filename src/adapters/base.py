"""
Exchange adapter contract — all exchange implementations must conform.
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.core.types import MarketSnapshot, Order, PerpCandle, PerpConfig, PerpPosition


class ExchangeAdapter(ABC):
    """Abstract interface for perpetual exchange adapters.

    Every method maps 1:1 to an exchange API call. No business logic.
    """

    @abstractmethod
    async def connect_ws(self):
        """Start WebSocket connection for real-time data."""

    @abstractmethod
    async def close(self):
        """Cleanup: close HTTP client, WebSocket, etc."""

    @abstractmethod
    async def fetch_all_mids(self) -> dict[str, float]:
        """Bulk fetch current mid prices for all assets. Returns {asset: price}."""

    @abstractmethod
    async def fetch_candles(
        self, asset: str, interval: str = "1h", limit: int = 200
    ) -> list[PerpCandle]:
        """Fetch OHLCV candles for an asset."""

    @abstractmethod
    async def fetch_funding(self) -> dict[str, float]:
        """Bulk fetch current funding rates. Returns {asset: rate}."""

    @abstractmethod
    async def fetch_open_interest(self) -> dict[str, float]:
        """Bulk fetch current open interest. Returns {asset: oi}."""

    @abstractmethod
    async def fetch_metadata(self) -> dict[str, PerpConfig]:
        """Fetch per-asset config (max leverage, step size, min size)."""

    @abstractmethod
    async def get_funding_rate(self, asset: str) -> float:
        """Get the latest cached funding rate for a single asset."""

    @abstractmethod
    async def get_spread(self, asset: str) -> float:
        """Get the current bid-ask spread percentage."""

    @abstractmethod
    async def fetch_price(self, asset: str) -> float:
        """Get the latest cached price for a single asset."""

    @abstractmethod
    async def fetch_snapshot(self, asset: str) -> MarketSnapshot:
        """Combined snapshot: price + funding + OI for one asset."""

    @abstractmethod
    async def fetch_positions(self) -> list[PerpPosition]:
        """Fetch all open positions from the exchange."""

    @abstractmethod
    async def place_order(self, order: Order) -> Optional[str]:
        """Place an order. Returns order ID on success, None on failure."""

    @abstractmethod
    async def cancel_order(self, cloid: str) -> bool:
        """Cancel an order by client order ID. Returns True if cancelled."""
