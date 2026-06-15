import asyncio
import inspect
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

async def _mock_get_funding_rate(asset: str) -> float:
    return 0.0

def _mock_exchange(latest_funding: dict | None = None) -> SimpleNamespace:
    ns = SimpleNamespace()
    ns._latest_funding = latest_funding or {}
    ns.get_funding_rate = _mock_get_funding_rate
    return ns

from src.adapters.paper_perp import PaperPerpExchange
from src.core.intents import TradeIntent
from src.core.loop import MIN_ENTRY_CONFIDENCE, TradingLoop
from src.core.perp_risk import PerpRiskManager
from src.core.reflect import SignalTracker
from src.core.types import Order, OrderType, PerpCandle, PerpPosition, Side, Signal
from src.store.sqlite import Store
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

    def test_intent_store_idempotency(self):
        now = datetime.now(timezone.utc)
        intent = {
            "idempotency_key": "test:BTC:1h:long",
            "source": "freqtrade",
            "strategy": "HermesPerpStrategy",
            "asset": "BTC",
            "side": "long",
            "confidence": 0.8,
            "intended_entry_price": 100.0,
            "requested_stop_price": 98.0,
            "requested_leverage": 2.0,
            "components": ["local:test"],
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=30)).isoformat(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "hermes.db")
            try:
                self.assertTrue(store.save_intent(intent))
                self.assertFalse(store.save_intent(intent))
                self.assertEqual(len(store.pending_intents()), 1)
            finally:
                store.close()

    def test_expired_intent_rejected(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                loop = TradingLoop({}, Path(tmp))
                ex = PaperPerpExchange(10_000)
                ex.update_price("BTC", 100.0)
                row = self._intent_row(expires_delta=-1)
                intent = TradeIntent.from_row(row)
                try:
                    return await loop._execute_intent(intent, ex, _mock_exchange({}))
                finally:
                    loop.store.close()

        ok, reason = asyncio.run(run())
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")

    def test_valid_intent_executes_paper_position(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                loop = TradingLoop({}, Path(tmp))
                loop.candle_cache["BTC"] = candles(100.0, 120)
                ex = PaperPerpExchange(10_000)
                ex.update_price("BTC", 100.0)
                row = self._intent_row()
                intent = TradeIntent.from_row(row)
                try:
                    ok, reason = await loop._execute_intent(intent, ex, _mock_exchange({"BTC": 0.0}))
                    return ok, reason, ex.positions.get("BTC")
                finally:
                    loop.store.close()

        ok, reason, pos = asyncio.run(run())
        self.assertTrue(ok, reason)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.signal_source, "intent:freqtrade:BTC")
        self.assertEqual(pos.component_sources, ["local:test"])

    def test_intent_invalid_stop_and_low_confidence_rejected(self):
        async def run(row):
            with tempfile.TemporaryDirectory() as tmp:
                loop = TradingLoop({}, Path(tmp))
                ex = PaperPerpExchange(10_000)
                ex.update_price("BTC", 100.0)
                intent = TradeIntent.from_row(row)
                try:
                    return await loop._execute_intent(intent, ex, _mock_exchange({"BTC": 0.0}))
                finally:
                    loop.store.close()

        low_conf = self._intent_row(confidence=0.69)
        ok, reason = asyncio.run(run(low_conf))
        self.assertFalse(ok)
        self.assertIn("confidence_below_gate", reason)

        bad_stop = self._intent_row(stop=99.9)
        ok, reason = asyncio.run(run(bad_stop))
        self.assertFalse(ok)
        self.assertIn("stop_distance_out_of_bounds", reason)

    def _intent_row(self, confidence=0.8, stop=97.0, expires_delta=30):
        now = datetime.now(timezone.utc)
        payload = {
            "idempotency_key": f"test:BTC:{now.timestamp()}:{confidence}:{stop}:{expires_delta}",
            "source": "freqtrade",
            "strategy": "HermesPerpStrategy",
            "asset": "BTC",
            "side": "long",
            "confidence": confidence,
            "intended_entry_price": 100.0,
            "requested_stop_price": stop,
            "requested_leverage": 2.0,
            "components": ["local:test"],
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=expires_delta)).isoformat(),
        }
        return {
            "id": 1,
            **payload,
            "components": json.dumps(payload["components"]),
            "payload": json.dumps(payload),
        }


if __name__ == "__main__":
    unittest.main()
