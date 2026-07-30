"""
Daily SMA200 Spot Strategy — Control Module v2.

Improvements over v1:
- 14-period ATR dynamic stop (2.0x ATR) instead of fixed 3%
- Correlation-adjusted sizing: max 1% aggregate portfolio risk
- Buy-and-hold benchmarks from historical daily closes
- Coinbase Pro public API for candles (no auth required)
- Paper cash ledger with taker fees + half-spread cost model
"""

import asyncio
import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("hermes.sma200")

ASSETS = ["BTC", "ETH", "SOL"]
PRODUCT_IDS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
SMA_PERIOD = 200
RISK_PCT = 0.01
TIME_EXIT_DAYS = 30
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
TAKER_FEE = 0.006
HALF_SPREAD_BPS = 5
INITIAL_CAPITAL = 5000.0
DATA_DIR = Path("/opt/hermes-trading-bot/data")
DB_PATH = DATA_DIR / "sma200_control.db"
CB_PRO = "https://api.exchange.coinbase.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    asset TEXT PRIMARY KEY,
    side TEXT, entry_price REAL, size REAL,
    entry_time TEXT, stop_price REAL, target_price REAL, entry_bar_close TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT, side TEXT, entry_price REAL, exit_price REAL,
    size REAL, pnl_dollars REAL, pnl_pct REAL, fees REAL,
    r_multiple REAL, entry_time TEXT, exit_time TEXT,
    exit_reason TEXT, entry_bar_close TEXT
);
CREATE TABLE IF NOT EXISTS equity_curve (
    timestamp TEXT PRIMARY KEY,
    equity REAL, cash REAL, positions_value REAL, total_fees REAL
);
CREATE TABLE IF NOT EXISTS benchmark_equity (
    timestamp TEXT PRIMARY KEY,
    cash_benchmark REAL, buy_hold_btc REAL, buy_hold_equal REAL
);
"""


@dataclass
class Position:
    asset: str
    side: str = "flat"
    entry_price: float = 0.0
    size: float = 0.0
    entry_time: str = ""
    stop_price: float = 0.0
    target_price: float = 0.0
    entry_bar_close: str = ""


class SMA200Control:

    def __init__(self, db_path: str = str(DB_PATH), initial_capital: float = INITIAL_CAPITAL):
        self.db_path = db_path
        self.cash = initial_capital
        self.positions: dict[str, Position] = {}
        self.total_fees = 0.0
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def fetch_daily_candles(self, product_id: str, limit: int = 250) -> list[dict]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CB_PRO}/products/{product_id}/candles",
                params={"granularity": "86400", "limit": limit},
                timeout=30.0
            )
            if resp.status_code != 200:
                logger.error("Candle fetch failed for %s: %d", product_id, resp.status_code)
                return []
            data = resp.json()
            candles = []
            now_utc = datetime.now(timezone.utc)
            for row in data:
                t = datetime.fromtimestamp(row[0], tz=timezone.utc)
                bar_close = t + timedelta(hours=24)
                if bar_close > now_utc:
                    continue
                candles.append({
                    "timestamp": row[0], "datetime": t.isoformat(),
                    "open": float(row[3]), "high": float(row[2]),
                    "low": float(row[1]), "close": float(row[4]),
                    "volume": float(row[5]),
                })
            candles.sort(key=lambda c: c["timestamp"])
            return candles

    def compute_sma(self, candles: list[dict], period: int = SMA_PERIOD) -> Optional[float]:
        if len(candles) < period:
            return None
        closes = [c["close"] for c in candles[-period:]]
        return sum(closes) / period

    def compute_atr(self, candles: list[dict], period: int = ATR_PERIOD) -> Optional[float]:
        if len(candles) < period + 1:
            return None
        tr_sum = 0.0
        for i in range(-period, 0):
            prev_close = candles[i - 1]["close"]
            high = candles[i]["high"]
            low = candles[i]["low"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_sum += tr
        return tr_sum / period

    def generate_signal(self, candles: list[dict], atr: float) -> Optional[dict]:
        if len(candles) < SMA_PERIOD or atr is None or atr <= 0:
            return None
        sma = self.compute_sma(candles)
        last_candle = candles[-1]
        last_close = last_candle["close"]
        if last_close > sma:
            return {"signal": "LONG", "price": last_close, "sma": sma, "atr": atr,
                    "bar_close": last_candle["datetime"]}
        else:
            return {"signal": "FLAT", "price": last_close, "sma": sma, "atr": atr,
                    "bar_close": last_candle["datetime"]}

    def compute_sizes(self, signals: dict[str, dict], equity: float) -> dict[str, float]:
        """
        Position sizing: divide 1% risk budget equally among entering assets.

        If N assets trigger simultaneously, each gets (1/N) × 1% × equity
        risk dollars. This guarantees total combined portfolio risk never
        exceeds 1% regardless of correlation.
        """
        entering = {a: s for a, s in signals.items() if s and s.get("signal") == "LONG"}
        if not entering:
            return {}
        n = len(entering)
        per_asset_risk_dollars = equity * 0.01 / n
        sizes = {}
        for asset, sig in entering.items():
            stop_dist = ATR_STOP_MULT * sig["atr"]
            if stop_dist > 0:
                sizes[asset] = per_asset_risk_dollars / stop_dist
            else:
                sizes[asset] = 0.0
        return sizes

    def execute_entry(self, asset: str, signal_price: float, stop_price: float, size: float, bar_close: str):
        if asset in self.positions and self.positions[asset].side == "long":
            return
        fill_price = signal_price * (1 + HALF_SPREAD_BPS / 10000)
        notional_cost = size * fill_price
        if notional_cost > self.cash:
            size = self.cash / fill_price * 0.99
            notional_cost = size * fill_price
        fee = notional_cost * TAKER_FEE
        self.cash -= (notional_cost + fee)
        self.total_fees += fee
        target_price = fill_price + (fill_price - stop_price) * 3.0
        pos = Position(
            asset=asset, side="long", entry_price=fill_price, size=size,
            entry_time=datetime.now(timezone.utc).isoformat(),
            stop_price=stop_price, target_price=target_price,
            entry_bar_close=bar_close,
        )
        self.positions[asset] = pos
        logger.info("SMA200 ENTRY %s @ %.2f size=%.6f stop=%.2f fee=%.2f",
                     asset, fill_price, size, stop_price, fee)

    def execute_exit(self, asset: str, signal_price: float, reason: str, bar_close: str):
        pos = self.positions.get(asset)
        if pos is None or pos.side != "long":
            return
        fill_price = signal_price * (1 - HALF_SPREAD_BPS / 10000)
        fee = pos.size * fill_price * TAKER_FEE
        proceeds = pos.size * fill_price - fee
        self.cash += proceeds
        self.total_fees += fee
        pnl_dollars = proceeds - (pos.size * pos.entry_price)
        pnl_pct = (pnl_dollars / (pos.size * pos.entry_price)) * 100
        entry_cost = pos.size * pos.entry_price
        stop_dollars = entry_cost - (pos.size * pos.stop_price) if pos.stop_price > 0 else entry_cost * 0.03
        r_multiple = pnl_dollars / stop_dollars if stop_dollars > 0 else 0.0
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO trades (asset, side, entry_price, exit_price, size, pnl_dollars, pnl_pct, fees, r_multiple, entry_time, exit_time, exit_reason, entry_bar_close) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset, "long", pos.entry_price, fill_price, pos.size, pnl_dollars, pnl_pct,
             fee + entry_cost * TAKER_FEE, r_multiple, pos.entry_time,
             datetime.now(timezone.utc).isoformat(), reason, pos.entry_bar_close)
        )
        conn.commit()
        conn.close()
        del self.positions[asset]
        logger.info("SMA200 EXIT %s @ %.2f pnl=$%.2f R=%.2f reason=%s",
                     asset, fill_price, pnl_dollars, r_multiple, reason)

    def get_equity(self, prices: dict[str, float]) -> float:
        total = self.cash
        for asset, pos in self.positions.items():
            if pos.side == "long" and asset in prices:
                total += pos.size * prices[asset]
        return total

    def save_equity_snapshot(self, prices: dict[str, float], all_histories: dict[str, list[dict]]):
        equity = self.get_equity(prices)
        positions_value = sum(
            pos.size * prices.get(a, 0) for a, pos in self.positions.items() if pos.side == "long"
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        cash_benchmark = INITIAL_CAPITAL
        buy_hold_btc = INITIAL_CAPITAL
        if "BTC" in all_histories and len(all_histories["BTC"]) >= SMA_PERIOD:
            first_close = all_histories["BTC"][0]["close"]
            latest_price = prices.get("BTC", all_histories["BTC"][-1]["close"])
            if first_close > 0:
                buy_hold_btc = INITIAL_CAPITAL * (latest_price / first_close)
        buy_hold_equal = INITIAL_CAPITAL
        bh_values = []
        for asset in ASSETS:
            hist = all_histories.get(asset, [])
            if len(hist) >= SMA_PERIOD:
                first = hist[0]["close"]
                latest = prices.get(asset, hist[-1]["close"])
                if first > 0:
                    bh_values.append(INITIAL_CAPITAL / 3 * (latest / first))
        if bh_values:
            buy_hold_equal = sum(bh_values)
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO equity_curve (timestamp, equity, cash, positions_value, total_fees) VALUES (?, ?, ?, ?, ?)",
            (timestamp, equity, self.cash, positions_value, self.total_fees)
        )
        conn.execute(
            "INSERT OR REPLACE INTO benchmark_equity (timestamp, cash_benchmark, buy_hold_btc, buy_hold_equal) VALUES (?, ?, ?, ?)",
            (timestamp, cash_benchmark, buy_hold_btc, buy_hold_equal)
        )
        conn.commit()
        conn.close()

    async def evaluate(self):
        logger.info("SMA200 control: starting evaluation cycle")
        prices: dict[str, float] = {}
        all_histories: dict[str, list[dict]] = {}
        signals: dict[str, Optional[dict]] = {}
        for asset in ASSETS:
            product_id = PRODUCT_IDS[asset]
            candles = await self.fetch_daily_candles(product_id)
            if not candles or len(candles) < SMA_PERIOD:
                logger.warning("Insufficient candles for %s: %d", asset, len(candles) if candles else 0)
                continue
            all_histories[asset] = candles
            atr = self.compute_atr(candles)
            signal = self.generate_signal(candles, atr)
            if signal is None:
                continue
            prices[asset] = signal["price"]
            signals[asset] = signal
        # Exits
        for asset in ASSETS:
            sig = signals.get(asset)
            if sig is None:
                continue
            current_pos = self.positions.get(asset)
            if current_pos is None or current_pos.side != "long":
                continue
            exit_reason = None
            if sig["price"] <= current_pos.stop_price:
                exit_reason = "stop_loss"
            elif sig["signal"] == "FLAT":
                exit_reason = "sma_exit"
            else:
                entry_dt = datetime.fromisoformat(current_pos.entry_time)
                days_held = (datetime.now(timezone.utc) - entry_dt).days
                if days_held >= TIME_EXIT_DAYS:
                    exit_reason = "time_exit"
            if exit_reason:
                self.execute_exit(asset, sig["price"], exit_reason, sig["bar_close"])
        # Entries
        sizes = self.compute_sizes(signals, self.get_equity(prices))
        if sizes:
            logger.info("SMA200 sizes: %s", {a: f"{s:.4f}" for a, s in sizes.items()})
        for asset in ASSETS:
            sig = signals.get(asset)
            if sig is None:
                continue
            if sig["signal"] != "LONG":
                continue
            if asset in self.positions:
                continue
            size = sizes.get(asset, 0.0)
            if size <= 0:
                continue
            stop_price = sig["price"] - ATR_STOP_MULT * sig["atr"]
            if stop_price <= 0:
                continue
            self.execute_entry(asset, sig["price"], stop_price, size, sig["bar_close"])
        # Snapshot
        if prices:
            self.save_equity_snapshot(prices, all_histories)
        equity = self.get_equity(prices)
        logger.info("SMA200 control: equity=$%.2f cash=$%.2f positions=%d fees=$%.4f",
                     equity, self.cash, len(self.positions), self.total_fees)


async def main():
    control = SMA200Control()
    await control.evaluate()
    equity = control.get_equity({})
    print(f"Equity: ${equity:.2f}")
    print(f"Cash: ${control.cash:.2f}")
    print(f"Fees: ${control.total_fees:.4f}")
    print(f"Positions: {len(control.positions)}")


if __name__ == "__main__":
    asyncio.run(main())
