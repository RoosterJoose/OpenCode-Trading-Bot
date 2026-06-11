"""Backfill historical candles from Coinbase CDE.

Fetches candles in reverse-chronological batches (300 per batch) to get the
maximum available history. Stores as JSON for the backtest engine.

Usage: sudo -u hermes /opt/hermes-trading-bot/.venv/bin/python3 scripts/backfill_candles.py
"""
import sys, os, json, re, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
from src.adapters.coinbase_advanced import CoinbaseAdvancedAdapter
from src.core.types import PerpCandle

DATA_DIR = Path(os.environ.get("HERMES_DATA_DIR", "/opt/hermes-trading-bot/data"))

# Load env vars
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

async def backfill_asset(a: "CoinbaseAdvancedAdapter", asset: str) -> list[PerpCandle]:
    now = int(time.time())
    all_candles: list[PerpCandle] = []
    batch_size = 300
    window_secs = 14 * 86400  # 14 days per batch (CDE max)
    end = now

    while True:
        start = end - window_secs
        try:
            batch = await a.fetch_candles(asset, limit=batch_size, start_time=start, end_time=end)
        except Exception as e:
            print(f"  {asset}: error at {start}: {e}")
            break
        if not batch:
            break
        existing_ts = {c.timestamp for c in all_candles}
        new = [c for c in batch if c.timestamp not in existing_ts]
        if not new:
            break
        all_candles.extend(new)
        print(f"  {asset}: {len(new)} candles at {batch[0].timestamp} (total {len(all_candles)})")
        earliest = min(c.timestamp for c in batch)
        end = earliest - 3600  # overlap by 1h to avoid gaps
        if len(batch) < batch_size:
            break
        await asyncio.sleep(0.5)  # rate limit

    all_candles.sort(key=lambda x: x.timestamp)
    return all_candles

async def main():
    a = CoinbaseAdvancedAdapter(kid, key)
    result = {}
    for asset in ASSETS:
        print(f"\nBackfilling {asset}...")
        candles = await backfill_asset(a, asset)
        result[asset] = [
            {"o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.volume, "t": c.timestamp}
            for c in candles
        ]
        print(f"  -> {len(candles)} total candles")

    path = DATA_DIR / "historical_candles.json"
    with open(path, "w") as f:
        json.dump({"candles": result, "fetched_at": int(time.time())}, f)
    print(f"\nSaved {len(result)} assets to {path}")

asyncio.run(main())
