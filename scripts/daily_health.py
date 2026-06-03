#!/usr/bin/env python3
"""
Daily behavioral health assessment for Hermes bot.

Checks beyond infrastructure — validates positions, trades, signal attribution,
anomalous patterns. Runs alongside daily_reflection at 00:05 UTC.

Results written to SQLite state key: daily_health
"""

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


def load_json(val):
    try:
        return json.loads(val) if val else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def main(db_path: str):
    db = Path(db_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    health = {
        "date": report_date,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "checks": [],
        "warnings": [],
        "failures": [],
    }

    def check(name: str, passed: bool, detail: str = ""):
        health["checks"].append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            health["passed"] = False
            health["failures"].append(f"{name}: {detail}")

    def warn(name: str, detail: str):
        health["warnings"].append(f"{name}: {detail}")

    # ── 1. Position sanity ─────────────────────────────────────────
    try:
        pos_raw = conn.execute("SELECT value FROM state WHERE key = 'positions'").fetchone()
        if pos_raw:
            positions = load_json(pos_raw["value"])
            for p in positions:
                asset = p.get("asset", "?")
                strat = p.get("strategy", "")
                conf = float(p.get("entry_confidence", 0))
                sl = p.get("stop_loss")
                lev = float(p.get("leverage", 0))

                if not strat:
                    warn(f"position_{asset}", "empty strategy")
                if conf <= 0:
                    warn(f"position_{asset}", f"zero confidence ({conf})")
                if sl is None:
                    warn(f"position_{asset}", "no stop loss")
                if lev <= 0:
                    warn(f"position_{asset}", f"invalid leverage ({lev})")

        check("position_sanity", True,
              f"{len(positions)} positions" if pos_raw else "0 positions")
    except Exception as e:
        check("position_sanity", False, repr(e))

    # ── 2. Trade anomaly detection ──────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, asset, strategy, pnl_pct, pnl_dollars, entry_time, exit_time FROM trades ORDER BY id DESC LIMIT 200"
        ).fetchall()

        if rows:
            total_trades = len(rows)
            if total_trades >= 500:
                check("trade_count", False,
                      f"{total_trades} total — possible flip-loop")
            elif total_trades >= 200:
                warn("trade_count", f"{total_trades} recent trades — high activity")
            else:
                check("trade_count", True, f"{total_trades} total")

            # Per-asset trade counts in last 24h
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            recent = [r for r in rows if str(r["exit_time"] or "") >= cutoff[:19]]

            asset_counts = defaultdict(int)
            asset_no_strategy = defaultdict(int)
            asset_flips = defaultdict(int)
            for r in recent:
                asset = r["asset"]
                asset_counts[asset] += 1
                strat = r["strategy"]
                if not strat:
                    asset_no_strategy[asset] += 1
                # Detect flips: same asset, alternating side
                # (simplified: check if trade count per asset exceeds reasonable rate)
                if asset_counts[asset] >= 10:
                    asset_flips[asset] = asset_counts[asset]

            for asset, count in asset_counts.items():
                if count >= 30:
                    warn(f"rapid_trading_{asset}",
                         f"{count} trades in last 24h — possible flip-loop")
                if asset_no_strategy.get(asset, 0) >= 5:
                    warn(f"no_strategy_trades_{asset}",
                         f"{asset_no_strategy[asset]} trades without strategy attribution")

            # Win rate per strategy
            strat_pnls = defaultdict(list)
            for r in rows:
                strat = r["strategy"] or "unattributed"
                strat_pnls[strat].append(float(r["pnl_dollars"] or 0))

            for s, pnls in sorted(strat_pnls.items()):
                total = sum(pnls)
                wins = sum(1 for p in pnls if p > 0)
                losses = sum(1 for p in pnls if p < 0)
                wr = wins / len(pnls) * 100 if pnls else 0
                if len(pnls) >= 5 and total < -10 and wr < 30:
                    warn(f"strategy_decay_{s}",
                         f"WR={wr:.0f}%, PnL=${total:+.2f} on {len(pnls)} trades — review")
                if s == "unattributed" and len(pnls) >= 5:
                    warn("unattributed_trades",
                         f"{len(pnls)} unattributed trades exist")

            check("trade_quality", len(recent) < 200,
                  f"{len(recent)} trades in last 24h")
        else:
            check("trade_count", True, "0 trades")
    except Exception as e:
        check("trade_quality", False, repr(e))

    # ── 3. Signal attribution ───────────────────────────────────────
    try:
        signal_rows = conn.execute(
            "SELECT key, value FROM state WHERE key LIKE 'last_signal_%'"
        ).fetchall()
        attributed = 0
        total_sigs = len(signal_rows)
        for row in signal_rows:
            sig = load_json(row["value"])
            if sig.get("strategy"):
                attributed += 1
        if total_sigs > 0:
            attribution_rate = attributed / total_sigs * 100
            if attribution_rate < 50:
                warn("signal_attribution",
                     f"only {attributed}/{total_sigs} signals have strategy ({attribution_rate:.0f}%)")
        check("signal_attribution", True,
              f"{attributed}/{total_sigs} signals with strategy")
    except Exception as e:
        check("signal_attribution", False, repr(e))

    # ── 4. Equity consistency ───────────────────────────────────────
    try:
        eq_rows = conn.execute(
            "SELECT equity, timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 10"
        ).fetchall()
        if len(eq_rows) >= 2:
            eqs = [float(r["equity"]) for r in eq_rows]
            if max(eqs) - min(eqs) > 5000:
                warn("equity_volatility",
                     f"swing ${max(eqs)-min(eqs):.0f} across last 10 snapshots")
            if eqs[0] < eqs[-1] * 0.85:
                warn("equity_drop",
                     f"down {((eqs[-1]-eqs[0])/eqs[-1]*100):.1f}% in 10 cycles")
        check("equity_consistency", True,
              f"{len(eq_rows)} snapshots available")
    except Exception as e:
        check("equity_consistency", False, repr(e))

    # ── 5. Service and DB freshness ─────────────────────────────────
    try:
        snap = conn.execute(
            "SELECT timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if snap:
            ts = datetime.fromisoformat(snap["timestamp"])
            age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
            fresh = age_sec < 300
            check("db_freshness", fresh,
                  f"{age_sec:.0f}s since last snapshot (threshold: 300s)")
    except Exception as e:
        check("db_freshness", False, repr(e))

    # ── 6. Daily reflection also ran ────────────────────────────────
    try:
        dr_raw = conn.execute(
            "SELECT value FROM state WHERE key = 'daily_reflection'"
        ).fetchone()
        if dr_raw:
            dr = load_json(dr_raw["value"])
            dr_date = dr.get("date", "")
            if dr_date == report_date:
                check("daily_reflection_ran", True,
                      f"reflection completed for {report_date}")
            else:
                warn("daily_reflection_stale",
                     f"last reflection: {dr_date}, today: {report_date}")
        else:
            warn("daily_reflection_stale", "never ran")
    except Exception as e:
        check("daily_reflection_ran", False, repr(e))

    # ── Write results ───────────────────────────────────────────────
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                 ("daily_health", json.dumps(health, default=str)))
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                 (f"daily_health_{report_date}", json.dumps(health, default=str)))
    conn.commit()
    conn.close()

    status = "PASS" if health["passed"] else "FAIL"
    print(f"Daily health {report_date}: {status}")
    for w in health["warnings"]:
        print(f"  WARN: {w}")
    for f in health["failures"]:
        print(f"  FAIL: {f}")
    print(f"  Checks: {len(health['checks'])} | Warnings: {len(health['warnings'])} | Failures: {len(health['failures'])}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: daily_health.py <db_path>")
        sys.exit(1)
    main(sys.argv[1])
