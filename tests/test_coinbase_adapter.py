"""Tests for CoinbaseAdvancedAdapter — verifies contract conformance."""

from unittest.mock import AsyncMock, patch

import pytest

from src.adapters.base import ExchangeAdapter
from src.adapters.coinbase_advanced import CoinbaseAdvancedAdapter
from src.core.types import MarketSnapshot, Order, OrderType, PerpCandle, PerpConfig, Side


SAMPLE_PRODUCTS_RESPONSE = {
    "products": [
        {
            "product_id": "BTC-PERP-INTX",
            "price": "50000.0",
            "base_increment": "0.0001",
            "base_min_size": "0.001",
            "future_product_details": {
                "funding_rate": "0.0001",
                "open_interest": "1000.0",
                "perpetual_details": {
                    "max_leverage": "50",
                    "funding_rate": "0.0001",
                    "open_interest": "1000.0",
                },
            },
        },
        {
            "product_id": "ETH-PERP-INTX",
            "price": "3000.0",
            "base_increment": "0.001",
            "base_min_size": "0.01",
            "future_product_details": {
                "funding_rate": "-0.00005",
                "open_interest": "5000.0",
                "perpetual_details": {
                    "max_leverage": "50",
                    "funding_rate": "-0.00005",
                    "open_interest": "5000.0",
                },
            },
        },
    ],
    "num_products": 2,
}

SAMPLE_CANDLES_RESPONSE = {
    "candles": [
        {"start": "1000000", "open": "50000", "high": "50100", "low": "49900", "close": "50050", "volume": "1000"},
        {"start": "1003600", "open": "50050", "high": "50150", "low": "49950", "close": "50000", "volume": "1200"},
    ],
}


class TestCoinbaseAdapterContract:
    """Verify CoinbaseAdvancedAdapter implements ExchangeAdapter."""

    def test_implements_exchange_adapter(self):
        adapter = CoinbaseAdvancedAdapter()
        assert isinstance(adapter, ExchangeAdapter)

    def test_asset_mapping(self):
        from src.adapters.coinbase_advanced import ASSET_TO_PRODUCT, PRODUCT_TO_ASSET, CoinbaseAdvancedAdapter
        # Standard convention: most assets use {NAME}-PERP-INTX
        adapter = CoinbaseAdvancedAdapter()
        assert adapter._product_id("BTC") == "BTC-PERP-INTX"
        assert adapter._product_id("ETH") == "ETH-PERP-INTX"
        # Small-priced tokens have 1000 prefix
        assert adapter._product_id("PEPE") == "1000PEPE-PERP-INTX"
        assert adapter._product_id("BONK") == "1000BONK-PERP-INTX"
        # Reverse mapping
        assert adapter._asset_name("BTC-PERP-INTX") == "BTC"
        assert adapter._asset_name("1000PEPE-PERP-INTX") == "PEPE"
        assert adapter._asset_name("1000BONK-PERP-INTX") == "BONK"
        # Override count (only non-standard mappings)
        assert len(ASSET_TO_PRODUCT) == 3

    @pytest.mark.asyncio
    async def test_fetch_all_mids(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._request = AsyncMock(return_value=SAMPLE_PRODUCTS_RESPONSE)
        result = await adapter.fetch_all_mids()
        assert isinstance(result, dict)
        assert "BTC" in result
        assert result["BTC"] == 50000.0
        assert "ETH" in result
        assert result["ETH"] == 3000.0

    @pytest.mark.asyncio
    async def test_fetch_candles(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._request = AsyncMock(return_value=SAMPLE_CANDLES_RESPONSE)
        candles = await adapter.fetch_candles("BTC", "1h", 2)
        assert isinstance(candles, list)
        assert len(candles) == 2
        c = candles[0]
        assert isinstance(c, PerpCandle)
        assert c.open == 50000.0
        assert c.close == 50050.0

    @pytest.mark.asyncio
    async def test_fetch_candles_empty(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._request = AsyncMock(return_value={"candles": []})
        candles = await adapter.fetch_candles("UNKNOWN", "1h", 1)
        assert candles == []

    @pytest.mark.asyncio
    async def test_fetch_funding(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._request = AsyncMock(return_value=SAMPLE_PRODUCTS_RESPONSE)
        result = await adapter.fetch_funding()
        assert isinstance(result, dict)
        assert result["BTC"] == 0.0001
        assert result["ETH"] == -0.00005

    @pytest.mark.asyncio
    async def test_fetch_open_interest(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._request = AsyncMock(return_value=SAMPLE_PRODUCTS_RESPONSE)
        result = await adapter.fetch_open_interest()
        assert isinstance(result, dict)
        assert result["BTC"] == 1000.0
        assert result["ETH"] == 5000.0

    @pytest.mark.asyncio
    async def test_fetch_metadata(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._request = AsyncMock(return_value=SAMPLE_PRODUCTS_RESPONSE)
        result = await adapter.fetch_metadata()
        assert isinstance(result, dict)
        assert "BTC" in result
        cfg = result["BTC"]
        assert isinstance(cfg, PerpConfig)
        assert cfg.max_leverage == 50
        assert cfg.step_size == 0.0001
        assert cfg.min_size == 0.001

    @pytest.mark.asyncio
    async def test_get_funding_rate(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._latest_funding = {"BTC": 0.0001}
        rate = await adapter.get_funding_rate("BTC")
        assert rate == 0.0001
        rate = await adapter.get_funding_rate("UNKNOWN")
        assert rate == 0.0

    @pytest.mark.asyncio
    async def test_fetch_price(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._latest_prices = {"BTC": 50000.0}
        price = await adapter.fetch_price("BTC")
        assert price == 50000.0

    @pytest.mark.asyncio
    async def test_fetch_snapshot(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._request = AsyncMock(return_value=SAMPLE_PRODUCTS_RESPONSE)
        snap = await adapter.fetch_snapshot("BTC")
        assert isinstance(snap, MarketSnapshot)
        assert snap.asset == "BTC"
        assert snap.funding_rate == 0.0001
        assert snap.open_interest == 1000.0

    @pytest.mark.asyncio
    async def test_fetch_positions_empty_without_uuid(self):
        adapter = CoinbaseAdvancedAdapter()
        positions = await adapter.fetch_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_place_order_noop_without_key(self):
        adapter = CoinbaseAdvancedAdapter()
        order = Order(asset="BTC", side=Side.LONG, order_type=OrderType.MARKET, quantity=1.0)
        result = await adapter.place_order(order)
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_order_returns_false_without_key(self):
        adapter = CoinbaseAdvancedAdapter()
        result = await adapter.cancel_order("BTC", "test_cloid")
        assert result is False

    @pytest.mark.asyncio
    async def test_close(self):
        adapter = CoinbaseAdvancedAdapter()
        adapter._http.aclose = AsyncMock()
        await adapter.close()
        adapter._http.aclose.assert_awaited_once()
