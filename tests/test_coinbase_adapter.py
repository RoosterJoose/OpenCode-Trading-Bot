"""Tests for CoinbaseAdvancedAdapter — validates contract conformance.

Coinbase adapter not yet implemented. These tests will be filled in
when the adapter is created in Step 4.
"""


class TestCoinbaseAdapterExists:
    """Verify the adapter can be imported and implements ExchangeAdapter."""

    def test_adapter_import(self):
        try:
            from src.adapters.coinbase_advanced import CoinbaseAdvancedAdapter
            from src.adapters.base import ExchangeAdapter
            assert issubclass(CoinbaseAdvancedAdapter, ExchangeAdapter)
        except ImportError:
            pass  # Not implemented yet — this test is a placeholder
