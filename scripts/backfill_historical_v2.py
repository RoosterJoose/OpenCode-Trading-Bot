"""
Backfill v2: fetch ALL available historical data from Coinbase CDE.
Uses smaller batch windows to get max coverage.
"""
import sys, os, json, re, time, math
from pathlib import Path
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

async def fetch_all_available(adapter, asset: str, interval: str) -> list[PerpCandle]:
    """Fetch ALL available data for asset/interval pair from CDE API.
    Uses aggressive backtracking: 300-candle batches with 1h overlap."""
    interval_secs = {"1h": 3600, "5m": 300}[interval]
    now = int(time.time())
    all_candles: dict[int, PerpCandle] = {}
    
    # CDE max: 300 candles per batch. Use 300-candle window.
    window_secs = 300 * interval_secs  # 300 candles at this interval
    end = now - 3600  # skip last hour (incomplete)
    max_batches = 200  # safety limit
    
    for batch_i in range(max_batches):
        start = end - window_secs
        if start <= 0:
            break
        
        # Don't go past the lookback
        # Check if we already have data from this period
        existing_ts = set(all_candles.keys())
        
        try:
            batch = await adapter.fetch_candles(asset, interval=interval, limit=300, start_time=start, end_time=end)
        except Exception as e:
            print(f"    {asset} {interval}: error at batch {batch_i}: {e}")
            await asyncio.sleep(2)
            continue
        
        if not batch:
            break
        
        duplicates = sum(1 for c in batch if c.timestamp in existing_ts)
        new_count = len(batch) - duplicates
        for c in batch:
            all_candles[c.timestamp] = c
        
        earliest = min(c.timestamp for c in batch)
        print(f"    {asset} {interval}: batch {batch_i+1} — {len(batch)} candles ({new_count} new, {duplicates} dup), range so far: {(now - earliest)/86400:.0f}d", end="\r")
        
        end = earliest - interval_secs  # overlap by 1 candle
        
        if len(batch) < 300:
            print(f"\n    {asset} {interval}: API returned < 300 — reached end of available history")
            break
        
        await asyncio.sleep(0.05)
    
    result = sorted(all_candles.values(), key=lambda c: c.timestamp)
    print(f"\n    {asset} {interval}: DONE — {len(result)} candles, {(now - result[0].timestamp)/86400:.0f} days" if result else f"\n    {asset} {interval}: DONE — 0 candles")
    return result

async def main():
    print(f"Fetching ALL available CDE data for {len(ASSETS)} assets × 2 intervals")
    print()
    
    adapter = CoinbaseAdvancedAdapter(api_key_id=kid, private_key=key, portfolio_uuid="")
    
    store: dict[str, dict[str, list[PerpCandle]]] = {"1h": {}, "5m": {}}
    
    for asset in ASSETS:
        for interval in ["1h", "5m"]:
            candles = await fetch_all_available(adapter, asset, interval)
            store[interval][asset] = candles
    
    # Write to DB
    print("\nWriting to backtest DB...")
    cx = sqlite3.connect(BACKTEST_DB)
    cx.execute("""
        CREATE TABLE IF NOT EXISTS candles_v2 (
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
    cx.execute("DELETE FROM candles_v2")
    cx.commit()
    
    total_1h, total_5m = 0, 0
    for interval in ["1h", "5m"]:
        for asset in ASSETS:
            candles = store[interval].get(asset, [])
            for c in candles:
                cx.execute(
                    "INSERT OR REPLACE INTO candles_v2 (asset, interval, timestamp, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (asset, interval, c.timestamp, c.open, c.high, c.low, c.close, c.volume),
                )
            print(f"  {asset} {interval}: {len(candles)} candles")
            if interval == "1h":
                total_1h += len(candles)
            else:
                total_5m += len(candles)
    
    cx.commit()
    cx.close()
    
    print(f"\nTotal: {total_1h} 1h candles ({total_1h/len(ASSETS):.0f}/asset), {total_5m} 5m candles ({total_5m/len(ASSETS):.0f}/asset)")
    # Time spans
    import time as tm
    for interval in ["1h", "5m"]:
        cx = sqlite3.connect(BACKTEST_DB)
        r = cx.execute("SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM candles_v2 WHERE interval=?", (interval,)).fetchone()
        if r[0]:
            days = (r[1] - r[0]) / 86400
            print(f"  {interval}: {r[2]} candles, {tm.strftime('%Y-%m-%d', tm.gmtime(r[0]))} -> {tm.strftime('%Y-%m-%d', tm.gmtime(r[1]))} ({days:.0f} days)")
        cx.close()

if __name__ == "__main__":
    asyncio.run(main())
