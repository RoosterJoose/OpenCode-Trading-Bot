import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from freqtrade.data.history import get_datahandler
from freqtrade.enums import CandleType


def ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_chunk(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps({
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    step_ms = 14 * 24 * 3600 * 1000
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + step_ms, end_ms)
        rows.extend(fetch_chunk(coin, interval, cursor, chunk_end))
        cursor = chunk_end + 1
        time.sleep(0.15)

    parsed = []
    seen = set()
    for row in rows:
        ts = int(row.get("t", 0))
        if not ts or ts in seen:
            continue
        seen.add(ts)
        parsed.append({
            "date": pd.to_datetime(ts, unit="ms", utc=True),
            "open": float(row["o"]),
            "high": float(row["h"]),
            "low": float(row["l"]),
            "close": float(row["c"]),
            "volume": float(row.get("v", 0.0)),
        })

    df = pd.DataFrame(parsed).sort_values("date")
    if df.empty:
        return df
    return df.drop_duplicates(subset=["date"]).reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", required=True)
    parser.add_argument("--start", default="2026-01-01T00:00:00Z")
    parser.add_argument("--end", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--pairs", nargs="+", default=["BTC/USDC:USDC", "ETH/USDC:USDC", "SOL/USDC:USDC"])
    args = parser.parse_args()

    start_ms = ms(args.start)
    end_ms = ms(args.end)
    handler = get_datahandler(Path(args.datadir), "feather")

    for pair in args.pairs:
        coin = pair.split("/")[0]
        df = fetch_candles(coin, args.timeframe, start_ms, end_ms)
        if df.empty:
            print(f"{pair}: no candles")
            continue
        handler.ohlcv_store(pair, args.timeframe, df, CandleType.FUTURES)
        print(f"{pair}: stored {len(df)} candles from {df['date'].iloc[0]} to {df['date'].iloc[-1]}")


if __name__ == "__main__":
    main()
