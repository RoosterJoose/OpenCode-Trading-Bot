"""Shared fixtures for adapter tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters._hyperliquid_deprecated import HyperliquidAdapter


@pytest.fixture
def mock_hl_adapter():
    """HyperliquidAdapter with _info mocked to return empty data."""
    adapter = HyperliquidAdapter(wallet_address="test", private_key="test")
    adapter._info = AsyncMock(return_value={})
    adapter.fetch_all_mids = AsyncMock(return_value={"BTC": 50000.0, "ETH": 3000.0})
    adapter._latest_mids = {"BTC": 50000.0, "ETH": 3000.0}
    adapter._latest_funding = {"BTC": 0.0001, "ETH": -0.00005}
    adapter._latest_oi = {"BTC": 1000.0, "ETH": 5000.0}
    return adapter


SAMPLE_CANDLE_RESPONSE = [
    {"t": 1000000, "o": "50000", "h": "50100", "l": "49900", "c": "50050", "v": "1000"},
    {"t": 1003600, "o": "50050", "h": "50150", "l": "49950", "c": "50000", "v": "1200"},
]

SAMPLE_ASSET_CONTEXTS = {
    "BTC": {"funding": "0.0001", "openInterest": "1000.0"},
    "ETH": {"funding": "-0.00005", "openInterest": "5000.0"},
}

SAMPLE_META_RESPONSE = {
    "universe": [
        {"name": "BTC", "maxLeverage": 50, "szDecimals": 3, "minSize": 0.001},
        {"name": "ETH", "maxLeverage": 50, "szDecimals": 3, "minSize": 0.001},
    ]
}
