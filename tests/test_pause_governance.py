"""
Tests for the hardwired auto-pause / sharpe-tracker logic.

Covers the composite-latch design and the four bugs it fixes:
  G1 - bot_paused serialization (bare literal `true` -> bool != "true")
  G2 - stale paused_strategies never cleared (only written when non-empty)
  D  - aggregate-Sharpe must NOT be a global circuit breaker
  C2 - sma200_perp immune to per-strategy pause (sharpe 0.0)

Run: PYTHONPATH=. python3 -m pytest tests/test_pause_governance.py -q
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.sharpe_tracker import DEAD_STRATEGIES, _state_truthy, _read_state_bool


class TestStateTruthiness(unittest.TestCase):
    """G1: every historical serialization form must decode to the same truth."""

    def test_bare_json_literal_true_is_truthy(self):
        # OLD sharpe_tracker wrote the bare literal `true` (json bool)
        self.assertTrue(_state_truthy(json.loads("true")))

    def test_quoted_json_string_is_truthy(self):
        # put_state("bot_paused", "true") -> json.dumps -> '"true"'
        self.assertTrue(_state_truthy(json.loads('"true"')))

    def test_raw_quoted_string_is_truthy(self):
        # status_report raw SQL '"true"'
        self.assertTrue(_state_truthy('"true"'))

    def test_false_forms_are_not_truthy(self):
        self.assertFalse(_state_truthy(json.loads("false")))
        self.assertFalse(_state_truthy(json.loads('"false"')))
        self.assertFalse(_state_truthy('"false"'))
        self.assertFalse(_state_truthy(""))


class TestReadStateBool(unittest.TestCase):
    def test_missing_key_returns_false(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE state (key TEXT, value TEXT)")
        self.assertFalse(_read_state_bool(conn, "manual_pause"))

    def test_reads_stored_latches(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE state (key TEXT, value TEXT)")
        conn.execute("INSERT INTO state VALUES ('manual_pause', ?)", (json.dumps("true"),))
        conn.execute("INSERT INTO state VALUES ('watchdog_pause', ?)", (json.dumps("false"),))
        self.assertTrue(_read_state_bool(conn, "manual_pause"))
        self.assertFalse(_read_state_bool(conn, "watchdog_pause"))


class TestDeadStrategyExclusion(unittest.TestCase):
    """A: dead legacy sleeves must never feed the pause aggregate."""

    def test_dead_names_present(self):
        self.assertIn("mr", DEAD_STRATEGIES)
        self.assertIn("donchian", DEAD_STRATEGIES)
        self.assertIn("trend", DEAD_STRATEGIES)
        self.assertIn("HermesPerpStrategy", DEAD_STRATEGIES)
        self.assertIn("manual", DEAD_STRATEGIES)

    def test_live_names_absent(self):
        self.assertNotIn("xs_momentum", DEAD_STRATEGIES)
        self.assertNotIn("sma200_perp", DEAD_STRATEGIES)
        self.assertNotIn("trend_4h", DEAD_STRATEGIES)
        self.assertNotIn("fade_5m", DEAD_STRATEGIES)


class TestPauseWritesIntegration(unittest.TestCase):
    """End-to-end: run sharpe_tracker main() against a temp DB and verify
    every state key it writes (composite latch, always-write, pause_reasons)."""

    def _seed_db(self, path):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, strategy TEXT, entry_time TEXT, exit_time TEXT, pnl_dollars REAL)")
        conn.execute("CREATE TABLE state (key TEXT, value TEXT)")
        conn.execute("CREATE TABLE equity_snapshots (id INTEGER PRIMARY KEY, equity REAL, timestamp TEXT)")
        # equity baseline
        conn.execute("INSERT INTO equity_snapshots VALUES (1, 5000.0, '2026-08-01 00:00:00')")
        # healthy XS momentum
        for i in range(25):
            day = i % 9 + 1
            conn.execute(
                "INSERT INTO trades (strategy, entry_time, exit_time, pnl_dollars) VALUES (?, ?, ?, ?)",
                ("xs_momentum", f"2026-07-2{day}T00:00:00Z", f"2026-07-2{day}T01:00:00Z", 3.0),
            )
        # bleeding sma200_perp (35 trades, -$300)
        for i in range(35):
            day = i % 9 + 1
            conn.execute(
                "INSERT INTO trades (strategy, entry_time, exit_time, pnl_dollars) VALUES (?, ?, ?, ?)",
                ("sma200_perp", f"2026-07-2{day}T00:00:00Z", f"2026-07-2{day}T01:00:00Z", -8.0),
            )
        # dead sleeve pollution
        conn.execute(
            "INSERT INTO trades (strategy, entry_time, exit_time, pnl_dollars) VALUES (?, ?, ?, ?)",
            ("mr", "2026-07-20T00:00:00Z", "2026-07-20T01:00:00Z", -200.0),
        )
        conn.commit()
        conn.close()

    def _run_tracker(self, path):
        import io
        from contextlib import redirect_stdout
        from scripts.sharpe_tracker import main
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(path)
        return buf.getvalue()

    def test_writes_composite_latches(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.db")
            self._seed_db(path)
            self._run_tracker(path)
            conn = sqlite3.connect(path)
            for key in ("circuit_breaker_pause", "bot_paused", "pause_reasons", "paused_strategies"):
                row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
                self.assertIsNotNone(row, f"{key} must be written")
            conn.close()

    def test_always_writes_paused_strategies_even_empty(self):
        # Force a healthy-only DB (no bleeding sleeve): paused_strategies must
        # still be written as [] so stale pauses clear.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, strategy TEXT, entry_time TEXT, exit_time TEXT, pnl_dollars REAL)")
            conn.execute("CREATE TABLE state (key TEXT, value TEXT)")
            conn.execute("CREATE TABLE equity_snapshots (id INTEGER PRIMARY KEY, equity REAL, timestamp TEXT)")
            conn.execute("INSERT INTO equity_snapshots VALUES (1, 5000.0, '2026-08-01 00:00:00')")
            for i in range(25):
                day = i % 9 + 1
                conn.execute(
                    "INSERT INTO trades (strategy, entry_time, exit_time, pnl_dollars) VALUES (?, ?, ?, ?)",
                    ("xs_momentum", f"2026-07-2{day}T00:00:00Z", f"2026-07-2{day}T01:00:00Z", 3.0),
                )
            conn.commit()
            conn.close()
            self._run_tracker(path)
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT value FROM state WHERE key='paused_strategies'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(json.loads(row[0]), [])
            conn.close()

    def test_dead_sleeve_pnl_excluded_from_report(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.db")
            self._seed_db(path)
            out = self._run_tracker(path)
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT value FROM state WHERE key='sharpe_30d'").fetchone()
            report = json.loads(row[0])
            by_strategy = report["by_strategy"]
            # mr must not appear in the per-strategy report
            self.assertNotIn("mr", by_strategy)
            self.assertIn("xs_momentum", by_strategy)
            self.assertIn("sma200_perp", by_strategy)
            conn.close()

    def test_sma200_bleeder_gets_paused_but_xs_stays(self):
        # sma200_perp: -$300 / $5000 = -6% < -5% threshold, 35 trades -> paused.
        # xs_momentum: +$75 net, positive -> NOT paused.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.db")
            self._seed_db(path)
            self._run_tracker(path)
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT value FROM state WHERE key='paused_strategies'").fetchone()
            paused = json.loads(row[0])
            self.assertIn("sma200_perp", paused)
            self.assertNotIn("xs_momentum", paused)
            conn.close()

    def test_sharpe_not_a_global_circuit_breaker(self):
        # Negative aggregate Sharpe alone must NOT set circuit_breaker_pause.
        # Only equity/risk breakers (daily loss, weekly DD, 30d DD) do.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, strategy TEXT, entry_time TEXT, exit_time TEXT, pnl_dollars REAL)")
            conn.execute("CREATE TABLE state (key TEXT, value TEXT)")
            conn.execute("CREATE TABLE equity_snapshots (id INTEGER PRIMARY KEY, equity REAL, timestamp TEXT)")
            conn.execute("INSERT INTO equity_snapshots VALUES (1, 5000.0, '2026-08-01 00:00:00')")
            # small losses each day — aggregate Sharpe negative, but no single
            # day > 2% and no week > 5%
            for i in range(20):
                day = i + 1
                conn.execute(
                    "INSERT INTO trades (strategy, entry_time, exit_time, pnl_dollars) VALUES (?, ?, ?, ?)",
                    ("xs_momentum", f"2026-07-{day:02d}T00:00:00Z", f"2026-07-{day:02d}T01:00:00Z", -1.0),
                )
            conn.commit()
            conn.close()
            self._run_tracker(path)
            conn = sqlite3.connect(path)
            cb = conn.execute("SELECT value FROM state WHERE key='circuit_breaker_pause'").fetchone()
            self.assertEqual(json.loads(cb[0]), "false")
            paused = conn.execute("SELECT value FROM state WHERE key='paused_strategies'").fetchone()
            self.assertEqual(json.loads(paused[0]), [])
            conn.close()


if __name__ == "__main__":
    unittest.main()
