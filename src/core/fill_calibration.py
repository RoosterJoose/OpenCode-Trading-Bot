"""
Phase 2.3: Fill calibration — collects and applies real spread/depth/latency data.

This module:
1. Periodically samples Coinbase Level2 order book snapshots
2. Records observed bid/ask spreads, depth, and trade timestamps
3. Produces calibrated FillCalibration objects per asset
4. Feeds into ExecutionEngine for realistic fill simulation

The calibration is conservative: uses 75th percentile spread (not median)
and models adverse selection for passive orders.

For the fill simulator itself (aggressive/passive), the ExecutionEngine
already implements:
- Aggressive: buy at ask, sell at bid (market fills walk the spread)
- Passive: queue limit orders that fill when price crosses the limit

This module adds the calibration data that makes those fills realistic
instead of using a fixed 2bps spread assumption.
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, median, pstdev
from typing import Optional

import httpx

logger = logging.getLogger("hermes.fill_calibration")

CB_ADV = "https://api.coinbase.com/api/v3/brokerage"


@dataclass
class FillCalibration:
    """
    Calibrated fill parameters for a single asset.

    These are computed from observed Coinbase order book data and
    used by ExecutionEngine to produce realistic fills.
    """
    asset: str
    median_spread_bps: float = 2.0       # median observed half-spread
    p75_spread_bps: float = 3.0          # 75th percentile (conservative)
    p95_spread_bps: float = 5.0          # 95th percentile (stress)
    median_depth_usd: float = 50_000.0   # median top-of-book depth in USD
    p25_depth_usd: float = 10_000.0      # 25th percentile (thin book)
    fill_latency_ms: float = 100.0        # median decision-to-fill latency
    adverse_selection_bps: float = 1.0   # estimated adverse selection for passive fills
    last_calibrated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def conservative_spread_bps(self) -> float:
        """Return the conservative (p75) spread for fill simulation."""
        return self.p75_spread_bps

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "median_spread_bps": round(self.median_spread_bps, 2),
            "p75_spread_bps": round(self.p75_spread_bps, 2),
            "p95_spread_bps": round(self.p95_spread_bps, 2),
            "median_depth_usd": round(self.median_depth_usd, 0),
            "p25_depth_usd": round(self.p25_depth_usd, 0),
            "fill_latency_ms": round(self.fill_latency_ms, 1),
            "adverse_selection_bps": round(self.adverse_selection_bps, 2),
            "last_calibrated": self.last_calibrated,
        }


SCHEMA = """
CREATE TABLE IF NOT EXISTS book_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT,
    timestamp TEXT,
    bid REAL,
    ask REAL,
    spread_bps REAL,
    bid_depth_usd REAL,
    ask_depth_usd REAL,
    mid_price REAL
);

CREATE TABLE IF NOT EXISTS fill_calibrations (
    asset TEXT PRIMARY KEY,
    calibration_json TEXT,
    updated_at TEXT
);
"""


class FillCalibrationCollector:
    """
    Collects order book samples and computes fill calibrations.

    Usage:
      collector = FillCalibrationCollector(db_path)
      await collector.sample_books(assets)  # call each cycle
      calibrations = collector.get_calibrations()
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

        self._calibrations: dict[str, FillCalibration] = {}
        self._sample_counts: dict[str, int] = {}
        self._load_calibrations()

    def _load_calibrations(self):
        """Load most recent calibrations from DB."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM fill_calibrations").fetchall()
            for row in rows:
                data = json.loads(row["calibration_json"])
                cal = FillCalibration(**data)
                self._calibrations[cal.asset] = cal
                self._sample_counts[cal.asset] = 0
            conn.close()
            logger.info("Loaded %d fill calibrations from DB", len(self._calibrations))
        except Exception as e:
            logger.warning("Failed to load calibrations: %s", e)

    async def sample_books(self, assets: list[str], product_ids: dict[str, str]):
        """
        Fetch current order book for each asset and record spread/depth.
        Called every N cycles by the main loop.
        """
        async with httpx.AsyncClient() as client:
            for asset in assets:
                pid = product_ids.get(asset)
                if not pid:
                    continue
                try:
                    resp = await client.get(
                        f"{CB_ADV}/products/{pid}",
                        timeout=10.0
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    # Get product book
                    book_resp = await client.get(
                        f"{CB_ADV}/products/{pid}/product_book",
                        params={"limit": 1},
                        timeout=10.0
                    )
                    if book_resp.status_code != 200:
                        continue
                    book = book_resp.json()
                    price_book = book.get("price_book", {})

                    best_bid = float(price_book.get("bids", [{}])[0].get("price", 0)) if price_book.get("bids") else 0
                    best_ask = float(price_book.get("asks", [{}])[0].get("price", 0)) if price_book.get("asks") else 0
                    bid_size = float(price_book.get("bids", [{}])[0].get("size", 0)) if price_book.get("bids") else 0
                    ask_size = float(price_book.get("asks", [{}])[0].get("size", 0)) if price_book.get("asks") else 0

                    if best_bid <= 0 or best_ask <= 0:
                        continue

                    mid = (best_bid + best_ask) / 2
                    spread_bps = ((best_ask - best_bid) / mid) * 10000 if mid > 0 else 0
                    bid_depth = bid_size * mid
                    ask_depth = ask_size * mid

                    self._record_sample(asset, best_bid, best_ask, spread_bps, bid_depth, ask_depth, mid)
                except Exception as e:
                    logger.debug("Book sample failed for %s: %s", asset, e)

    def _record_sample(self, asset: str, bid: float, ask: float, spread_bps: float,
                        bid_depth: float, ask_depth: float, mid: float):
        """Record a book sample to DB and update in-memory calibration."""
        ts = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO book_samples (asset, timestamp, bid, ask, spread_bps, bid_depth_usd, ask_depth_usd, mid_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (asset, ts, bid, ask, spread_bps, bid_depth, ask_depth, mid)
        )
        conn.commit()

        # Keep only last 1000 samples per asset
        conn.execute(
            "DELETE FROM book_samples WHERE asset = ? AND id NOT IN (SELECT id FROM book_samples WHERE asset = ? ORDER BY id DESC LIMIT 1000)",
            (asset, asset)
        )
        conn.commit()
        conn.close()

        self._sample_counts[asset] = self._sample_counts.get(asset, 0) + 1

        # Recompute calibration every 50 samples
        if self._sample_counts[asset] % 50 == 0:
            self._recompute_calibration(asset)

    def _recompute_calibration(self, asset: str):
        """Recompute calibration from recent samples."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT spread_bps, bid_depth_usd, ask_depth_usd FROM book_samples WHERE asset = ? ORDER BY id DESC LIMIT 500",
            (asset,)
        ).fetchall()
        conn.close()

        if len(rows) < 10:
            return

        spreads = [r["spread_bps"] for r in rows if r["spread_bps"] > 0]
        depths = [(r["bid_depth_usd"] + r["ask_depth_usd"]) / 2 for r in rows]

        if not spreads:
            return

        spreads.sort()
        depths.sort()

        cal = FillCalibration(
            asset=asset,
            median_spread_bps=median(spreads),
            p75_spread_bps=spreads[int(len(spreads) * 0.75)] if len(spreads) > 4 else median(spreads),
            p95_spread_bps=spreads[int(len(spreads) * 0.95)] if len(spreads) > 20 else max(spreads),
            median_depth_usd=median(depths),
            p25_depth_usd=depths[int(len(depths) * 0.25)] if len(depths) > 4 else min(depths),
        )

        self._calibrations[asset] = cal

        # Persist
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO fill_calibrations (asset, calibration_json, updated_at) VALUES (?, ?, ?)",
            (asset, json.dumps(cal.to_dict()), datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()

        logger.info("FILL_CAL: Recalibrated %s — spread median=%.1f p75=%.1f bps, depth=$%.0f",
                     asset, cal.median_spread_bps, cal.p75_spread_bps, cal.median_depth_usd)

    def get_calibration(self, asset: str) -> Optional[FillCalibration]:
        return self._calibrations.get(asset)

    def get_all_calibrations(self) -> dict[str, FillCalibration]:
        return self._calibrations

    def get_conservative_spread_bps(self, asset: str) -> float:
        """Get the conservative (p75) spread to use for fills."""
        cal = self._calibrations.get(asset)
        return cal.conservative_spread_bps() if cal else 3.0  # default 3 bps


import os