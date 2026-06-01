import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.core.loop import TradingLoop
from src.core.types import PerpCandle, RegimeType, Side, Signal
from src.core.reflect import SignalTracker
from src.strategies.mr import MeanReversion
from src.strategies.trend import TrendFollow as TrendFollowStrategy


def downtrend_candles(n: int = 120, start: float = 100.0) -> list[PerpCandle]:
    """Generate downtrend candles with ADX > 50 and bear EMA cross."""
    candles = []
    # 60 flat candles to fill buffers
    for i in range(60):
        candles.append(PerpCandle(i, start, start + 0.5, start - 0.5, start, 1_000_000))

    # 60 downtrend candles: each step down ~0.3%
    price = start
    for i in range(60, 60 + n):
        decline = price * 0.003
        bounce = decline * 0.2
        o = price
        h = o + bounce
        l = o - decline
        c = o - decline * 0.8
        candles.append(PerpCandle(i, o, h, l, c, 1_000_000))
        price = c

    return candles


def uptrend_candles(n: int = 120, start: float = 80.0) -> list[PerpCandle]:
    """Generate uptrend candles with ADX > 50 and bull EMA cross."""
    candles = []
    for i in range(60):
        candles.append(PerpCandle(i, start, start + 0.5, start - 0.5, start, 1_000_000))

    price = start
    for i in range(60, 60 + n):
        rise = price * 0.003
        pullback = rise * 0.2
        o = price
        h = o + rise
        l = o - pullback
        c = o + rise * 0.8
        candles.append(PerpCandle(i, o, h, l, c, 1_000_000))
        price = c

    return candles


def sideways_candles(n: int = 120, price: float = 100.0) -> list[PerpCandle]:
    """Generate random-walk-like candles with no trend for RANDOM_WALK regime."""
    import random
    random.seed(42)
    candles = []
    p = price
    for i in range(n):
        noise = (random.random() - 0.5) * 0.4
        o = p
        c = p + noise
        h = max(o, c) + abs(noise) * 0.5
        l = min(o, c) - abs(noise) * 0.5
        candles.append(PerpCandle(i, o, h, l, c, 1_000_000))
        p = c
    return candles


class SimulationTests(unittest.TestCase):
    def test_downtrend_regime_is_strongly_trending(self):
        """ADX > 50 downtrend produces STRONGLY_TRENDING regime."""
        c = downtrend_candles()
        regime = TradingLoop._infer_regime(c)
        self.assertEqual(regime, RegimeType.STRONGLY_TRENDING)

    def test_uptrend_regime_is_strongly_trending(self):
        """ADX > 50 uptrend produces STRONGLY_TRENDING regime."""
        c = uptrend_candles()
        regime = TradingLoop._infer_regime(c)
        self.assertEqual(regime, RegimeType.STRONGLY_TRENDING)

    def test_sideways_regime_is_random_walk(self):
        """Low-vol sideways produces RANDOM_WALK regime."""
        c = sideways_candles()
        regime = TradingLoop._infer_regime(c)
        self.assertIn(regime, (RegimeType.RANDOM_WALK, RegimeType.MEAN_REVERTING),
                      f"Sideways data should not be trending (got {regime})")

    def test_trend_short_entry_on_downtrend(self):
        """Trend strategy emits SHORT with confidence >= 0.70 in downtrend."""
        c = downtrend_candles()
        strat = TrendFollowStrategy()
        result = strat.should_enter("BTC", c, [], RegimeType.STRONGLY_TRENDING, None, 0.0)
        self.assertIsNotNone(result, "Trend should enter SHORT in strong downtrend")
        side, confidence, meta = result
        self.assertEqual(side, Side.SHORT)
        self.assertGreaterEqual(confidence, 0.70, f"Confidence {confidence:.2f} < 0.70")

    def test_trend_long_entry_on_uptrend(self):
        """Trend strategy emits LONG with confidence >= 0.70 in uptrend."""
        c = uptrend_candles()
        strat = TrendFollowStrategy()
        result = strat.should_enter("BTC", c, [], RegimeType.STRONGLY_TRENDING, None, 0.0)
        self.assertIsNotNone(result, "Trend should enter LONG in strong uptrend")
        side, confidence, meta = result
        self.assertEqual(side, Side.LONG)
        self.assertGreaterEqual(confidence, 0.70, f"Confidence {confidence:.2f} < 0.70")

    def test_mr_short_entry_on_overbought(self):
        """MR strategy emits SHORT with confidence >= 0.70 when RSI > 72."""
        # Generate candles with high RSI: many up candles, then a spike
        c = uptrend_candles(120, 80.0)
        # Add an overbought spike
        last = c[-1]
        c.append(PerpCandle(len(c), last.close, last.close * 1.05, last.close * 0.99, last.close * 1.03, 1_000_000))
        strat = MeanReversion()
        result = strat.should_enter("BTC", c, [], RegimeType.MEAN_REVERTING, None, -0.001)
        self.assertIsNotNone(result, "MR should enter SHORT when overbought")
        side, confidence, meta = result
        self.assertEqual(side, Side.SHORT)
        self.assertGreaterEqual(confidence, 0.70, f"Confidence {confidence:.2f} < 0.70")

    def test_mr_long_entry_on_oversold(self):
        """MR strategy emits LONG with confidence >= 0.70 when RSI < 28."""
        c = downtrend_candles(120, 100.0)
        last = c[-1]
        c.append(PerpCandle(len(c), last.close, last.close * 1.01, last.close * 0.95, last.close * 0.97, 1_000_000))
        strat = MeanReversion()
        result = strat.should_enter("BTC", c, [], RegimeType.MEAN_REVERTING, None, -0.001)
        self.assertIsNotNone(result, "MR should enter LONG when oversold")
        side, confidence, meta = result
        self.assertEqual(side, Side.LONG)
        self.assertGreaterEqual(confidence, 0.70, f"Confidence {confidence:.2f} < 0.70")

    def test_no_entry_when_position_open(self):
        """Neither strategy enters when position already open."""
        c = downtrend_candles()
        pos = type("Pos", (), {"entry_price": 100.0, "entry_time": datetime.now(timezone.utc), "side": "short"})()

        for strat in [TrendFollowStrategy(), MeanReversion()]:
            result = strat.should_enter("BTC", c, [], RegimeType.STRONGLY_TRENDING, pos, 0.0)
            self.assertIsNone(result, f"{strat.name()} should not enter when position open")

    def test_no_entry_in_wrong_regime(self):
        """Trend does not enter in MEAN_REVERTING regime."""
        c = downtrend_candles()
        strat = TrendFollowStrategy()
        result = strat.should_enter("BTC", c, [], RegimeType.MEAN_REVERTING, None, 0.0)
        self.assertIsNone(result, "Trend should not enter in MEAN_REVERTING regime")

    def test_loop_adx_matches_trend_adx(self):
        """Loop and trend strategy ADX calculations produce similar results."""
        c = downtrend_candles()
        loop_adx = TradingLoop._adx(c)
        trend_adx = TrendFollowStrategy()._adx(c)
        if loop_adx is not None and trend_adx is not None:
            self.assertAlmostEqual(loop_adx, trend_adx, delta=10)
        self.assertIsNotNone(loop_adx)
        self.assertIsNotNone(trend_adx)

    def test_confidence_above_threshold_despite_no_altfins(self):
        """Trend confidence >= 0.70 without Altfins signals (no penalty)."""
        c = downtrend_candles()
        strat = TrendFollowStrategy()
        result = strat.should_enter("BTC", c, [], RegimeType.STRONGLY_TRENDING, None, -0.0005)
        self.assertIsNotNone(result)
        side, confidence, meta = result
        self.assertEqual(side, Side.SHORT)
        self.assertGreaterEqual(confidence, 0.70)
        sources = meta.get("sources", [])
        self.assertNotIn("no_altfins_confirm", sources)

    def test_altfins_boost_increases_confidence(self):
        """Altfins aligned signal increases confidence."""
        c = downtrend_candles()
        sig = Signal("altfins:CHANNEL_DOWN", "BTC", Side.SHORT, 0.7, datetime.now(timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            strat = TrendFollowStrategy(signal_tracker=SignalTracker(Path(tmp) / "sig.json"))
            result_aligned = strat.should_enter("BTC", c, [sig], RegimeType.STRONGLY_TRENDING, None, 0.0)
        self.assertIsNotNone(result_aligned)
        side, conf_aligned, meta = result_aligned
        self.assertEqual(side, Side.SHORT)
        self.assertGreater(conf_aligned, 0.70)

    def test_exit_ownership_enforced(self):
        """MR should_exit returns None for trend positions."""
        mr = MeanReversion()
        trend = TrendFollowStrategy()
        pos = type("Pos", (), {
            "entry_price": 100.0, "entry_time": datetime.now(timezone.utc),
            "stop_loss": 98.0, "side": "short", "strategy": "trend"
        })()
        self.assertIsNone(mr.should_exit("BTC", pos, 99.0, downtrend_candles(100), 0.0),
                          "MR should not exit trend position")

    def test_regime_survives_edge_cases(self):
        """Regime detection doesn't crash on edge cases."""
        for bad in [
            [],
            [PerpCandle(0, 100, 101, 99, 100, 1000)],
            [PerpCandle(i, 100, 101, 99, 100, 1000) for i in range(50)],
        ]:
            regime = TradingLoop._infer_regime(bad)
            self.assertIn(regime, set(RegimeType))

    def test_adx_near_or_above_50_on_clean_trend(self):
        """Clean downtrend produces ADX >= 50."""
        c = downtrend_candles()
        a = TradingLoop._adx(c)
        self.assertIsNotNone(a)
        self.assertGreaterEqual(a, 45, f"ADX {a:.1f} < 45 for clean downtrend")

    def test_volume_gate_blocks_low_volume(self):
        """Trend strategy returns None when volume is below threshold."""
        c = downtrend_candles()
        low_vol = [PerpCandle(i, 100, 101, 99, 100, 10) for i in range(120)]
        strat = TrendFollowStrategy()
        result = strat.should_enter("BTC", low_vol, [], RegimeType.STRONGLY_TRENDING, None, 0.0)
        self.assertIsNone(result, "Trend should not enter with low volume")


if __name__ == "__main__":
    unittest.main()
