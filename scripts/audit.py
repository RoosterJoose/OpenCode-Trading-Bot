"""Hermes runtime audit.

Fail-closed checks for code health, live APIs, DB freshness, dashboard APIs,
and key trading invariants. Designed to run after deploy and from cron/systemd.
"""

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.adapters.hyperliquid import HyperliquidAdapter
from src.adapters.paper_perp import PaperPerpExchange
from src.core.perp_risk import PerpRiskManager
from src.core.types import Order, OrderType, PerpCandle, PerpPosition, Side
from src.strategies.mr import MeanReversion


def ok(name: str, detail: str = ""):
    print(f"PASS {name}{': ' + detail if detail else ''}")


def fail(failures: list[str], name: str, detail: str):
    failures.append(f"{name}: {detail}")
    print(f"FAIL {name}: {detail}")


def check_compile(failures: list[str]):
    result = subprocess.run([sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests"], cwd=ROOT)
    if result.returncode == 0:
        ok("compile")
    else:
        fail(failures, "compile", f"exit={result.returncode}")


def check_invariants(failures: list[str]):
    candles = [PerpCandle(i, 100, 101, 99, 100, 1_000_000) for i in range(80)]
    mr = MeanReversion()
    pos = PerpPosition("BTC", Side.LONG, 100.0, 1.0, stop_loss=98.5, entry_time=datetime.now(timezone.utc))
    if mr.should_exit("BTC", pos, 100.0, candles, 0.0) is None:
        ok("mr_stop_invariant")
    else:
        fail(failures, "mr_stop_invariant", "MR stop exits above stop")

    risk = PerpRiskManager(initial_equity=10_000)
    qty, _, _ = risk.position_size("BTC", 10_000, 1.5, 100.0, current_gross_exposure=29_950)
    if qty <= 0.5:
        ok("post_trade_exposure_cap")
    else:
        fail(failures, "post_trade_exposure_cap", f"qty={qty}")

    async def paper():
        ex = PaperPerpExchange(10_000)
        ex.update_price("BTC", 100.0)
        await ex.place_order(Order("BTC", Side.LONG, OrderType.MARKET, 1.0, stop_price=98.5, leverage=2.0))
        return ex.positions["BTC"]

    p = asyncio.run(paper())
    if p.leverage == 2.0 and p.stop_loss == 98.5:
        ok("paper_leverage_stop")
    else:
        fail(failures, "paper_leverage_stop", f"lev={p.leverage} stop={p.stop_loss}")


async def check_hyperliquid(failures: list[str]):
    hl = HyperliquidAdapter()
    try:
        mids = await hl.fetch_all_mids()
        candles = await hl.fetch_candles("BTC", "1h", 5)
        funding = await hl.fetch_funding()
        oi = await hl.fetch_open_interest()
        meta = await hl.fetch_metadata()
        checks = {
            "mids": len(mids) > 0,
            "candles": len(candles) > 0,
            "funding": "BTC" in funding,
            "oi": "BTC" in oi,
            "meta": "BTC" in meta,
        }
        for name, passed in checks.items():
            ok(f"hyperliquid_{name}") if passed else fail(failures, f"hyperliquid_{name}", "missing data")
    except Exception as e:
        fail(failures, "hyperliquid", repr(e))
    finally:
        await hl.close()


def check_db(failures: list[str], db_path: Path):
    if not db_path.exists():
        fail(failures, "db", f"missing {db_path}")
        return
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            fail(failures, "db_snapshot", "no equity snapshots")
            return
        ts = datetime.fromisoformat(row["timestamp"])
        age = (datetime.now(timezone.utc).replace(tzinfo=None) - ts).total_seconds() / 60
        if age <= 6:
            ok("db_snapshot_fresh", f"{age:.1f} min")
        else:
            fail(failures, "db_snapshot_fresh", f"{age:.1f} min old")
    finally:
        conn.close()


def check_dashboard(failures: list[str], base_url: str):
    for path in ["/api/status", "/api/positions", "/api/trades", "/api/signals", "/api/markets", "/api/reflection", "/api/readiness", "/api/intents"]:
        try:
            raw = urllib.request.urlopen(base_url + path, timeout=8).read().decode()
            json.loads(raw)
            ok(f"dashboard_{path}")
        except Exception as e:
            fail(failures, f"dashboard_{path}", repr(e))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "hermes.db")
    parser.add_argument("--dashboard", default="http://127.0.0.1:8081")
    parser.add_argument("--skip-dashboard", action="store_true")
    parser.add_argument("--skip-db", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    check_compile(failures)
    check_invariants(failures)
    asyncio.run(check_hyperliquid(failures))
    if not args.skip_db:
        check_db(failures, args.db)
    if not args.skip_dashboard:
        check_dashboard(failures, args.dashboard)

    if failures:
        print("\nAUDIT FAILED")
        for item in failures:
            print(f"- {item}")
        raise SystemExit(1)
    print("\nAUDIT PASSED")


if __name__ == "__main__":
    main()
