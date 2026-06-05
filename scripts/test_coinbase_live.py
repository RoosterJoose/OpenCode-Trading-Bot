#!/usr/bin/env python3
"""
Live verification script for CoinbaseAdvancedAdapter.

Fetches candles, prices, funding, and OI for BTC using the live API.
Run with your CDP API key set in environment variables:

  export HERMES_COINBASE__API_KEY_ID="organizations/.../apiKeys/..."
  export HERMES_COINBASE__PRIVATE_KEY="-----BEGIN EC PRIVATE KEY-----\\n..."
  python scripts/test_coinbase_live.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.adapters.coinbase_advanced import CoinbaseAdvancedAdapter


async def main():
    api_key_id = os.environ.get("HERMES_COINBASE__API_KEY_ID", "")
    private_key = os.environ.get("HERMES_COINBASE__PRIVATE_KEY", "")

    if not api_key_id or not private_key:
        print("ERROR: Set HERMES_COINBASE__API_KEY_ID and HERMES_COINBASE__PRIVATE_KEY")
        sys.exit(1)

    adapter = CoinbaseAdvancedAdapter(
        api_key_id=api_key_id,
        private_key=private_key,
    )

    print("=== Test 1: fetch_all_mids ===")
    mids = await adapter.fetch_all_mids()
    print(f"  Got {len(mids)} assets")
    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        if asset in mids:
            print(f"  {asset}: ${mids[asset]:,.2f}")

    print("\n=== Test 2: fetch_candles (BTC, 1h, 3) ===")
    candles = await adapter.fetch_candles("BTC", "1h", 3)
    print(f"  Got {len(candles)} candles")
    for c in candles[:3]:
        print(f"  {c.timestamp}: O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} C={c.close:.2f} V={c.volume:.2f}")

    print("\n=== Test 3: fetch_funding ===")
    funding = await adapter.fetch_funding()
    print(f"  Got {len(funding)} assets")
    for asset in ["BTC", "ETH", "SOL"]:
        if asset in funding:
            print(f"  {asset}: {funding[asset]:.8f}")

    print("\n=== Test 4: fetch_open_interest ===")
    oi = await adapter.fetch_open_interest()
    print(f"  Got {len(oi)} assets")
    for asset in ["BTC", "ETH", "SOL"]:
        if asset in oi:
            print(f"  {asset}: {oi[asset]:,.2f}")

    print("\n=== Test 5: fetch_metadata ===")
    meta = await adapter.fetch_metadata()
    print(f"  Got {len(meta)} configs")
    for asset in ["BTC", "ETH", "SOL"]:
        if asset in meta:
            cfg = meta[asset]
            print(f"  {asset}: max_lev={cfg.max_leverage}x step={cfg.step_size} min={cfg.min_size}")

    print("\n=== Test 6: fetch_snapshot (BTC) ===")
    snap = await adapter.fetch_snapshot("BTC")
    print(f"  price={snap.price:.2f} funding={snap.funding_rate:.8f} OI={snap.open_interest:.2f}")

    await adapter.close()
    print("\n✓ All live tests completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
