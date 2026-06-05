"""Tests for HyperliquidAdapter — verifies contract conformance."""

from unittest.mock import AsyncMock, patch

import pytest

from src.adapters.base import ExchangeAdapter
from src.adapters.hyperliquid import HyperliquidAdapter
from src.core.types import MarketSnapshot, Order, OrderType, PerpCandle, PerpConfig, Side


class TestHyperliquidAdapterContract:
    """Verify HyperliquidAdapter implements every ExchangeAdapter method."""

    def test_implements_exchange_adapter(self):
        adapter = HyperliquidAdapter()
        assert isinstance(adapter, ExchangeAdapter)

    @pytest.mark.asyncio
    async def test_fetch_all_mids(self, mock_hl_adapter):
        result = await mock_hl_adapter.fetch_all_mids()
        assert isinstance(result, dict)
        assert "BTC" in result
        assert result["BTC"] == 50000.0

    @pytest.mark.asyncio
    async def test_fetch_candles(self, mock_hl_adapter):
        mock_hl_adapter._info = AsyncMock(return_value=[
            {"t": 1000000, "o": "50000", "h": "50100", "l": "49900", "c": "50050", "v": "1000"},
        ])
        candles = await mock_hl_adapter.fetch_candles("BTC", "1h", 1)
        assert isinstance(candles, list)
        assert len(candles) == 1
        c = candles[0]
        assert isinstance(c, PerpCandle)
        assert c.open == 50000.0
        assert c.close == 50050.0
        assert c.high == 50100.0
        assert c.low == 49900.0
        assert c.volume == 1000.0

    @pytest.mark.asyncio
    async def test_fetch_candles_empty(self, mock_hl_adapter):
        mock_hl_adapter._info = AsyncMock(return_value=[])
        candles = await mock_hl_adapter.fetch_candles("UNKNOWN", "1h", 1)
        assert candles == []

    @pytest.mark.asyncio
    async def test_fetch_funding(self, mock_hl_adapter):
        with patch.object(mock_hl_adapter, 'fetch_asset_contexts',
                          AsyncMock(return_value={"BTC": {"funding": "0.0001"}})):
            result = await mock_hl_adapter.fetch_funding()
        assert isinstance(result, dict)
        assert "BTC" in result
        assert result["BTC"] == 0.0001

    @pytest.mark.asyncio
    async def test_fetch_open_interest(self, mock_hl_adapter):
        with patch.object(mock_hl_adapter, 'fetch_asset_contexts',
                          AsyncMock(return_value={"BTC": {"openInterest": "1000.0"}})):
            result = await mock_hl_adapter.fetch_open_interest()
        assert isinstance(result, dict)
        assert "BTC" in result
        assert result["BTC"] == 1000.0

    @pytest.mark.asyncio
    async def test_fetch_metadata(self, mock_hl_adapter):
        mock_hl_adapter._info = AsyncMock(return_value={
            "universe": [{"name": "BTC", "maxLeverage": 50, "szDecimals": 3, "minSize": 0.001}]
        })
        result = await mock_hl_adapter.fetch_metadata()
        assert isinstance(result, dict)
        assert "BTC" in result
        cfg = result["BTC"]
        assert isinstance(cfg, PerpConfig)
        assert cfg.max_leverage == 50
        assert cfg.step_size == 0.001

    @pytest.mark.asyncio
    async def test_get_funding_rate(self, mock_hl_adapter):
        rate = await mock_hl_adapter.get_funding_rate("BTC")
        assert rate == 0.0001
        rate = await mock_hl_adapter.get_funding_rate("UNKNOWN")
        assert rate == 0.0

    @pytest.mark.asyncio
    async def test_fetch_price(self, mock_hl_adapter):
        price = await mock_hl_adapter.fetch_price("BTC")
        assert price == 50000.0
        price = await mock_hl_adapter.fetch_price("UNKNOWN")
        assert price == 0.0

    @pytest.mark.asyncio
    async def test_fetch_snapshot(self, mock_hl_adapter):
        with patch.multiple(
            mock_hl_adapter,
            fetch_all_mids=AsyncMock(return_value={"BTC": 50000.0}),
            fetch_funding=AsyncMock(return_value={"BTC": 0.0001}),
            fetch_open_interest=AsyncMock(return_value={"BTC": 1000.0}),
        ):
            snap = await mock_hl_adapter.fetch_snapshot("BTC")
        assert isinstance(snap, MarketSnapshot)
        assert snap.asset == "BTC"
        assert snap.price == 50000.0
        assert snap.funding_rate == 0.0001
        assert snap.open_interest == 1000.0

    @pytest.mark.asyncio
    async def test_place_order_not_implemented_without_wallet(self):
        adapter = HyperliquidAdapter()
        order = Order(asset="BTC", side=Side.LONG, order_type=OrderType.MARKET, quantity=1.0)
        result = await adapter.place_order(order)
        assert result is None  # no wallet configured

    @pytest.mark.asyncio
    async def test_close(self, mock_hl_adapter):
        mock_hl_adapter._http.aclose = AsyncMock()
        await mock_hl_adapter.close()
        mock_hl_adapter._http.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_order_returns_false_without_wallet(self):
        adapter = HyperliquidAdapter()
        result = await adapter.cancel_order("test_cloid")
        assert result is False
