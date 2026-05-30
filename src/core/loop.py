"""
Main trading loop — Hyperliquid perps with semi-auto mode.

60s cadence:
  1. Fetch market data (prices, candels, funding, OI) for universe
  2. Detect regime per asset
  3. Evaluate signal ensemble
  4. Check perp-aware risk gates
  5. Run strategies for entries
  6. Run exits for open positions
  7. Journal all signals + decisions to DB
  8. Snapshot equity
  9. Weekly reflection on Sunday
"""

import asyncio
import json
import logging
import os
import signal as sig
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from src.adapters.altfins import AltfinsAdapter
from src.adapters.hyperliquid import HyperliquidAdapter
from src.adapters.paper_perp import PaperPerpExchange
from src.core.perp_risk import PerpRiskManager
from src.core.reflect import SignalTracker, WeeklyReflector
from src.core.types import (
    Order,
    OrderType,
    PerpCandle,
    PerpPosition,
    RegimeType,
    Side,
    Signal,
    TradeRecord,
)
from src.store.sqlite import Store
from src.strategies.base import PerpStrategy
from src.strategies.mr import MeanReversion
from src.strategies.trend import TrendFollow

logger = logging.getLogger("hermes.loop")

ASSET_UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT"]
TREND_UNIVERSE = {"BTC", "ETH", "SOL"}


class TradingLoop:
    def __init__(self, config: dict, data_dir: Path):
        self.config = config
        self.data_dir = data_dir
        self.running = False
        self._cycle_count = 0
        self._last_reflection: Optional[datetime] = None

        store_path = data_dir / config.get("store", {}).get("path", "hermes.db")
        self.store = Store(store_path)

        eq = config.get("exchange", {}).get("initial_balance", 10_000.0)
        self.risk = PerpRiskManager(initial_equity=eq)

        signal_path = data_dir / config.get("signal_tracker", {}).get("path", "signals.json")
        self.signal_tracker = SignalTracker(signal_path)
        self.reflector = WeeklyReflector(self.signal_tracker)

        self.strategies: list[PerpStrategy] = [
            MeanReversion(),
            TrendFollow(),
        ]

        self.assets = list(
            config.get("strategies", {})
            .get("mean_reversion", {})
            .get("assets", ASSET_UNIVERSE)
        )
        self.candle_cache: dict[str, list[PerpCandle]] = {}
        self.signal_cache: dict[str, list[Signal]] = defaultdict(list)
        self._stop_event = asyncio.Event()
        self._suggested_params: list[dict] = []
        self._daily_signals_log: list[dict] = []
        self._altfins_cycle = 0
        self._altfins = None

    async def start(self):
        self.running = True
        exchange = PaperPerpExchange(self.config.get("exchange", {}).get("initial_balance", 10_000.0))
        hl = HyperliquidAdapter(
            wallet_address=self.config.get("hyperliquid", {}).get("wallet", ""),
            private_key=self.config.get("hyperliquid", {}).get("private_key", ""),
            testnet=bool(self.config.get("hyperliquid", {}).get("testnet", False)),
        )

        loop = asyncio.get_event_loop()
        for s in (sig.SIGINT, sig.SIGTERM):
            try:
                loop.add_signal_handler(s, self._stop_event.set)
            except NotImplementedError:
                pass

        altfins_key = self.config.get("altfins", {}).get("api_key", "") or os.environ.get("ALTFINS_API_KEY", "")
        if altfins_key:
            self._altfins = AltfinsAdapter(altfins_key)
            logger.info("Altfins: enabled")
        else:
            logger.info("Altfins: disabled (no API key)")

        ws_task = asyncio.create_task(hl.connect_ws())

        logger.info("=== Hermes v2 — Hyperliquid Perps ===")
        logger.info("Assets: %s | Strategies: MR + Trend | Mode: semi-auto", len(self.assets))

        while self.running and not self._stop_event.is_set():
            try:
                await self._cycle(hl, exchange)
            except Exception as e:
                logger.exception("Cycle error: %s", e)
            self._cycle_count += 1
            await asyncio.sleep(60)

        ws_task.cancel()
        await hl.close()
        await exchange.close()
        self.store.close()
        logger.info("Shutdown complete")

    async def _cycle(self, hl: HyperliquidAdapter, exchange: PaperPerpExchange):
        # 1. Fetch market data
        try:
            mids = await hl.fetch_all_mids()
            for asset in self.assets:
                price = mids.get(asset, 0.0)
                if price > 0:
                    exchange.update_price(asset, price)
        except Exception as e:
            logger.debug("fetch mids: %s", e)

        # 2. Fetch candles
        for asset in self.assets:
            try:
                candles = await hl.fetch_candles(asset, "1h", 200)
                if candles:
                    self.candle_cache[asset] = candles
            except Exception as e:
                logger.debug("fetch candles %s: %s", asset, e)

        # 2b. Altfins signals (every 5 cycles = 5 min to save API credits)
        self._altfins_cycle += 1
        if self._altfins and self._altfins_cycle % 5 == 0:
            try:
                altfins_signals = await self._altfins.get_all_signals(self.assets)
                for sig in altfins_signals:
                    existing = self.signal_cache.get(sig.asset, [])
                    existing = [s for s in existing if s.source != sig.source]
                    existing.append(sig)
                    self.signal_cache[sig.asset] = existing[-20:]
            except Exception as e:
                logger.debug("altfins fetch: %s", e)

        # 3. Fetch funding + OI
        try:
            funding = await hl.fetch_funding()
            for asset, rate in funding.items():
                exchange.update_funding(asset, rate)
            oi_data = await hl.fetch_open_interest()
            for asset, oi in oi_data.items():
                exchange.update_open_interest(asset, oi)
                self.risk.record_oi(asset, oi)
        except Exception as e:
            logger.debug("fetch funding/oi: %s", e)

        # 4. Fetch perp configs
        try:
            configs = await hl.fetch_metadata()
            self.risk.set_perp_configs(configs)
            for asset, cfg in configs.items():
                exchange.set_perp_config(asset, cfg)
        except Exception as e:
            logger.debug("fetch meta: %s", e)

        # 5. Process each asset
        for asset in self.assets:
            candles = self.candle_cache.get(asset, [])
            if not candles:
                continue
            try:
                await self._process_asset(asset, candles, hl, exchange)
            except Exception as e:
                logger.exception("asset %s: %s", asset, e)

        # 6. Daily signal journal
        if self._daily_signals_log:
            self.store.put_state("daily_signals", self._daily_signals_log[-500:])
            self._daily_signals_log.clear()

        # 7. Weekly reflection
        await self._maybe_reflect(exchange)

        # 8. Equity snapshot
        eq = exchange.equity
        ge = exchange.gross_exposure
        self.risk.update_equity(eq, ge)
        self.store.save_equity_snapshot(eq, self.risk.peak_equity)

    async def _process_asset(
        self,
        asset: str,
        candles: list[PerpCandle],
        hl: HyperliquidAdapter,
        exchange: PaperPerpExchange,
    ):
        pos = await exchange.fetch_position(asset)
        price = await exchange.fetch_price(asset)

        funding_rate = hl._latest_funding.get(asset, 0.0)
        oi_vel = self.risk.oi_velocity(asset)
        altfins_sigs = self.signal_cache.get(asset, [])

        if pos and exchange.check_liquidation(asset):
            logger.warning("%s liquidated", asset)
            return

        # Check exits first
        if pos and price > 0:
            for strat in self.strategies:
                result = strat.should_exit(asset, pos, price, candles, funding_rate)
                if result:
                    reason, limit = result
                    await self._close(asset, pos, price, reason, exchange)
                    return

        # Check risk gates
        risk_ok, risk_msg = self.risk.allow_entry(exchange.gross_exposure, exchange.effective_leverage)
        if not risk_ok:
            return

        oi_ok, oi_msg = self.risk.oi_gate_allows(asset)
        if not oi_ok:
            return

        funding_ok, funding_msg = self.risk.funding_gate(funding_rate)
        if not funding_ok:
            return

        # Evaluate entries
        for strat in self.strategies:
            sig_bucket = f"{strat.name()}:{asset}"
            all_signals = altfins_sigs + self.signal_cache.get("all", [])
            result = strat.should_enter(asset, candles, all_signals, RegimeType.RANDOM_WALK, pos, funding_rate)
            if result is None:
                continue

            side, confidence, meta = result

            # Leverage + stop sizing
            lev, lev_reason = self.risk.compute_leverage(asset, candles, side)
            stop_pct, stop_reason = self.risk.compute_stop_distance(asset, candles)

            if stop_pct <= 0:
                continue

            entry_price = meta.get("entry_price", price)
            qty, risk_dollars, max_notional = self.risk.position_size(
                asset, exchange.equity, stop_pct, entry_price
            )

            if qty <= 0:
                continue

            stop_price = entry_price * (1 - stop_pct / 100)

            # Journal the signal
            signal_entry = {
                "time": datetime.now(timezone.utc).isoformat(),
                "asset": asset,
                "strategy": strat.name(),
                "side": side.value,
                "confidence": round(confidence, 3),
                "entry_price": round(entry_price, 2),
                "stop_price": round(stop_price, 2),
                "stop_pct": round(stop_pct, 2),
                "leverage": lev,
                "lev_reason": lev_reason,
                "stop_reason": stop_reason,
                "quantity": round(qty, 6),
                "risk_dollars": round(risk_dollars, 2),
                "oi_velocity": round(oi_vel, 1),
                "funding_rate": round(funding_rate, 6),
                "meta": {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool, list))},
            }
            self._daily_signals_log.append(signal_entry)
            self.store.put_state(f"last_signal_{asset}", signal_entry)

            logger.info(
                "SIGNAL %s %s %s | conf=%.2f entry=%.2f stop=%.2f(%.1f%%) lev=%.1fx qty=%.4f risk=$%.0f OI=%.1f%% fund=%.4f",
                side.value.upper(), asset, strat.name(), confidence,
                entry_price, stop_price, stop_pct, lev, qty, risk_dollars,
                oi_vel, funding_rate,
            )

            if self._suggested_params:
                self.store.put_state("pending_param_changes", self._suggested_params)
                logger.info("PENDING PARAM CHANGES: %d suggestions", len(self._suggested_params))

    async def _close(
        self,
        asset: str,
        pos: PerpPosition,
        price: float,
        reason: str,
        exchange: PaperPerpExchange,
    ):
        await exchange.place_order(Order(
            asset=asset,
            side=pos.side.opposite,
            order_type=OrderType.MARKET,
            quantity=pos.size,
            reduce_only=True,
        ))

        pnl_pct = pos.pnl_pct
        pnl_dollars = pos.unrealized_pnl
        r_mult = ((price - pos.entry_price) / pos.entry_price * pos.leverage) if pos.entry_price > 0 else 0.0

        self.risk.record_trade(asset, pnl_pct, pnl_dollars)
        self.signal_tracker.record(pos.signal_source, pnl_pct > 0)

        trade = TradeRecord(
            asset=asset,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=price,
            size=pos.size,
            leverage=pos.leverage,
            pnl_pct=round(pnl_pct, 2),
            pnl_dollars=round(pnl_dollars, 2),
            fees=0.0,
            funding_paid=0.0,
            exit_reason=reason,
            strategy=pos.strategy,
            signal_source=pos.signal_source,
            entry_confidence=pos.entry_confidence,
            entry_time=pos.entry_time,
            exit_time=datetime.now(timezone.utc),
            r_multiple=round(r_mult, 3),
        )
        self.store.save_trade(trade.__dict__)

        logger.info(
            "EXIT %s %s price=%.2f pnl=%.1f%% r=%.2f reason=%s",
            asset, pos.side.value.upper(), price, pnl_pct, r_mult, reason,
        )

    async def _maybe_reflect(self, exchange: PaperPerpExchange):
        now = datetime.now(timezone.utc)
        if self._last_reflection and (now - self._last_reflection).days < 7:
            return
        if now.weekday() != 6:  # Sunday
            return
        if now.hour < 12 or now.hour > 14:
            return

        self._last_reflection = now
        trades_raw = self.store.trades(limit=500)
        trades = []
        for t in trades_raw:
            try:
                trades.append(TradeRecord(
                    asset=t.get("asset", ""),
                    side=Side(t.get("side", "long")),
                    entry_price=float(t.get("entry_price", 0)),
                    exit_price=float(t.get("exit_price", 0)),
                    size=float(t.get("size", 0)),
                    leverage=float(t.get("leverage", 1)),
                    pnl_pct=float(t.get("pnl_pct", 0)),
                    pnl_dollars=float(t.get("pnl_dollars", 0)),
                    fees=float(t.get("fees", 0)),
                    funding_paid=float(t.get("funding_paid", 0)),
                    exit_reason=t.get("exit_reason", ""),
                    strategy=t.get("strategy", ""),
                    signal_source=t.get("signal_source", ""),
                    entry_confidence=float(t.get("entry_confidence", 0)),
                    entry_time=datetime.fromisoformat(t.get("entry_time", "2025-01-01T00:00:00")),
                    exit_time=datetime.fromisoformat(t.get("exit_time", "2025-01-01T00:00:00")),
                ))
            except Exception:
                continue

        params = {
            "rsi_oversold": 28.0,
            "cooldown_bars": 12,
        }

        reflection = self.reflector.reflect(trades, params)
        self.store.put_state("weekly_reflection", reflection)
        self._suggested_params = reflection["suggestions"]

        logger.info("=== WEEKLY REFLECTION ===")
        logger.info("Trades: %d | Sharpe: %.2f | Win rate: %.0f%%",
                     reflection["metrics"].get("total_trades", 0),
                     reflection["metrics"].get("sharpe", 0),
                     reflection["metrics"].get("win_rate", 0) * 100)
        for s in reflection["suggestions"]:
            logger.info("  SUGGEST: %s = %.2f (was %.2f) — %s [conf=%.2f]",
                         s["parameter"], s["suggested_value"],
                         s["current_value"], s["reason"], s["confidence"])
        if reflection["needs_human_review"]:
            logger.info("  ⚠ Human review recommended for low-confidence suggestions")
        logger.info("===========================")
