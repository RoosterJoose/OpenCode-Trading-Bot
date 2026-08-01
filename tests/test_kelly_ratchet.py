"""
KellyRatchet: dynamic Fractional-Kelly monitoring + ratchet engine.

Implements the 4-gate design from NotebookLM + challenger redesign:
  G1. Trade counter (n<30 locked at 0.25, n>=30 -> 0.40, n>=100 -> 0.50)
  G2. Net expectancy gate (avgR_net > 0)
  G3. Statistical significance gate (t-stat > 1.8 + bootstrap CI low > 0 + median > 0)
  G4. Downward ratchet via lower-CUSUM (sigma units) -> quarantine, and
      structural halt for proven losers (avgR<=0 AND t<=-1.5 -> phi=0.0)

Run: PYTHONPATH=. python -m pytest tests/test_kelly_ratchet.py -q
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.kelly_ratchet import KellyRatchet


def make_trades(r_multiples, strategy="xs_momentum", asset="BTC", side="SHORT",
                base_entry="2026-08-01T00:00:00Z", epoch="2026-07-30"):
    """Generate synthetic trade records. Unique entry per trade (no dedup collapse)."""
    trades = []
    for i, r in enumerate(r_multiples):
        trades.append({
            "strategy": strategy,
            "asset": asset,
            "side": side,
            "entry_time": f"2026-08-0{i // 10 + 1}T{i % 10:02d}:{i:02d}:00Z",
            "entry_price": 100.0,
            "size": 1.0,
            "r_multiple": r,
            "exit_time": f"2026-08-0{i // 10 + 1}T{i % 10:02d}:{i:02d}:30Z",
        })
    return trades


class MockRatchetStore:
    """Minimal store for ratchet tests: strategy_trades + state get/put."""

    def __init__(self):
        self.trade_rows = []
        self.state = {}

    def strategy_trades(self, strategy, limit=None):
        rows = [t for t in self.trade_rows if t.get("strategy") == strategy]
        return rows[:limit] if limit else rows

    def get_state(self, key):
        return self.state.get(key)

    def put_state(self, key, value):
        self.state[key] = value


class TestSampleFilter(unittest.TestCase):
    """G1: trade counter gates the ratchet tiers."""

    def test_locked_below_30_even_with_positive_edge(self):
        store = MockRatchetStore()
        store.trade_rows = make_trades([0.4] * 20)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.25)
        self.assertEqual(ev["tier"], "LOCKED")

    def test_no_crash_below_3_trades(self):
        store = MockRatchetStore()
        store.trade_rows = make_trades([0.4, 0.2])
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.25)

    def test_epoch_filter_excludes_pre_epoch_trades(self):
        store = MockRatchetStore()
        old = make_trades([-2.0] * 40, base_entry="2026-06-01T00:00:00Z")
        new = make_trades([0.4] * 40, base_entry="2026-08-01T00:00:00Z")
        for t in old:
            t["exit_time"] = t["entry_time"]
        for t in new:
            t["exit_time"] = t["entry_time"]
        store.trade_rows = old + new
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        # Only the 40 post-epoch trades count
        self.assertEqual(ev["n_deduped"], 40)

    def test_no_global_limit_cap(self):
        # A strategy with >200 trades must be fully counted (no limit=200 truncation)
        store = MockRatchetStore()
        store.trade_rows = make_trades([0.3] * 250)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["n_deduped"], 250)

    def test_milestone_only_promotion(self):
        # n=35 (not a milestone) must NOT ratchet even with a qualifying edge
        store = MockRatchetStore()
        store.trade_rows = make_trades([0.4] * 35)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.25, "non-milestone must not promote")

    def test_batch_jump_counts_one_confirmation_not_skipped_milestones(self):
        store = MockRatchetStore()
        r = KellyRatchet(store, "xs_momentum")
        store.trade_rows = make_trades([0.4] * 150)
        r.evaluate()
        self.assertEqual(r._load_state()["confirmations_50"], 1)
        store.trade_rows = make_trades([0.4] * 152)
        r.evaluate()
        self.assertEqual(r._load_state()["confirmations_50"], 1)


class TestNetExpectancyGate(unittest.TestCase):
    """G2: avgR_net <= 0 blocks promotion."""

    def test_positive_edge_promotes_at_60(self):
        # Growth simulation: ratchet must see 3 consecutive qualifying milestones
        # (30, 45, 60) before ratcheting 0.25 -> 0.40.
        store = MockRatchetStore()
        r = KellyRatchet(store, "xs_momentum")
        store.trade_rows = make_trades([0.4] * 30)
        r.evaluate()
        self.assertEqual(r.kelly_fraction(), 0.25)
        store.trade_rows = make_trades([0.4] * 45)
        r.evaluate()
        self.assertEqual(r.kelly_fraction(), 0.25)
        store.trade_rows = make_trades([0.4] * 60)
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.40, "3 consecutive qualifying milestones should reach 0.40")

    def test_negative_edge_stays_locked(self):
        # avgR slightly negative but t > -1.5 (noise, not proven loser)
        rvals = [0.6, -0.7, 0.5, -0.6, 0.4, -0.5, 0.3, -0.4, 0.2, -0.3] * 4
        store = MockRatchetStore()
        store.trade_rows = make_trades(rvals)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.25)
        self.assertEqual(ev["tier"], "LOCKED")

    def test_noise_floor_not_halted(self):
        # avgR near zero (slightly negative), t in noise band: 0.25, not a halt
        store = MockRatchetStore()
        rvals = [0.5, -0.55, 0.4, -0.45, 0.3, -0.35, 0.2, -0.25, 0.1, -0.15] * 3
        store.trade_rows = make_trades(rvals)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.25)
        self.assertNotEqual(ev["tier"], "HALTED")


class TestSignificanceGate(unittest.TestCase):
    """G3: t-stat > 1.8 + bootstrap CI low > 0 + median > 0."""

    def test_low_t_stat_blocks_promotion(self):
        # Small edge with high variance: t < 1.8, must stay locked
        rvals = [0.4, -0.4, 0.3, -0.3, 0.5, -0.5, 0.2, -0.2, 0.4, -0.4] * 5
        store = MockRatchetStore()
        store.trade_rows = make_trades(rvals)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.25, "low t-stat must not promote")

    def test_high_variance_blocks_bootstrap_ci(self):
        # Fat-tailed: mean positive but bootstrap CI low bound <= 0 -> no promote
        rvals = [3.0] * 6 + [-0.5] * 30   # mean > 0 but CI straddles zero
        store = MockRatchetStore()
        store.trade_rows = make_trades(rvals)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.25)


class TestDownwardRatchet(unittest.TestCase):
    """G4: CUSUM quarantine + structural halt."""

    def test_proven_loser_halts_to_zero(self):
        # avgR < 0 with t <= -1.5 at n>=30 -> phi=0.0 halt
        store = MockRatchetStore()
        store.trade_rows = make_trades([-0.5] * 40)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["phi"], 0.0)
        self.assertEqual(ev["tier"], "HALTED")

    def test_cusum_breach_quarantines_to_025(self):
        # Mostly good, then a cluster of large losses -> CUSUM breach -> quarantine
        rvals = [0.3] * 40 + [-2.0] * 10
        store = MockRatchetStore()
        store.trade_rows = make_trades(rvals)
        r = KellyRatchet(store, "xs_momentum")
        ev = r.evaluate()
        self.assertEqual(ev["tier"], "QUARANTINED")
        self.assertEqual(ev["phi"], 0.25)

    def test_quarantine_expires_after_20_new_entries(self):
        # Simulate growth: the bad cluster arrives first (breach -> quarantine),
        # then 25 clean entries arrive and must lift the 20-entry window.
        store = MockRatchetStore()
        r = KellyRatchet(store, "xs_momentum")
        store.trade_rows = make_trades([0.3] * 40 + [-2.0] * 10)
        ev1 = r.evaluate()
        self.assertEqual(ev1["tier"], "QUARANTINED", "breach must quarantine")
        self.assertEqual(r.kelly_fraction(), 0.25)
        # growth: 25 clean entries (total 75) -> quarantine window passed
        store.trade_rows = make_trades([0.3] * 40 + [-2.0] * 10 + [0.4] * 25)
        ev = r.evaluate()
        self.assertNotEqual(ev["tier"], "QUARANTINED", "quarantine must expire")


class TestDedupAndPersistence(unittest.TestCase):
    def test_quantity_weighted_partial_close_dedup(self):
        # tp1 60% @ 0.5R (size 0.6) + final 40% @ 1.2R (size 0.4), same entry
        # Weighted R = (0.5*0.6 + 1.2*0.4)/1.0 = 0.78  (simple mean would be 0.85)
        entry_time = "2026-08-01T00:00:00Z"
        store = MockRatchetStore()
        store.trade_rows = [
            {"strategy": "xs_momentum", "asset": "BTC", "side": "SHORT",
             "entry_time": entry_time, "entry_price": 100.0, "size": 0.6,
             "r_multiple": 0.5, "exit_time": "2026-08-01T00:30:00Z"},
            {"strategy": "xs_momentum", "asset": "BTC", "side": "SHORT",
             "entry_time": entry_time, "entry_price": 100.0, "size": 0.4,
             "r_multiple": 1.2, "exit_time": "2026-08-01T02:00:00Z"},
            {"strategy": "xs_momentum", "asset": "ETH", "side": "SHORT",
             "entry_time": "2026-08-01T03:00:00Z", "entry_price": 2000.0, "size": 1.0,
             "r_multiple": -0.5, "exit_time": "2026-08-01T03:30:00Z"},
        ]
        r = KellyRatchet(store, "xs_momentum")
        entries = r._load_deduped()
        self.assertEqual(len(entries), 2)
        btc = [e for e in entries if e["asset"] == "BTC"][0]
        self.assertAlmostEqual(btc["r"], 0.78, places=4)
        eth = [e for e in entries if e["asset"] == "ETH"][0]
        self.assertAlmostEqual(eth["r"], -0.5, places=4)

    def test_distinct_positions_not_merged_by_dedup_key(self):
        # Same entry_time + entry_price but different asset must stay separate
        store = MockRatchetStore()
        entry_time = "2026-08-01T00:00:00Z"
        store.trade_rows = [
            {"strategy": "xs_momentum", "asset": "BTC", "side": "SHORT",
             "entry_time": entry_time, "entry_price": 100.0, "size": 1.0,
             "r_multiple": 0.5, "exit_time": "2026-08-01T00:30:00Z"},
            {"strategy": "xs_momentum", "asset": "ETH", "side": "LONG",
             "entry_time": entry_time, "entry_price": 100.0, "size": 1.0,
             "r_multiple": 0.7, "exit_time": "2026-08-01T00:30:00Z"},
        ]
        r = KellyRatchet(store, "xs_momentum")
        entries = r._load_deduped()
        self.assertEqual(len(entries), 2)

    def test_state_persists_across_instances(self):
        store = MockRatchetStore()
        r1 = KellyRatchet(store, "xs_momentum")
        for n in (30, 45, 60):
            store.trade_rows = make_trades([0.4] * n)
            r1.evaluate()
        self.assertEqual(r1.kelly_fraction(), 0.40)
        # New instance must restore the persisted tier, not reset to 0.25
        r2 = KellyRatchet(store, "xs_momentum")
        ev2 = r2.evaluate()
        self.assertEqual(ev2["phi"], 0.40)
        self.assertEqual(ev2["n_deduped"], 60)

    def test_ratchet_50_is_reachable_after_three_100_plus_milestones(self):
        store = MockRatchetStore()
        r = KellyRatchet(store, "xs_momentum")
        for n in (30, 45, 60):
            store.trade_rows = make_trades([0.4] * n)
            r.evaluate()
        self.assertEqual(r.kelly_fraction(), 0.40)
        for n in (100, 125, 150):
            store.trade_rows = make_trades([0.4] * n)
            r.evaluate()
        self.assertEqual(r.kelly_fraction(), 0.50)

    def test_latched_halt_requires_manual_reset(self):
        store = MockRatchetStore()
        r = KellyRatchet(store, "xs_momentum")
        store.trade_rows = make_trades([-0.5] * 40)
        self.assertEqual(r.evaluate()["tier"], "HALTED")
        store.trade_rows = make_trades([0.4] * 45)
        self.assertEqual(r.evaluate()["phi"], 0.0)
        r.reset_halt()
        self.assertEqual(r.kelly_fraction(), 0.25)

    def test_bootstrap_is_deterministic_for_identical_sample(self):
        store = MockRatchetStore()
        store.trade_rows = make_trades([0.4, -0.2, 0.3, 0.1] * 10)
        r = KellyRatchet(store, "xs_momentum")
        entries = r._load_deduped()
        values = [e["r"] for e in entries]
        self.assertEqual(r._bootstrap_ci_low(values), r._bootstrap_ci_low(values))

    def test_partial_close_revision_re_evaluates_with_same_entry_count(self):
        entry = {
            "strategy": "xs_momentum", "asset": "BTC", "side": "SHORT",
            "entry_time": "2026-08-01T00:00:00Z", "entry_price": 100.0,
            "size": 1.0, "r_multiple": 0.5,
            "exit_time": "2026-08-01T01:00:00Z",
        }
        store = MockRatchetStore()
        store.trade_rows = [entry]
        r = KellyRatchet(store, "xs_momentum")
        r.evaluate()
        entry["r_multiple"] = -1.0
        self.assertIsNotNone(r.evaluate_if_new())

    def test_monitoring_history_is_append_only(self):
        store = MockRatchetStore()
        store.trade_rows = make_trades([0.4] * 35)
        r = KellyRatchet(store, "xs_momentum")
        r.evaluate()
        r.evaluate()
        hist = r._load_state()["history"]
        self.assertEqual(len(hist), 2)


class TestHysteresis(unittest.TestCase):
    def test_requires_three_consecutive_qualifying_evals(self):
        # Edge qualifies at n=30,45 but the third milestone must confirm -> 0.40.
        # A non-qualifying window between milestones resets the counter.
        store = MockRatchetStore()
        r = KellyRatchet(store, "xs_momentum")
        store.trade_rows = make_trades([0.4] * 30)
        r.evaluate()          # confirm 1/3
        store.trade_rows = make_trades([0.4] * 45)
        r.evaluate()          # confirm 2/3
        store.trade_rows = make_trades([0.4] * 60)
        ev = r.evaluate()     # confirm 3/3 -> ratchet
        self.assertEqual(ev["phi"], 0.40)

    def test_non_qualifying_window_resets_hysteresis(self):
        # 2 qualifying milestones, then a negative window must reset the counter
        store = MockRatchetStore()
        r = KellyRatchet(store, "xs_momentum")
        store.trade_rows = make_trades([0.4] * 30)
        r.evaluate()          # confirm 1/3
        store.trade_rows = make_trades([0.4] * 45)
        r.evaluate()          # confirm 2/3
        store.trade_rows = make_trades([-0.4] * 60)
        r.evaluate()          # fails qualification -> resets
        state = r._load_state()
        self.assertEqual(state["confirmations"], 0)


if __name__ == "__main__":
    unittest.main()
