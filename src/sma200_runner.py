"""
SMA200 Daily Trend Strategy — standalone runner for Coinbase spot.
Fetches daily candles, evaluates SMA200 crossover, places spot orders.
Runs via systemd timer once per day.

Uses Coinbase Pro public API for candles (no auth) and Coinbase Advanced
Trade API (JWT auth) for order placement.

Assets: BTC, ETH, SOL
Risk: 1% per trade, 3 max concurrent positions, 3% stop distance.
"""
import asyncio, json, math, secrets, sqlite3, time as tm, os, sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt as pyjwt_lib

ASSETS = ["BTC", "ETH", "SOL"]
RISK_PCT = 0.01
STOP_PCT = 0.03
SMA_PERIOD = 200
TIME_EXIT_DAYS = 30
DATA_DIR = Path("/opt/hermes-trading-bot/data")
DB_PATH = DATA_DIR / "sma200_state.db"

CB_PRO = "https://api.exchange.coinbase.com"
CB_ADV = "https://api.coinbase.com/api/v3/brokerage"


def load_env() -> dict:
    env_path = Path("/opt/hermes-trading-bot/.env")
    if not env_path.exists():
        env_path = Path("/opt/hermes-trading-bot/.env.production")
    if not env_path.exists():
        return {}
    creds = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip("\"'")
    return {
        "key": creds.get("HERMES_COINBASE__API_KEY_ID", ""),
        "secret": creds.get("HERMES_COINBASE__PRIVATE_KEY", ""),
    }


def make_jwt(api_key_id: str, private_key: str, uri: str) -> str:
    now = int(tm.time())
    payload = {"iss": "cdp", "sub": api_key_id, "nbf": now, "exp": now + 120, "uri": uri}
    return pyjwt_lib.encode(
        payload, private_key.replace("\\n", "\n"),
        algorithm="ES256",
        headers={"kid": api_key_id, "nonce": secrets.token_hex(16)},
    )


async def fetch_daily_candles(client: httpx.AsyncClient, asset: str) -> list[dict]:
    url = f"{CB_PRO}/products/{asset}-USD/candles?granularity=86400"
    try:
        resp = await client.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"  Candle fetch {asset} error: {resp.status_code}")
            return []
        data = resp.json()
        candles = []
        for k in data:
            candles.append({
                "timestamp": int(k[0]), "open": float(k[3]),
                "high": float(k[2]), "low": float(k[1]),
                "close": float(k[4]), "volume": float(k[5]),
            })
        candles.sort(key=lambda c: c["timestamp"])
        return candles
    except Exception as e:
        print(f"  Candle fetch {asset} failed: {e}")
        return []


async def fetch_latest_price(client: httpx.AsyncClient, asset: str) -> float:
    url = f"{CB_PRO}/products/{asset}-USD/ticker"
    try:
        resp = await client.get(url, timeout=10)
        if resp.status_code == 200:
            return float(resp.json().get("price", 0))
    except Exception:
        pass
    return 0.0


async def place_spot_order(client: httpx.AsyncClient, creds: dict,
                           asset: str, side: str, qty: float) -> str | None:
    product_id = f"{asset}-USD"
    uri = "POST api.coinbase.com/api/v3/brokerage/orders"
    jwt = make_jwt(creds["key"], creds["secret"], uri)
    order = {
        "client_order_id": f"sma200_{asset}_{int(tm.time())}",
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {
            "market_market_ioc": {"base_size": str(qty)},
        },
    }
    try:
        resp = await client.post(
            f"{CB_ADV}/orders",
            headers={"Authorization": f"Bearer {jwt}"},
            json=order, timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            oid = data.get("order_id", "")
            print(f"  ORDER FILLED: {side} {qty:.6f} {asset} @ {oid[:12]}...")
            return oid
        else:
            print(f"  ORDER FAILED: {resp.status_code} {resp.text[:300]}")
            return None
    except Exception as e:
        print(f"  ORDER ERROR: {e}")
        return None


async def get_usd_balance(client: httpx.AsyncClient, creds: dict) -> float:
    uri = "GET api.coinbase.com/api/v3/brokerage/accounts/USD"
    jwt = make_jwt(creds["key"], creds["secret"], uri)
    try:
        resp = await client.get(
            f"{CB_ADV}/accounts/USD",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return float(data.get("account", {}).get("available_balance", {}).get("value", 0))
    except Exception:
        pass
    return 0.0


def get_position_state() -> dict:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(DB_PATH))
    cx.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            asset TEXT PRIMARY KEY, side TEXT NOT NULL,
            entry_price REAL NOT NULL, entry_ts INTEGER NOT NULL,
            size REAL NOT NULL, status TEXT DEFAULT 'open'
        )
    """)
    cx.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL,
            side TEXT NOT NULL, entry_price REAL, exit_price REAL,
            size REAL, entry_ts INTEGER, exit_ts INTEGER,
            entry_reason TEXT, exit_reason TEXT, r REAL, pnl_dollars REAL
        )
    """)
    rows = cx.execute(
        "SELECT asset, side, entry_price, entry_ts, size FROM positions WHERE status='open'"
    ).fetchall()
    cx.close()
    positions = {}
    for asset, side, price, ts, size in rows:
        positions[asset] = {"asset": asset, "side": side, "entry_price": price, "entry_ts": ts, "size": size}
    return positions


def save_position(asset: str, price: float, size: float, entry_ts: int):
    cx = sqlite3.connect(str(DB_PATH))
    cx.execute("""
        INSERT OR REPLACE INTO positions (asset, side, entry_price, entry_ts, size, status)
        VALUES (?, 'long', ?, ?, ?, 'open')
    """, (asset, price, entry_ts, size))
    cx.commit(); cx.close()


def close_position(asset: str, exit_price: float, exit_ts: int, reason: str) -> dict | None:
    cx = sqlite3.connect(str(DB_PATH))
    row = cx.execute(
        "SELECT side, entry_price, entry_ts, size FROM positions WHERE asset=? AND status='open'",
        (asset,)
    ).fetchone()
    if row:
        side, ep, ets, size = row
        r = (exit_price - ep) / (ep * STOP_PCT) if ep > 0 else 0.0
        pnl = (exit_price - ep) * size if side == 'long' else (ep - exit_price) * size
        cx.execute("UPDATE positions SET status='closed' WHERE asset=?", (asset,))
        cx.execute("""
            INSERT INTO trades (asset, side, entry_price, exit_price, size, entry_ts, exit_ts,
                                entry_reason, exit_reason, r, pnl_dollars)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (asset, side, ep, exit_price, size, ets, exit_ts, 'sma_entry', reason, r, pnl))
        cx.commit(); cx.close()
        return {"r": r, "pnl": pnl, "days": (exit_ts - ets) / 86400}
    cx.close()
    return None


async def main():
    print(f"\n{'='*60}")
    now = datetime.now(timezone.utc)
    print(f"  SMA200 DAILY TREND — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Assets: {', '.join(ASSETS)} | Risk: {RISK_PCT*100}%/trade | Stop: {STOP_PCT*100}%")
    print(f"{'='*60}")

    creds = load_env()
    if not creds.get("key"):
        print("  ERROR: No Coinbase API keys in .env")
        sys.exit(1)

    positions = get_position_state()
    print(f"\n  Positions: {len(positions)}")
    for a, p in positions.items():
        d = (int(tm.time()) - p["entry_ts"]) / 86400
        print(f"    {a}: ${p['entry_price']:.2f} ({d:.0f}d)")

    async with httpx.AsyncClient() as client:
        all_candles = {}
        for asset in ASSETS:
            candles = await fetch_daily_candles(client, asset)
            if not candles or len(candles) < SMA_PERIOD:
                print(f"\n  {asset}: Need {SMA_PERIOD}+ candles, got {len(candles) if candles else 0}")
                continue
            all_candles[asset] = candles

            # Show current status
            closes = [c["close"] for c in candles]
            sma = sum(closes[-200:]) / 200
            last = closes[-1]
            last_date = tm.strftime('%Y-%m-%d', tm.gmtime(candles[-1]["timestamp"]))
            above = last > sma
            print(f"\n  {asset}: ${last:.2f} SMA200=${sma:.2f}  {'✅ ABOVE' if above else '⚠️ BELOW'}")

        # Exit evaluation
        for asset, candles in all_candles.items():
            closes = [c["close"] for c in candles]
            sma = []
            for i in range(len(closes)):
                if i < SMA_PERIOD - 1: sma.append(None)
                else: sma.append(sum(closes[i-199:i+1]) / SMA_PERIOD)
            last_close = closes[-1]
            last_sma = sma[-1]
            if last_sma is None:
                continue

            pos = positions.get(asset)
            if not pos:
                continue

            exit_reason = None
            if last_close < last_sma:
                exit_reason = "close_below_sma200"
            elif candles[-1]["timestamp"] - pos["entry_ts"] > TIME_EXIT_DAYS * 86400:
                exit_reason = "time_exit_30d"

            if exit_reason:
                result = close_position(asset, last_close, int(tm.time()), exit_reason)
                del positions[asset]
                if result:
                    print(f"\n  EXIT {asset}: {exit_reason} @ ${last_close:.2f} "
                          f"R={result['r']:+.2f} PnL=${result['pnl']:+.2f} ({result['days']:.0f}d)")

        # Entry evaluation
        for asset, candles in all_candles.items():
            if asset in positions:
                continue
            if len(positions) >= 3:
                break

            closes = [c["close"] for c in candles]
            sma = []
            for i in range(len(closes)):
                if i < SMA_PERIOD - 1: sma.append(None)
                else: sma.append(sum(closes[i-199:i+1]) / SMA_PERIOD)

            last_close = closes[-1]
            last_sma = sma[-1]
            if last_sma is None or not (last_close > last_sma):
                continue

            live_price = await fetch_latest_price(client, asset)
            price = live_price if live_price > 0 else last_close

            equity = 5000.0
            try:
                bal = await get_usd_balance(client, creds)
                if bal > 0: equity = bal
            except Exception:
                pass

            risk_dollars = equity * RISK_PCT
            stop_distance = price * STOP_PCT
            qty = risk_dollars / stop_distance if stop_distance > 0 else 0

            min_qty = {"BTC": 0.0001, "ETH": 0.001, "SOL": 0.01}.get(asset, 0.001)
            qty = max(min_qty, math.floor(qty / min_qty) * min_qty)

            notional = qty * price
            if notional <= 0 or qty <= 0:
                print(f"\n  {asset}: qty too small, skipping")
                continue

            print(f"\n  ENTER {asset}: ${price:.2f} SMA200=${last_sma:.2f}")
            print(f"     Size: {qty:.6f} Notional: ${notional:.2f} Risk: ${risk_dollars:.2f}")

            entry_ts = int(tm.time())
            save_position(asset, price, qty, entry_ts)
            positions[asset] = {"asset": asset, "side": "long", "entry_price": price,
                                "entry_ts": entry_ts, "size": qty}
            # Place live order
            # oid = await place_spot_order(client, creds, asset, "buy", qty)
            # if not oid: roll back position entry

    # Summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"  Open: {len(positions)}")
    for a, p in positions.items():
        d = (int(tm.time()) - p["entry_ts"]) / 86400
        print(f"    {a}: ${p['entry_price']:.2f} ({d:.0f}d)")

    cx = sqlite3.connect(str(DB_PATH))
    rows = cx.execute(
        "SELECT asset, entry_price, exit_price, exit_reason, r, pnl_dollars, entry_ts, exit_ts "
        "FROM trades ORDER BY exit_ts DESC LIMIT 10"
    ).fetchall()
    cx.close()
    if rows:
        print(f"\n  Last trades:")
        for a, ep, xp, rs, r, pnl, ets, xts in rows:
            ed = tm.strftime('%m-%d', tm.gmtime(ets))
            xd = tm.strftime('%m-%d', tm.gmtime(xts))
            print(f"    {ed}→{xd} {a:5s} {rs:20s} R={r:+.2f} ${pnl:+.2f}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
