"""
Backfill 6 months of candle data from Coinbase CDE.
Writes to a separate DB (backtest_hermes.db) to avoid interfering with live bot.

Usage: sudo -u hermes /opt/hermes-trading-bot/.venv/bin/python3 scripts/backfill_historical.py
"""
import sys, os, json, re, time, math
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import sqlite3

from src.adapters.coinbase_advanced import CoinbaseAdvancedAdapter
from src.core.types import PerpCandle

DATA_DIR = Path(os.environ.get("HERMES_DATA_DIR", "/opt/hermes-trading-bot/data"))
env_path = Path(os.environ.get("HERMES_ENV_FILE", "/opt/hermes-trading-bot/.env_key"))
if env_path.exists():
    with open(env_path) as f:
        content = f.read()
    m = re.search(r'HERMES_COINBASE__API_KEY_ID="([^"]+)"', content)
    kid = m.group(1) if m else ""
    m2 = re.search(r"HERMES_COINBASE__PRIVATE_KEY='(.+?)'\n", content, re.DOTALL)
    key = m2.group(1) if m2 else ""
else:
    kid = os.environ.get("HERMES_COINBASE__API_KEY_ID", "")
    key = os.environ.get("HERMES_COINBASE__PRIVATE_KEY", "")

ASSETS = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "AAVE", "LTC", "NEAR", "SUI", "BNB", "XLM", "HBAR", "BCH", "ZEC",
    "PEPE", "SHIB", "HYPE", "ONDO", "ENA",
]

BACKTEST_DB = "/opt/hermes-trading-bot/data/backtest_hermes.db"

MONTHS = 6
SECONDS_PER_MONTH = 30 * 86400
LOOKBACK = MONTHS * SECONDS_PER_MONTH

# Store candles by (interval, asset) in memory
_candle_store: dict[str, dict[str, list[PerpCandle]]] = {}
_candle_store["1h"] = {}
_candle_store["5m"] = {}

async def backfill_interval(adapter, asset: str, interval: str) -> list[PerpCandle]:
    """Backfill one asset/interval pair, returning collected candles."""
    now = int(time.time()) + 3600  # slight future buffer
    all_candles: dict[int, PerpCandle] = {}  # dedup by timestamp
    batch_size = 300

    # Map interval to seconds per candle
    interval_secs = {"1h": 3600, "5m": 300}[interval]

    # Coinbase API: max 300 candles per request, and time window must be reasonable
    # For 1h: 300 candles = 300h = 12.5 days max window
    # For 5m: 300 candles = 1500m = 25h max window
    window_secs = min(batch_size * interval_secs, 14 * 86400)  # cap at 14 days
    end = now - 3600  # start from 1h ago (skip newest incomplete)

    batch_count = 0
    while (now - end) < LOOKBACK:
        start = end - window_secs
        try:
            start_ts = max(start, int(now - LOOKBACK - 86400))  # don't go before lookback
            if start_ts <= 0:
                break
            batch = await adapter.fetch_candles(asset, interval=interval, limit=batch_size, start_time=start_ts, end_time=end)
        except Exception as e:
            print(f"    {asset} {interval}: API error at {start}: {e}")
            break
        if not batch:
            break
        for c in batch:
            all_candles[c.timestamp] = c
        batch_count += 1
        print(f"    {asset} {interval}: {len(batch)} candles (total {len(all_candles)}, batch {batch_count})", end="\r")

        earliest = min(c.timestamp for c in batch)
        end = earliest - interval_secs  # overlap by 1 candle
        if len(batch) < batch_size:
            break
        await asyncio.sleep(0.1)  # rate limit

    print(f"\n    {asset} {interval}: DONE — {len(all_candles)} total candles")
    return list(all_candles.values())

async def main():
    print(f"Backfilling {MONTHS} months of candle data for {len(ASSETS)} assets...")
    print(f"Output DB: {BACKTEST_DB}")
    print()

    adapter = CoinbaseAdvancedAdapter(api_key_id=kid, private_key=key, portfolio_uuid="")

    for asset in ASSETS:
        for interval in ["1h", "5m"]:
            candles = await backfill_interval(adapter, asset, interval)
            _candle_store[interval][asset] = candles
            print()

    # Write to backtest DB
    print("Writing to backtest DB...")
    cx = sqlite3.connect(BACKTEST_DB)
    cx.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            asset TEXT NOT NULL,
            interval TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            PRIMARY KEY (asset, interval, timestamp)
        )
    """)
    cx.execute("DELETE FROM candles")
    cx.commit()

    for interval in ["1h", "5m"]:
        for asset in ASSETS:
            candles = _candle_store[interval].get(asset, [])
            for c in candles:
                cx.execute(
                    "INSERT OR REPLACE INTO candles (asset, interval, timestamp, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (asset, interval, c.timestamp, c.open, c.high, c.low, c.close, c.volume),
                )
            print(f"  {asset} {interval}: {len(candles)} candles saved")

    cx.commit()
    cx.close()

    # Summary
    total_1h = sum(len(_candle_store["1h"].get(a, [])) for a in ASSETS)
    total_5m = sum(len(_candle_store["5m"].get(a, [])) for a in ASSETS)
    print()
    print(f"Backfill complete:")
    print(f"  1h: {total_1h} candles ({total_1h / len(ASSETS):.0f} per asset avg)")
    print(f"  5m: {total_5m} candles ({total_5m / len(ASSETS):.0f} per asset avg)")
    print(f"  DB: {BACKTEST_DB}")

if __name__ == "__main__":
    asyncio.run(main())
