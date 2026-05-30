import asyncio
import inspect
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.paper_perp import PaperPerpExchange
from src.core.loop import MIN_ENTRY_CONFIDENCE, TradingLoop
from src.core.perp_risk import PerpRiskManager
from src.core.reflect import SignalTracker
from src.core.types import Order, OrderType, PerpCandle, PerpPosition, Side, Signal
from src.strategies.mr import MeanReversion
from src.strategies.trend import TrendFollow


def candles(close: float = 100.0, n: int = 80):
    return [PerpCandle(i, close, close + 1, close - 1, close, 1_000_000) for i in range(n)]


class RegressionTests(unittest.TestCase):
    def test_mr_stop_does_not_exit_above_stop(self):
        strat = MeanReversion()
        pos = PerpPosition("BTC", Side.LONG, 100.0, 1.0, stop_loss=98.5, entry_time=datetime.now(timezone.utc))
        self.assertIsNone(strat.should_exit("BTC", pos, 100.0, candles(), 0.0))
        self.assertEqual(strat.should_exit("BTC", pos, 98.4, candles(), 0.0)[0], "stop_loss")

    def test_paper_order_uses_order_leverage_and_stop(self):
        async def run():
            ex = PaperPerpExchange(10_000)
            ex.update_price("BTC", 100.0)
            await ex.place_order(Order("BTC", Side.LONG, OrderType.MARKET, 1.0, stop_price=98.5, leverage=2.0))
            return ex.positions["BTC"]

        pos = asyncio.run(run())
        self.assertEqual(pos.leverage, 2.0)
        self.assertEqual(pos.stop_loss, 98.5)

    def test_risk_position_size_uses_current_exposure(self):
        risk = PerpRiskManager(initial_equity=10_000)
        qty, _, _ = risk.position_size("BTC", 10_000, 1.5, 100.0, current_gross_exposure=29_950)
        self.assertLessEqual(qty, 0.5)

    def test_signal_tracker_records_component_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = SignalTracker(Path(tmp) / "signals.json")
            tracker.record("altfins:rsi14_oversold", True)
            self.assertGreater(tracker.weight("altfins:rsi14_oversold"), 0.5)

    def test_strategy_component_sources_include_altfins(self):
        sig = Signal("altfins:CHANNEL_UP", "BTC", Side.LONG, 0.7, datetime.now(timezone.utc))
        strat = TrendFollow(signal_tracker=None)
        self.assertTrue(sig.source.startswith("altfins:"))
        self.assertEqual(strat.name(), "trend")

    def test_loop_has_exit_ownership_and_confidence_gate(self):
        source = inspect.getsource(TradingLoop._process_asset)
        self.assertIn("strat.name() != pos.strategy", source)
        self.assertIn("confidence < MIN_ENTRY_CONFIDENCE", source)
        self.assertEqual(MIN_ENTRY_CONFIDENCE, 0.70)


if __name__ == "__main__":
    unittest.main()
