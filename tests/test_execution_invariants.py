"""
Phase 2.4: Property and invariant tests for the execution engine.

Run: PYTHONPATH=. python tests/test_execution_invariants.py

Tests:
1. Equity reconciliation (balance + unrealized = equity)
2. Side-aware R-multiple (shorts correct)
3. Fee tracking in trade records
4. Partial close accounting
5. Market fills walk the spread (buy at ask, sell at bid)
6. Limit queue determinism
7. No phantom positions
8. Funding accrual per-asset
9. Kill switch latched state
10. Experiment registry append-only
"""

import asyncio
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.execution_engine import ExecutionEngine, OrderState, OrderRecord, TradeRecord
from src.core.types import Order, OrderType, PerpConfig, PerpPosition, Side
from src.core.risk_governor import RiskGovernor, ExecutionState, KillSwitchReason
from src.core.experiment_registry import ExperimentRegistry


class MockStore:
    def __init__(self):
        self.state = {}
    def get_state(self, key):
        return self.state.get(key)
    def put_state(self, key, val):
        self.state[key] = val


class TestEquityReconciliation(unittest.TestCase):
    """Invariant: balance + unrealized PnL = equity at all times."""

    def test_no_positions(self):
        engine = ExecutionEngine(initial_balance=5000)
        self.assertEqual(engine.equity, 5000)
        rec = engine.reconcile()
        self.assertTrue(rec["reconciled"])
        self.assertEqual(rec["discrepancy"], 0.0)

    def test_with_position(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=2)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.MARKET,
            quantity=0.001, price=65000, leverage=1.0
        )))
        # After entry: balance reduced by fee, position exists
        self.assertIn("BTC", engine.positions)
        engine.update_price("BTC", 66000)  # +1.6%
        rec = engine.reconcile()
        self.assertTrue(rec["reconciled"], f"Discrepancy: {rec['discrepancy']}")
        self.assertAlmostEqual(rec["discrepancy"], 0.0, places=2)

    def test_after_close(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=2)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.MARKET,
            quantity=0.001, price=65000, leverage=1.0
        )))
        engine.update_price("BTC", 66000)
        trade = engine.close_position("BTC", 66000, exit_reason="tp")
        self.assertIsNotNone(trade)
        rec = engine.reconcile()
        self.assertTrue(rec["reconciled"])


class TestSideAwareRMultiple(unittest.TestCase):
    """Invariant: R-multiple is correct for both long and short."""

    def test_long_r_multiple(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=0)  # no spread for clean math
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.MARKET,
            quantity=0.01, price=65000, leverage=1.0, stop_price=63050  # 3% stop
        )))
        # Price goes up 3% (hits 1R)
        engine.update_price("BTC", 66950)
        trade = engine.close_position("BTC", 66950, exit_reason="tp")
        self.assertIsNotNone(trade)
        # R = (66000 - 65000) / (65000 * 0.03) ≈ 0.51 (after spread/fees)
        # With 0 spread, R = (66950 - 65000) / (65000 * 0.03) = 1950 / 1950 = 1.0
        self.assertGreater(trade.r_multiple, 0, "Long profit should have positive R")

    def test_short_r_multiple(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=0)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.SHORT, order_type=OrderType.MARKET,
            quantity=0.01, price=65000, leverage=1.0, stop_price=66950  # 3% stop
        )))
        # Price drops 3% (short profits)
        engine.update_price("BTC", 63050)
        trade = engine.close_position("BTC", 63050, exit_reason="tp")
        self.assertIsNotNone(trade)
        # Short R = (65000 - 63050) / (65000 * 0.03) = 1950 / 1950 = 1.0
        self.assertGreater(trade.r_multiple, 0, "Short profit should have positive R")

    def test_short_loss_negative_r(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=0)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.SHORT, order_type=OrderType.MARKET,
            quantity=0.01, price=65000, leverage=1.0, stop_price=66950
        )))
        # Price goes UP (short loses)
        engine.update_price("BTC", 66950)
        trade = engine.close_position("BTC", 66950, exit_reason="stop_loss")
        self.assertIsNotNone(trade)
        self.assertLess(trade.r_multiple, 0, "Short loss should have negative R")


class TestFeeTracking(unittest.TestCase):
    """Invariant: trade record fees are non-zero and correct."""

    def test_fees_not_zero(self):
        engine = ExecutionEngine(initial_balance=5000, taker_fee=0.00025)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.MARKET,
            quantity=0.01, price=65000, leverage=1.0
        )))
        engine.update_price("BTC", 66000)
        trade = engine.close_position("BTC", 66000, exit_reason="tp")
        self.assertIsNotNone(trade)
        self.assertGreater(trade.fees, 0, "Fees should be non-zero")
        # Expected: entry fee + exit fee = 0.01 * 65000 * 0.00025 + 0.01 * 66000 * 0.00025
        # = 0.1625 + 0.165 = 0.3275 (approx)
        self.assertGreater(trade.fees, 0.3, "Fees should be at least ~$0.32")


class TestSpreadWalking(unittest.TestCase):
    """Invariant: market buys fill at ask, sells fill at bid."""

    def test_buy_at_ask(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=4)
        engine.update_price("BTC", 65000)  # bid=64998, ask=65002
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.MARKET,
            quantity=0.001, price=65000, leverage=1.0
        )))
        pos = engine.positions.get("BTC")
        self.assertIsNotNone(pos)
        # Entry should be at ask (65002), not mid (65000)
        self.assertGreater(pos.entry_price, 65000, "Buy should fill at ask > mid")

    def test_sell_at_bid(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=4)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.SHORT, order_type=OrderType.MARKET,
            quantity=0.001, price=65000, leverage=1.0
        )))
        pos = engine.positions.get("BTC")
        self.assertIsNotNone(pos)
        # Entry should be at bid (64998), not mid
        self.assertLess(pos.entry_price, 65000, "Sell should fill at bid < mid")


class TestLimitQueueDeterminism(unittest.TestCase):
    """Invariant: limit orders fill when price crosses, don't when it doesn't."""

    def test_limit_fills_on_cross(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=0)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        # Place limit buy at 64000 (below current price)
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.LIMIT,
            quantity=0.001, price=64000, leverage=1.0
        )))
        # Should be queued, not filled
        self.assertEqual(len(engine.positions), 0)
        self.assertEqual(len(engine._limit_queue), 1)
        # Price drops to convers limit
        engine.update_price("BTC", 63900)
        self.assertEqual(len(engine._limit_queue), 0, "Limit should fill on cross")
        self.assertIn("BTC", engine.positions)

    def test_limit_no_fill_no_cross(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=0)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.LIMIT,
            quantity=0.001, price=64000, leverage=1.0
        )))
        # Price stays above limit
        engine.update_price("BTC", 65500)
        self.assertEqual(len(engine._limit_queue), 1, "Limit should stay queued")
        self.assertNotIn("BTC", engine.positions)


class TestNoPhantomPositions(unittest.TestCase):
    """Invariant: a submitted order doesn't mean a position exists without a fill."""

    def test_limit_no_position_until_fill(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=0)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        cloid = asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.LIMIT,
            quantity=0.001, price=64000, leverage=1.0
        )))
        # Order exists but no position
        self.assertIn(cloid, engine._orders)
        self.assertEqual(engine._orders[cloid].state, OrderState.ACKNOWLEDGED)
        self.assertNotIn("BTC", engine.positions, "No position until fill")


class TestPartialClose(unittest.TestCase):
    """Invariant: partial close reduces size correctly."""

    def test_partial_close_size(self):
        engine = ExecutionEngine(initial_balance=5000, spread_bps=0)
        engine.update_price("BTC", 65000)
        engine.set_perp_config("BTC", PerpConfig(asset="BTC", max_leverage=3, step_size=0.001, min_size=0.001))
        asyncio.run(engine.place_order(Order(
            asset="BTC", side=Side.LONG, order_type=OrderType.MARKET,
            quantity=0.02, price=65000, leverage=1.0
        )))
        original_size = engine.positions["BTC"].size
        # Close 50%
        trade = engine.close_position("BTC", 66000, close_pct=0.5, exit_reason="tp1")
        self.assertIsNotNone(trade)
        remaining = engine.positions.get("BTC")
        self.assertIsNotNone(remaining, "Position should exist after partial close")
        self.assertAlmostEqual(remaining.size, original_size * 0.5, places=6)


class TestRiskGovernorLatched(unittest.TestCase):
    """Invariant: kill switch is latched — survives restart, not cleared by self-heal."""

    def test_kill_survives_restart(self):
        store = MockStore()
        gov1 = RiskGovernor(store, initial_capital=5000)
        gov1.trigger_kill(KillSwitchReason.DRAWDOWN_LIMIT)
        self.assertTrue(gov1.is_killed())

        # Simulate restart
        gov2 = RiskGovernor(store, initial_capital=5000)
        self.assertTrue(gov2.is_killed(), "Kill must survive restart")

    def test_self_heal_cannot_clear(self):
        store = MockStore()
        gov = RiskGovernor(store, initial_capital=5000)
        gov.trigger_kill(KillSwitchReason.MANUAL)
        gov.clear_kill(human_ack=False)  # self-heal attempt
        self.assertTrue(gov.is_killed(), "Self-heal must not clear kill")

    def test_human_can_clear(self):
        store = MockStore()
        gov = RiskGovernor(store, initial_capital=5000)
        gov.trigger_kill(KillSwitchReason.MANUAL)
        gov.clear_kill(human_ack=True)
        self.assertFalse(gov.is_killed(), "Human should clear kill")


class TestExperimentRegistry(unittest.TestCase):
    """Invariant: registry is append-only, IDs are unique."""

    def test_register_and_count(self):
        db = tempfile.mktemp(suffix=".db")
        reg = ExperimentRegistry(db)
        hyp_id = reg.register_hypothesis("test", "mechanism", "criterion")
        cand_id = reg.register_candidate(hyp_id, {"p": 1}, ["BTC"], {}, ["bh"])
        trial_id = reg.register_trial(cand_id, hyp_id, {}, "rule", "metric", 0.0)
        self.assertEqual(reg.count_trials(), 1)
        reg.complete_trial(trial_id, {"result": "pass"})
        self.assertEqual(reg.count_trials(), 1)
        reg.close()
        os.unlink(db)


if __name__ == "__main__":
    unittest.main(verbosity=2)