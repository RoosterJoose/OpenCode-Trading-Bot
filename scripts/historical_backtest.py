#!/usr/bin/env python3
"""
Historical backtest engine: simulates candle data through all 5 strategies.
Reports per-strategy PnL, WR, avg R, Sharpe.

Usage:
  python3 scripts/historical_backtest.py [--sweep]
"""
import sys, os, json, math, itertools, datetime as dt
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import PerpCandle, PerpPosition, RegimeType, Side, Signal
from src.strategies.trend import TrendFollow
from src.strategies.mr import MeanReversion
from src.strategies.donchian import DonchianBreakout
from src.strategies.xs_momentum import CrossSectionalMomentum
from src.strategies.momentum import DriftMomentum

STOP_PCT = 0.05  # overridden by --sweep


class BTTrade:
    """Simple backtest trade."""
    def __init__(self, asset: str, side: Side, strategy: str, entry_price: float, entry_time: str):
        self.asset = asset
        self.side = side
        self.strategy = strategy
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.high_price = entry_price
        self.low_price = entry_price
        self.exit_price: Optional[float] = None
        self.exit_time: Optional[str] = None
        self.exit_reason: Optional[str] = None
        self.r = 0.0

    @property
    def pnl(self) -> float:
        return self.r * 100.0


def compute_sharpe(returns: list[float]) -> float:
    n = len(returns)
    if n < 5:
        return 0.0
    m = sum(returns) / n
    if m <= 0:
        return 0.0
    v = sum((r - m) ** 2 for r in returns) / n
    s = math.sqrt(v) if v > 0 else 1e-9
    return (m / s) * math.sqrt(365)


def classify_regime(candles: list[PerpCandle]) -> RegimeType:
    if len(candles) < 50:
        return RegimeType.RANDOM_WALK
    recent = candles[-50:]
    closes = [c.close for c in recent]
    lo = min(closes)
    hi = max(closes)
    rng = (hi - lo) / lo if lo > 0 else 0
    if rng < 0.002:
        return RegimeType.DEAD_MARKET
    if rng > 0.15:
        return RegimeType.HIGH_VOL
    n = len(closes)
    xs = list(range(n))
    mx = n / 2
    my = sum(closes) / n
    num = sum((xs[i] - mx) * (closes[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return RegimeType.RANDOM_WALK
    sp = num / den
    sp_pct = sp / my * 100 if my > 0 else 0
    if abs(sp_pct) > 0.001:
        return RegimeType.TRENDING
    return RegimeType.RANDOM_WALK


def run_backtest(assets: list[str], candle_map: dict) -> dict:
    """Run all 5 strategies over candle data synchronously."""
    strategies = [
        TrendFollow(),
        MeanReversion(),
        DonchianBreakout(),
        CrossSectionalMomentum(),
        DriftMomentum(),
    ]

    all_trades: dict[str, list[BTTrade]] = {s.name(): [] for s in strategies}
    open_trades: dict[str, tuple[BTTrade, str]] = {}  # asset -> (trade, strategy_name)

    for asset in assets:
        candles = candle_map.get(asset, [])
        if len(candles) < 100:
            continue

        for i in range(100, len(candles)):
            bar = candles[i]
            window = candles[:i + 1]
            regime = classify_regime(window)
            price = bar.close
            ts = dt.datetime.fromtimestamp(bar.timestamp, tz=dt.timezone.utc).isoformat()

            # Check exits for open trade
            if asset in open_trades:
                trade, sname = open_trades[asset]
                trade.high_price = max(trade.high_price, price)
                trade.low_price = min(trade.low_price, price)

                # Find the matching strategy for exit
                for strat in strategies:
                    if strat.name() != sname:
                        continue
                    pos = PerpPosition(
                        asset=asset, side=trade.side, entry_price=trade.entry_price,
                        size=1.0, stop_loss=trade.entry_price * (1 - STOP_PCT) if trade.side == Side.LONG else trade.entry_price * (1 + STOP_PCT),
                        strategy=sname,
                    )
                    result = strat.should_exit(asset, pos, price, window, 0.0)
                    if result:
                        reason, exit_price = result
                        risk = abs(trade.entry_price * STOP_PCT)
                        if trade.side == Side.SHORT:
                            trade.r = (trade.entry_price - price) / risk
                        else:
                            trade.r = (price - trade.entry_price) / risk
                        trade.exit_price = price
                        trade.exit_time = ts
                        trade.exit_reason = reason
                        all_trades[sname].append(trade)
                        del open_trades[asset]
                        break
                continue  # skip entries when position open

            # Check entries
            for strat in strategies:
                result = strat.should_enter(asset, window, [], regime, None, 0.0)
                if result:
                    side, confidence, meta = result
                    trade = BTTrade(asset, side, strat.name(), price, ts)
                    open_trades[asset] = (trade, strat.name())
                    break

    # Close remaining trades at last price
    for asset, (trade, sname) in open_trades.items():
        candles = candle_map.get(asset, [])
        if candles:
            last = candles[-1]
            risk = abs(trade.entry_price * STOP_PCT)
            if trade.side == Side.SHORT:
                trade.r = (trade.entry_price - last.close) / risk
            else:
                trade.r = (last.close - trade.entry_price) / risk
            trade.exit_price = last.close
            trade.exit_time = dt.datetime.fromtimestamp(last.timestamp, tz=dt.timezone.utc).isoformat()
            trade.exit_reason = "end_of_data"
            all_trades[sname].append(trade)

    # Stats
    results = {}
    for name, trades in all_trades.items():
        n = len(trades)
        if n == 0:
            results[name] = {"trades": 0, "pnl": 0.0, "wr": 0.0, "avg_r": 0.0, "sharpe": 0.0}
            continue
        wins = sum(1 for t in trades if t.r > 0)
        total_r = sum(t.r for t in trades)
        returns = [t.r for t in trades]
        results[name] = {
            "trades": n,
            "wins": wins,
            "wr": round(wins / n, 3),
            "pnl": round(sum(t.pnl for t in trades), 2),
            "avg_r": round(total_r / n, 3) if n else 0.0,
            "sharpe": round(compute_sharpe(returns), 3),
        }
        # Print trades for analysis
        if n > 0:
            pass  # keep trades for later
    return results


async def main():
    from src.adapters.coinbase_advanced import CoinbaseAdvancedAdapter
    import re

    with open("/etc/systemd/system/hermes-bot.service.d/override.conf") as f:
        c = f.read()
    ak = re.search(r'COINBASE__API_KEY_ID=([^"\n]+)', c).group(1)
    pk_raw = re.search(r'COINBASE__PRIVATE_KEY=([^"\n]+)', c).group(1)
    pk = pk_raw.replace("\\n", "\n")

    adapter = CoinbaseAdvancedAdapter(ak, pk)

    assets = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
              "AAVE", "LTC", "NEAR", "SUI", "BNB", "XLM", "HBAR", "BCH", "ZEC",
              "PEPE", "SHIB", "HYPE", "ONDO", "ENA"]

    print("Loading candles...")

    # Try loading from historical_candles.json first
    json_path = Path("/opt/hermes-trading-bot/data/historical_candles.json")
    candle_map = {}
    limit_flag = next((int(sys.argv[i + 1]) for i, a in enumerate(sys.argv) if a == "--limit" and i + 1 < len(sys.argv)), 0)
    if json_path.exists():
        print(f"  Loading from {json_path}...")
        with open(json_path) as f:
            data = json.load(f)
        for asset in data.get("candles", {}):
            candles_raw = data["candles"][asset]
            if limit_flag > 0 and len(candles_raw) > limit_flag:
                candles_raw = candles_raw[-limit_flag:]
            candles = [
                PerpCandle(
                    open=c["o"], high=c["h"], low=c["l"], close=c["c"],
                    volume=c["v"], timestamp=c["t"],
                ) for c in candles_raw
            ]
            if len(candles) >= 100:
                # Sort by timestamp ascending
                candles.sort(key=lambda x: x.timestamp)
                candle_map[asset] = candles
                print(f"  {asset}: {len(candles)} candles")
            else:
                print(f"  {asset}: insufficient data ({len(candles)})")

    if not candle_map:
        # Fallback: load from adapter
        assets = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
                  "AAVE", "LTC", "NEAR", "SUI", "BNB", "XLM", "HBAR", "BCH", "ZEC",
                  "PEPE", "SHIB", "HYPE", "ONDO", "ENA"]
        for asset in assets:
            try:
                c = await adapter.fetch_candles(asset, "1h", 300)
                if c and len(c) >= 100:
                    candle_map[asset] = c
                    print(f"  {asset}: {len(c)} candles (live)")
                else:
                    print(f"  {asset}: insufficient data ({len(c) if c else 0})")
            except Exception as e:
                print(f"  {asset}: error: {e}")

    print(f"Loaded {len(candle_map)} assets")

    if not candle_map:
        print("ERROR: No candle data loaded. Cannot run backtest.")
        return
    print(f"Running backtest on {len(candle_map)} assets...")

    sweep = "--sweep" in sys.argv

    if sweep:
        # Param sweep: vary min stop
        best_score = -999.0
        best_results = None
        best_label = ""
        for stop in [0.03, 0.05, 0.08]:
            global STOP_PCT
            STOP_PCT = stop
            label = f"stop={stop:.0%}"
            results = run_backtest(list(candle_map.keys()), candle_map)
            # Score: total PnL excluding strategies with < 5 trades
            score = sum(r["pnl"] * min(r["trades"], 50) for r in results.values())
            weight = sum(min(r["trades"], 50) for r in results.values())
            avg_score = score / weight if weight else 0
            print(f"  {label}: weighted PnL={avg_score:.2f}")
            for s, r in results.items():
                if r["trades"] > 0:
                    print(f"    {s}: {r['trades']}t, ${r['pnl']}, WR {r['wr']:.1%}, avg_r {r['avg_r']:.3f}")
            if avg_score > best_score:
                best_score = avg_score
                best_results = results
                best_label = label
        print(f"\n=== Best: {best_label} ===")
        for s, r in best_results.items():
            if r["trades"] > 0:
                print(f"  {s}: {r['trades']}t, ${r['pnl']}, WR {r['wr']:.1%}, avg_r {r['avg_r']:.3f}")
    else:
        results = run_backtest(list(candle_map.keys()), candle_map)
        for s, r in results.items():
            print(f"{s}: {r['trades']}t, ${r['pnl']} PnL, WR {r['wr']:.1%}, avg_r {r['avg_r']:.3f}, Sharpe {r['sharpe']:.2f}")

    # Save
    with open("/tmp/backtest_result.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved to /tmp/backtest_result.json")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
