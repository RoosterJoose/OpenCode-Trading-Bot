"""
Main trading loop — perps with semi-auto mode.

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
import math
import time
import statistics
import os
import signal as sig
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from src.adapters.altfins import AltfinsAdapter
from src.adapters.base import ExchangeAdapter
from src.adapters.coinbase_advanced import CoinbaseAdvancedAdapter
from src.adapters.kalshi import KalshiAdapter
from src.adapters.paper_perp import PaperPerpExchange
from src.core.telegram_bot import TelegramBot
from src.core.intents import TradeIntent
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
from src.strategies.donchian import DonchianBreakout
from src.strategies.xs_momentum import CrossSectionalMomentum
from src.strategies.momentum import DriftMomentum

logger = logging.getLogger("hermes.loop")

ASSET_UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK",
                    "DOT", "AAVE", "LTC", "NEAR", "SUI", "XLM", "HBAR", "BCH",
                    "ZEC", "PEPE", "SHIB", "HYPE", "ONDO", "ENA"]
MIN_ENTRY_CONFIDENCE = 0.70


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
            MeanReversion(signal_tracker=self.signal_tracker),
            TrendFollow(signal_tracker=self.signal_tracker),
            DonchianBreakout(signal_tracker=self.signal_tracker),
            CrossSectionalMomentum(signal_tracker=self.signal_tracker),
            DriftMomentum(signal_tracker=self.signal_tracker),
        ]

        self.assets = list(
            config.get("strategies", {})
            .get("mean_reversion", {})
            .get("assets", ASSET_UNIVERSE)
        )
        self.candle_cache: dict[str, list[PerpCandle]] = {}
        self.candle_4h_cache: dict[str, list[PerpCandle]] = {}
        self._restore_candle_cache()  # 4h aggregated for regime detection
        self._asset_coverage_warned: bool = False
        self.signal_cache: dict[str, list[Signal]] = defaultdict(list)
        self._stop_event = asyncio.Event()
        self._suggested_params: list[dict] = []
        self._daily_signals_log: list[dict] = []
        self._altfins_cycle = 0
        self._cusum: dict[str, dict] = {}  # per-asset CUSUM state: {"S_high": 0, "S_low": 0, "mean": 0, "std": 1, "n": 0}
        self._altfins = None
        self._kalshi = None
        self._kalshi_funding = {}
        self._strategy_budget = {}
        self._ic_budget_cycle = 0
        self._block_reasons: dict[str, int] = {}
        token = self.config.get("telegram", {}).get("bot_token") or os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
        chat_id = self.config.get("telegram", {}).get("chat_id") or os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
        self.telegram = TelegramBot(token, chat_id, self.store)
        self.notifier = self.telegram
        self._sent_alerts: dict[str, float] = {}  # rate-limit alerts (key -> timestamp)

    def _restore_paper_positions(self, exchange: PaperPerpExchange):
        positions = self.store.get_state("positions") or []
        exchange.restore_positions(positions)
        for pos in exchange.positions.values():
            self.risk.record_position_open(pos.asset)
        if exchange.positions:
            logger.info("Restored %d paper position(s)", len(exchange.positions))

    async def start(self):
        self.running = True
        saved_eq = self.store.get_state("paper_equity")
        initial = float(saved_eq) if saved_eq else self.config.get("exchange", {}).get("initial_balance", 10_000.0)
        exchange = PaperPerpExchange(initial_balance=initial)
        self._restore_paper_positions(exchange)
        self._restore_risk_state()
        asyncio.ensure_future(self.notifier.bot_started(exchange.equity))
        asyncio.ensure_future(self.telegram.start_polling())
        saved_peak = self.store.get_state("paper_peak_equity")
        if saved_peak:
            self.risk.peak_equity = max(float(saved_peak), initial)
            self.risk.current_equity = initial
        hl = CoinbaseAdvancedAdapter(
            api_key_id=self.config.get("coinbase", {}).get("api_key_id", ""),
            private_key=self.config.get("coinbase", {}).get("private_key", ""),
            portfolio_uuid=self.config.get("coinbase", {}).get("portfolio_uuid", ""),
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
        try:
            saved_ac = self.store.get_state("altfins_cycle")
            if saved_ac:
                self._altfins_cycle = int(saved_ac)
        except Exception:
            pass

        # Restore IC budget cycle counter
        try:
            saved_ic = self.store.get_state("ic_budget_cycle")
            if saved_ic:
                self._ic_budget_cycle = int(saved_ic)
        except Exception:
            pass


        kalshi_key_id = self.config.get("kalshi", {}).get("api_key_id", "")
        kalshi_pk = self.config.get("kalshi", {}).get("private_key", "") or os.environ.get("KALSHI_PRIVATE_KEY", "")
        if kalshi_key_id and kalshi_pk:
            try:
                self._kalshi = KalshiAdapter(
                    api_key_id=kalshi_key_id,
                    private_key_pem=kalshi_pk,
                    base_url="https://external-api.kalshi.com",
                )
                logger.info("Kalshi: enabled (11 assets)")
            except Exception as e:
                logger.warning("Kalshi: failed to initialize (%s), continuing without it", e)
                self._kalshi = None
        else:
            logger.info("Kalshi: disabled (no API key)")

        ws_task = asyncio.create_task(hl.connect_ws())

        logger.info("=== Hermes v2 — Coinbase Perps ===")
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
        if self._kalshi:
            await self._kalshi.close()
        await exchange.close()
        self.store.close()
        logger.info("Shutdown complete")

    def _restore_candle_cache(self) -> None:
        """Restore persisted candle cache on startup to avoid silent data loss on restart."""
        restored = 0
        for asset in self.assets:
            try:
                candles = self.store.load_candles(asset, max_bars=250)
                if candles:
                    self.candle_cache[asset] = candles
                    restored += 1
            except Exception as e:
                logger.debug("restore candles %s: %s", asset, e)
        if restored:
            logger.info("RESTORED candle cache: %d/%d assets from DB", restored, len(self.assets))
            # Also rebuild 4h cache from restored data where possible
            for asset, candles in self.candle_cache.items():
                if len(candles) >= 200:
                    try:
                        self.candle_4h_cache[asset] = self._aggregate_to_4h(candles)
                    except Exception:
                        pass
        else:
            logger.info("No cached candles found — will fetch fresh from exchange")

    def _import_file_intents(self):
        intent_dir = self.data_dir / "intents"
        done_dir = intent_dir / "done"
        invalid_dir = intent_dir / "invalid"
        if not intent_dir.exists():
            return
        done_dir.mkdir(parents=True, exist_ok=True)
        invalid_dir.mkdir(parents=True, exist_ok=True)
        for f in sorted(intent_dir.glob("*.json")):
            try:
                raw = f.read_text()
                data = json.loads(raw)
                saved = self.store.save_intent(data)
                f.rename(done_dir / f.name)
                if saved:
                    logger.info("Imported intent: %s %s %s",
                                data.get("asset", "?"), data.get("side", "?"), data.get("source", "?"))
                else:
                    logger.info("Skipped duplicate intent: %s", data.get("idempotency_key", f.name))
            except Exception as e:
                try:
                    f.rename(invalid_dir / f.name)
                except Exception:
                    pass
                logger.debug("intent import %s: %s", f.name, e)

    def _restore_risk_state(self) -> None:
        try:
            raw = self.store.get_state("risk_consecutive_losses")
            if raw:
                self.risk._consecutive_losses.update(json.loads(raw) if isinstance(raw, str) else raw)
            raw = self.store.get_state("risk_global_loss_streak")
            if raw:
                self.risk._global_loss_streak = int(raw)
            raw = self.store.get_state("risk_recent_outcomes")
            if raw:
                restored = json.loads(raw) if isinstance(raw, str) else raw
                self.risk._recent_outcomes = list(restored)
            if self.risk._recent_outcomes or self.risk._consecutive_losses:
                logger.info(
                    "Restored risk state: %d outcomes, %d consecutive-loss entries, global streak=%d",
                    len(self.risk._recent_outcomes),
                    len(self.risk._consecutive_losses),
                    self.risk._global_loss_streak,
                )
        except Exception as e:
            logger.debug("restore risk state: %s", e)

    def _save_risk_state(self) -> None:
        try:
            self.store.put_state("risk_consecutive_losses", json.dumps(dict(self.risk._consecutive_losses)))
            self.store.put_state("risk_global_loss_streak", str(self.risk._global_loss_streak))
            self.store.put_state("risk_recent_outcomes", json.dumps(self.risk._recent_outcomes))
        except Exception as e:
            logger.debug("save risk state: %s", e)

    def _restore_strategy_cooldowns(self) -> None:
        try:
            raw = self.store.get_state("strategy_cooldowns")
            if raw:
                saved = json.loads(raw) if isinstance(raw, str) else raw
                for strat in self.strategies:
                    if hasattr(strat, "_cooldowns") and strat.name() in saved:
                        strat._cooldowns = {
                            k: v for k, v in saved[strat.name()].items() if v > 0
                        }
                logger.info("Restored strategy cooldowns from DB")
        except Exception as e:
            logger.debug("restore cooldowns: %s", e)

    def _save_strategy_cooldowns(self) -> None:
        try:
            cooldowns = {}
            for s in self.strategies:
                if hasattr(s, "_cooldowns"):
                    cooldowns[s.name()] = dict(s._cooldowns)
            self.store.put_state("strategy_cooldowns", json.dumps(cooldowns))
        except Exception as e:
            logger.debug("save cooldowns: %s", e)

    def _send_alert_ratelimited(self, key: str, message: str, min_interval: float = 3600.0) -> None:
        now = time.time()
        last = self._sent_alerts.get(key, 0.0)
        if now - last >= min_interval:
            self._sent_alerts[key] = now
            asyncio.ensure_future(self.notifier.send(message))

    async def _cycle(self, hl: ExchangeAdapter, exchange: PaperPerpExchange):
        if self._cycle_count % 5 == 0:
            logger.info("heartbeat cycle=%d", self._cycle_count)

        # Self-heal 1: detect entry stall — if no ENTRY_DIAG for 20+ cycles (20 min), restart
        if not hasattr(self, "_last_entry_diag_cycle"):
            self._last_entry_diag_cycle = 0
        # The ENTRY_DIAG check will update this counter from _process_asset
        cycles_stalled = self._cycle_count - self._last_entry_diag_cycle
        if cycles_stalled > 20 and self._cycle_count > 30:
            logger.warning("SELF-HEAL: %d cycles without entry diagnostics — restarting", cycles_stalled)
            self.store.put_state("self_heal", json.dumps({"action": "restart", "reason": f"entry_stall_{cycles_stalled}cycles"}))
            os._exit(42)

        # Auto-pause check (sharpe_tracker runs daily at 00:05 UTC)
        try:
            paused = self.store.get_state("bot_paused")
            if paused == "true":
                reasons = self.store.get_state("pause_reasons") or "[]"
                logger.warning("BOT PAUSED by auto-pause logic: %s", reasons)
                self._send_alert_ratelimited("bot_paused", f"⚠️ BOT PAUSED: {reasons}", 7200.0)
                return
        except Exception as e:
            logger.debug("pause check failed: %s", e)
        # Load dynamic thresholds from closed_loop.py and inject into strategies
        try:
            raw = self.store.get_state("dynamic_thresholds")
            if raw:
                thresholds = json.loads(raw) if isinstance(raw, str) else raw
                for strat in self.strategies:
                    if hasattr(strat, "set_dynamic_thresholds"):
                        strat.set_dynamic_thresholds(thresholds)
        except Exception as e:
            logger.debug("dynamic thresholds: %s", e)
        # Load strategy budget from strategy_budget.py
        self._strategy_budget = {}
        self._ic_budget_cycle = 0
        self._block_reasons: dict[str, int] = {}
        token = self.config.get("telegram", {}).get("bot_token") or os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
        chat_id = self.config.get("telegram", {}).get("chat_id") or os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
        self.telegram = TelegramBot(token, chat_id, self.store)
        self.notifier = self.telegram
        try:
            raw = self.store.get_state("strategy_budget")
            if raw:
                sb = json.loads(raw) if isinstance(raw, str) else raw
                self._strategy_budget = sb.get("weights", {})
        except Exception as e:
            logger.debug("strategy budget: %s", e)
        self._restore_strategy_cooldowns()
        self._import_file_intents()
        # 1. Fetch market data
        try:
            mids = await hl.fetch_all_mids()
            for asset in self.assets:
                price = mids.get(asset, 0.0)
                if price > 0:
                    exchange.update_price(asset, price)
                    self.risk.record_price(asset, price)
        except Exception as e:
            logger.debug("fetch mids: %s", e)

        # 2. Fetch candles (parallel with semaphore) — 1h for signals, 4h for regime detection
        _candle_sema = asyncio.Semaphore(5)
        async def _fetch_one(asset: str) -> None:
            async with _candle_sema:
                try:
                    candles_1h = await asyncio.wait_for(hl.fetch_candles(asset, "1h", 250), timeout=30.0)
                    if candles_1h:
                        self.candle_cache[asset] = candles_1h
                        try:
                            self.store.save_candles(asset, candles_1h)
                        except Exception as e:
                            logger.debug("save candles %s: %s", asset, e)
                        # Build 4h aggregation for regime detection (NotebookLM round 10)
                        if len(candles_1h) >= 200:
                            self.candle_4h_cache[asset] = self._aggregate_to_4h(candles_1h)
                    else:
                        logger.warning("candle fetch %s: empty response", asset)
                except asyncio.TimeoutError:
                    logger.warning("candle fetch %s: timeout after 30s", asset)
                except Exception as e:
                    logger.warning("candle fetch %s: %s", asset, e)
        await asyncio.gather(*[_fetch_one(a) for a in self.assets])

        # Asset coverage invariant: every asset must have fresh candle data
        covered = sum(1 for a in self.assets if a in self.candle_cache and len(self.candle_cache[a]) > 0)
        if covered < len(self.assets):
            missing = [a for a in self.assets if a not in self.candle_cache or not self.candle_cache[a]]
            logger.warning("ASSET COVERAGE: %d/%d assets have candles — missing: %s", covered, len(self.assets), missing)
            self._asset_coverage_warned = True
            if covered < 20:
                await self.notifier.send(
                    f"⚠️ ASSET COVERAGE: {covered}/{len(self.assets)} assets active — {missing}"
                )
        else:
            self._asset_coverage_warned = False

        # 2a. Kalshi data supplement (parallel, fills gaps)
        self._kalshi_funding = {}
        if self._kalshi:
            try:
                kalshi_mids = await self._kalshi.fetch_all_mids()
                for asset, price in kalshi_mids.items():
                    if price > 0 and mids.get(asset, 0) == 0:
                        exchange.update_price(asset, price)
                        self.risk.record_price(asset, price)
                kalshi_oi = await self._kalshi.fetch_open_interest()
                for asset, oi in kalshi_oi.items():
                    exchange.update_open_interest(asset, oi)
                kalshi_fr = await self._kalshi.fetch_funding()
                self._kalshi_funding = kalshi_fr
                for asset, rate in kalshi_fr.items():
                    exchange.update_funding(asset, rate)
            except Exception as e:
                logger.debug("kalshi supplement: %s", e)

        # 2b. Altfins: decoupled cadences per NotebookLM
        #   - Screener (1h/15m): every 5 cycles (5 min) — primary intraday data
        #   - Signal Feed (1D): every 360 cycles (6h) — daily confirmation only
        self._altfins_cycle += 1
        if self._altfins:
            if self._altfins_cycle % 5 == 0:
                try:
                    indicator_sigs = await self._altfins.fetch_indicators_as_signals(self.assets)
                    for sig in indicator_sigs:
                        existing = self.signal_cache.get(sig.asset, [])
                        existing = [s for s in existing if s.source != sig.source]
                        existing.append(sig)
                        self.signal_cache[sig.asset] = existing[-20:]
                except Exception as e:
                    logger.warning("Altfins screener: %s", e)

            if self._altfins_cycle % 360 == 0:
                try:
                    altfins_sigs = await self._altfins.fetch_signals(self.assets)
                    for sig in altfins_sigs:
                        existing = self.signal_cache.get(sig.asset, [])
                        existing = [s for s in existing if s.source != sig.source]
                        existing.append(sig)
                        self.signal_cache[sig.asset] = existing[-20:]
                        logger.info("Altfins signal: %s %s %.2f %s",
                                     sig.asset, sig.direction.value.upper(),
                                     sig.confidence, sig.source)
                except Exception as e:
                    logger.warning("Altfins signals: %s", e)

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

        # 4b. Process external intents after all risk inputs are fresh.
        await self._process_external_intents(exchange, hl)

        # 4c. Export external data snapshot for Freqtrade lab
        try:
            altfins_signals = []
            for asset_sigs in self.signal_cache.values():
                for s in asset_sigs:
                    altfins_signals.append({
                        "asset": s.asset, "source": s.source,
                        "direction": s.direction.value,
                        "confidence": s.confidence,
                        "bucket": s.bucket,
                        "timestamp": s.timestamp.isoformat() if s.timestamp else None,
                    })
            altfins_indicators = {}
            permit_info = {}
            if self._altfins:
                altfins_indicators = self._altfins._cached_indicators
                permit_info = await self._altfins.check_permit_usage()
            snapshot = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "prices": dict(getattr(hl, "_latest_prices", {})),
                "funding": dict(getattr(exchange, "_funding_rates", {})),
                "oi": dict(getattr(exchange, "_open_interest", {})),
                "change_24h": dict(getattr(hl, "_latest_changes_24h", {})),
                "oi_velocity": {
                    a: self.risk.oi_velocity(a)
                    for a in self.assets
                },
                "altfins_signal_count": len(altfins_signals),
                "altfins_signals": altfins_signals[:50],
                "altfins_indicators": altfins_indicators,
                "altfins_permits": permit_info,
                "coinbase_requests": getattr(hl, "request_count", 0),
                "coinbase_rate_limited": getattr(hl, "_consecutive_429s", 0) > 0,
                "kalshi_enabled": self._kalshi is not None,
            }
            snapshot_path = self.data_dir / "external_snapshot.json"
            tmp = snapshot_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snapshot, indent=2, default=str))
            tmp.rename(snapshot_path)
        except Exception as e:
            logger.debug("snapshot export: %s", e)

        # 4b. Compute cross-sectional returns (NotebookLM round 10)
        try:
            xs_returns: dict[str, float] = {}
            for asset in self.assets:
                candles = self.candle_cache.get(asset, [])
                if len(candles) >= 169:  # 7 days + 1
                    ret = (candles[-1].close - candles[-168].close) / candles[-168].close
                    xs_returns[asset] = ret
            if xs_returns:
                CrossSectionalMomentum.set_returns(xs_returns)
                CrossSectionalMomentum._downtrend_override = self._btc_knife_block
        except Exception as e:
            logger.debug("xs returns: %s", e)

        # 5a. BTC falling-knife guard (once per cycle)
        btc_candles = self.candle_cache.get("BTC", [])
        self._btc_knife_block = False
        if len(btc_candles) >= 60:
            btc_adx = TradingLoop._adx(btc_candles)
            btc_closes = [c.close for c in btc_candles]
            btc_ema50 = TradingLoop._ema(btc_closes, 50)
            btc_price = btc_closes[-1]
            if btc_adx > 28 and btc_price < btc_ema50:
                self._btc_knife_block = True
                logger.info("BTC knife guard: ADX=%.1f price=%.0f < EMA50=%.0f",
                             btc_adx, btc_price, btc_ema50)

        # 5. Process each asset
        for asset in self.assets:
            candles = self.candle_cache.get(asset, [])
            if not candles:
                continue
            try:
                await self._process_asset(asset, candles, hl, exchange)
            except Exception as e:
                logger.exception("asset %s: %s", asset, e)

                # Cycle summary every 5 cycles
        if self._cycle_count % 5 == 0:
            logger.info('CYCLE_SUMMARY cycle=%d equity=$%.0f positions=%d altfins=%s knife=%s',
                         self._cycle_count,
                         exchange.equity if hasattr(exchange, 'equity') else 0,
                         len(exchange.positions),
                         'dead(429)' if self._altfins_cycle > 0 and not self._altfins else 'ok',
                         self._btc_knife_block)

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
        self.risk.set_gross_exposure(ge)
        self.store.save_equity_snapshot(eq, self.risk.peak_equity)
        self.store.put_state("paper_equity", str(exchange.balance))
        self.store.put_state("paper_peak_equity", str(self.risk.peak_equity))
        await self.notifier.daily_drawdown(eq, self.risk.peak_equity,
            (self.risk.peak_equity - eq) / self.risk.peak_equity * 100 if self.risk.peak_equity > 0 else 0)

        # IC budget refresh every 30 cycles
        self._ic_budget_cycle += 1
        if self._ic_budget_cycle % 30 == 0:
            try:
                from src.core.ic_allocator import compute_weights
                weights = compute_weights()
                budget = {"weights": weights, "source": "ic_rollingsharpe", "timestamp": time.time()}
                self.store.put_state("strategy_budget", json.dumps(budget))
                self._strategy_budget = weights
                logger.info("IC_BUDGET: %s", {k: round(v,3) for k, v in weights.items()})
            except Exception as e:
                logger.error("IC budget failed: %s", e)
        # Persist risk state + strategy cooldowns (survive restarts)
        # Self-heal 3: detect dominant block reason — if same reason for 60 cycles, auto-clear
        if hasattr(self, '_block_reasons') and self._block_reasons:
            top_reason = max(self._block_reasons, key=self._block_reasons.get)
            top_count = self._block_reasons[top_reason]
            total_blocks = sum(self._block_reasons.values())
            if total_blocks >= 30 and top_count / max(total_blocks, 1) > 0.8:
                logger.warning("SELF-HEAL: %s blocks dominate (%.0f%%) for %d cycles — clearing", 
                               top_reason, top_count/total_blocks*100, total_blocks)
                if "wr_halt" in top_reason:
                    self.risk._recent_outcomes = []
                    self.store.put_state("risk_recent_outcomes", "[]")
                elif "gs_halt" in top_reason or "loss_streak" in top_reason:
                    self.risk.global_loss_streak = 0
                    self.store.put_state("risk_global_loss_streak", "0")
                elif "lev_halt" in top_reason and self._block_reasons.get(top_reason, 0) > 60:
                    logger.warning("SELF-HEAL: lev_halt stale for 30+ cycles — clearing")
                    self.store.put_state("equity_snapshots", json.dumps({"eq": 0, "peak": 0}))
                self._block_reasons = {}
        
        # Clear block reasons periodically to prevent unbounded growth
        if self._cycle_count % 60 == 0:
            self._block_reasons = {}

        # Self-heal 4: loss streak auto-reset — if stale and blocked, clear
        gs = getattr(self.risk, 'global_loss_streak', 0)
        if gs >= 5 and self._cycle_count - self._last_entry_diag_cycle > 30:
            logger.warning("SELF-HEAL: GS=%d stale for 30+ cycles — clearing", gs)
            self._global_loss_streak = 0
            self.risk.global_loss_streak = 0
            self.store.put_state("risk_global_loss_streak", "0")
        self._save_risk_state()
        self._save_strategy_cooldowns()
        self.store.put_state("altfins_cycle", str(self._altfins_cycle))
        self.store.put_state("ic_budget_cycle", str(self._ic_budget_cycle))
        self.store.put_state("positions", [
            {
                "asset": p.asset,
                "side": p.side.value,
                "entry_price": p.entry_price,
                "size": p.size,
                "leverage": p.leverage,
                "liquidation_price": p.liquidation_price,
                "unrealized_pnl": p.unrealized_pnl,
                "realized_pnl": p.realized_pnl,
                "entry_time": p.entry_time.isoformat(),
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
                "strategy": p.strategy,
                "signal_source": p.signal_source,
                "entry_confidence": p.entry_confidence,
                "component_sources": p.component_sources,
                "regime": getattr(p, "regime", ""),
                "entry_regime": getattr(p, "entry_regime", ""),
            }
            for p in exchange.positions.values()
        ])
        # Periodic WAL checkpoint every 5 cycles to prevent WAL bloat
        if self._cycle_count % 5 == 0:
            try:
                self.store._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass

    async def _process_asset(
        self,
        asset: str,
        candles: list[PerpCandle],
        hl: ExchangeAdapter,
        exchange: PaperPerpExchange,
    ):
        # Self-heal: candle freshness + quality check
        if candles:
            try:
                newest = max(c.timestamp for c in candles)
                age_h = (time.time() - newest) / 3600
                if age_h > 2:
                    logger.warning("STALE_CANDLES %s: %.1fh old — skipping", asset, age_h)
                    return
                bad = [c for c in candles if c.high <= c.low or c.close <= 0]
                if len(bad) > len(candles) * 0.5:
                    logger.warning("CORRUPT_CANDLES %s: %d/%d corrupted — skipping", asset, len(bad), len(candles))
                    return
            except Exception:
                pass

        pos = await exchange.fetch_position(asset)
        price = await exchange.fetch_price(asset)

        funding_rate = await hl.get_funding_rate(asset)
        if self._kalshi and self._kalshi_funding:
            kalshi_fr = self._kalshi_funding.get(asset)
            if kalshi_fr is not None:
                funding_rate = max(funding_rate, kalshi_fr, key=abs) if funding_rate else kalshi_fr
        oi_vel = self.risk.oi_velocity(asset)
        altfins_sigs = self.signal_cache.get(asset, [])

        # Track MAE and MFE: Max Adverse / Favorable Excursion
        if pos:
            if not getattr(pos, 'mae_pct', None): pos.mae_pct = 0.0
            if not getattr(pos, 'mfe_pct', None): pos.mfe_pct = 0.0
            if pos.side == Side.LONG:
                worst = pos.entry_price - price
                best = price - pos.entry_price
            else:
                worst = price - pos.entry_price
                best = pos.entry_price - price
            current_mae = worst / pos.entry_price * 100 if pos.entry_price > 0 else 0
            current_mfe = best / pos.entry_price * 100 if pos.entry_price > 0 else 0
            if current_mae > (getattr(pos, 'mae_pct', 0) or 0):
                pos.mae_pct = current_mae
            if current_mfe > (getattr(pos, 'mfe_pct', 0) or 0):
                pos.mfe_pct = current_mfe

        # Position concentration check: max 50% equity in one position
        if pos:
            notional = abs(pos.size) * (pos.entry_price if pos.entry_price > 0 else 1)
            max_notional = exchange.equity * 0.50 if hasattr(exchange, 'equity') else 999999
            if notional > max_notional:
                logger.warning("CONCENTRATION_HALT %s: notional=%.0f > 50%%%% eq=%.0f", asset, notional, max_notional)
                return

        if pos and exchange.check_liquidation(asset):
            logger.warning("%s liquidated", asset)
            return

        # Emergency close_all check
        if self.store.get_state("close_all_pending") == '"true"' and pos and price > 0:
            logger.warning("CLOSE_ALL: closing %s %s at %.2f", asset, pos.side.value, price)
            await self._close(asset, pos, price, "close_all", exchange)
            self.store.put_state("close_all_pending", '"false"')
            return

        # Check exits first
        if pos and price > 0:
            if not pos.strategy:
                logger.debug("%s: skipping exit — no strategy on position", asset)
            else:
                for strat in self.strategies:
                    if strat.name() != pos.strategy:
                        continue
                    result = strat.should_exit(asset, pos, price, candles, funding_rate)
                    if result:
                        reason, limit = result
                        await self._close(asset, pos, price, reason, exchange)
                        return

                # BTC knife-guard time-stop: close longs held >60min during confirmed downtrend
                if self._btc_knife_block and pos.side == Side.LONG:
                    age = datetime.now(timezone.utc) - pos.entry_time
                    if age.total_seconds() > 3600:
                        logger.warning("KNIFE_TIMESTOP %s: long held %.0f min >60 min during BTC knife guard -- closing",
                                       asset, age.total_seconds() / 60)
                        await self._close(asset, pos, price, "knife_guard_time_exit", exchange)
                        return

        # Dual regime (NotebookLM): primary (200-period) for sizing/risk,
        # secondary (50-period) for entry direction
        # Regime detection: use 4h aggregated candles (NotebookLM round 10)
        # 1h Hurst only covers 2 days; 4h covers 8 days — matches trend timeframe
        candles_4h = self.candle_4h_cache.get(asset, [])
        if len(candles_4h) >= 30:
            primary_regime = self._infer_regime(asset, candles_4h, 30)
            regime = self._infer_regime(asset, candles_4h, 30)
        else:
            primary_regime = self._infer_regime(asset, candles, 50)
            regime = self._infer_regime(asset, candles, 30)

        # Dead market — skip entries entirely
        if regime == RegimeType.DEAD_MARKET and not pos:
            return

        # Check risk gates (primary regime influences risk budget)
        risk_ok, risk_msg = self.risk.allow_entry(exchange.gross_exposure, exchange.effective_leverage)
        if not risk_ok:
            self._last_entry_diag_cycle = self._cycle_count
            reason_key = f"risk:{risk_msg[:60]}"
            self._block_reasons[reason_key] = self._block_reasons.get(reason_key, 0) + 1
            logger.info("ENTRY_DIAG %s: skip -- risk: %s", asset, risk_msg)
            if "wr_halt" in risk_msg or "daily_loss" in risk_msg or "loss_streak" in risk_msg:
                self._send_alert_ratelimited("risk_halt", f"⚠️ RISK HALT: {risk_msg}", 3600.0)
            return

        oi_ok, oi_msg = self.risk.oi_gate_allows(asset)
        if not oi_ok:
            self._last_entry_diag_cycle = self._cycle_count
            reason_key = f"OI:{oi_msg[:50]}"
            self._block_reasons[reason_key] = self._block_reasons.get(reason_key, 0) + 1
            logger.info("ENTRY_DIAG %s: skip -- OI: %s", asset, oi_msg)
            return

        # Bid-ask spread micro-filter (NotebookLM: block if > 0.08%)
        spread_pct = hl.get_spread(asset)
        spread_ok, spread_msg = self.risk.spread_gate_allows(asset, spread_pct)
        if not spread_ok:
            self._last_entry_diag_cycle = self._cycle_count
            reason_key = f"spread:{spread_msg[:50]}"
            self._block_reasons[reason_key] = self._block_reasons.get(reason_key, 0) + 1
            logger.info("ENTRY_DIAG %s: skip -- spread: %s", asset, spread_msg)
            return

        funding_ok, funding_msg = self.risk.funding_gate(funding_rate)
        if not funding_ok:
            self._last_entry_diag_cycle = self._cycle_count
            reason_key = f"funding:{funding_msg[:50]}"
            self._block_reasons[reason_key] = self._block_reasons.get(reason_key, 0) + 1
            logger.info("ENTRY_DIAG %s: skip -- funding: %s", asset, funding_msg)
            return

        cl_ok, cl_msg = self.risk.consecutive_loss_allows(asset)
        if not cl_ok:
            return

        # Evaluate entries
        for strat in self.strategies:
            sig_bucket = f"{strat.name()}:{asset}"
            all_signals = altfins_sigs + self.signal_cache.get("all", [])
            # Kalshi OI surge as breakout confirmation signal
            if self._kalshi and oi_vel > 15:
                oi_signal = Signal(
                    source="kalshi:oi_surge",
                    asset=asset,
                    direction=Side.LONG,
                    confidence=min(abs(oi_vel) / 100, 1.0),
                    timestamp=datetime.now(timezone.utc),
                    bucket="breakout_confirmation",
                )
                all_signals.append(oi_signal)
            result = strat.should_enter(asset, candles, all_signals, regime, pos, funding_rate)
            if result is None:
                continue

            side, confidence, meta = result
            if confidence < MIN_ENTRY_CONFIDENCE:
                # MR sleeve can use lower threshold (0.55) while trend stays at 0.70
                mr_min = 0.55
                if confidence < mr_min or strat.name() != "mr":
                    continue
            # Absolute floor for non-MR strategies (trend/donchian/need >= 0.70 regardless of global MIN)
            if strat.name() != "mr" and confidence < 0.70:
                logger.debug("%s %s: trend confidence %.2f < 0.70 floor", strat.name(), asset, confidence)
                continue

            # BTC falling-knife guard: block ALL longs during strong BTC downtrend
            if self._btc_knife_block and side == Side.LONG:
                logger.debug("%s %s: blocked by BTC falling-knife guard (longs blocked)",
                             strat.name(), asset)
                continue

            # Leverage + stop sizing
            lev, lev_reason = self.risk.compute_leverage(asset, candles, side)
            stop_pct, stop_reason = self.risk.compute_stop_distance(asset, candles)

            if stop_pct <= 0:
                continue

            entry_price = price
            qty, risk_dollars, max_notional = self.risk.position_size(
                asset, exchange.equity, stop_pct, entry_price, exchange.gross_exposure
            )

            if qty <= 0:
                continue

            # Strategy budget scaling (based on 30d Sharpe)
            strat_name = strat.name()
            budget_weight = self._strategy_budget.get(strat_name, 1.0)
            if budget_weight <= 0:
                continue
            if budget_weight < 1.0:
                qty = qty * budget_weight
                logger.debug(
                    "%s %s: budget=%s qty=%s -> %s",
                    strat_name, asset, budget_weight, qty / budget_weight, qty,
                )

            if side == Side.SHORT:
                stop_price = entry_price * (1 + stop_pct / 100)
            else:
                stop_price = entry_price * (1 - stop_pct / 100)

            # Structural stop anchor (NotebookLM): use 5-bar swing low/high as tighter invalidation
            if len(candles) >= 5:
                if side == Side.SHORT:
                    swing_high = max(c.high for c in candles[-5:])
                    stop_price = min(swing_high, stop_price)
                else:
                    swing_low = min(c.low for c in candles[-5:])
                    stop_price = max(swing_low, stop_price)

            # Journal the signal
            signal_entry = {
                "time": datetime.now(timezone.utc).isoformat(),
                "asset": asset,
                "strategy": strat.name(),
                "side": side.value,
                "confidence": round(confidence, 3),
                "entry_price": round(entry_price, 6),
            "stop_price": round(stop_price, 6),
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

            # Execute paper trade
            if strat.name() == "mr" or strat.name() == "donchian":
                logger.info("ENTRY_ATTEMPT %s %s conf=%.2f qty=%.4f stop=%.2f entry=%.2f",
                             asset, side.value, confidence, qty, stop_price, entry_price)
            order = Order(
                asset=asset,
                side=side,
                order_type=OrderType.MARKET,
                quantity=qty,
                stop_price=stop_price,
                reduce_only=False,
                leverage=lev,
                metadata={"component_sources": meta.get("component_sources", [])},
            )
            order_id = await exchange.place_order(order)
            if order_id:
                pos = exchange.positions.get(asset)
                if pos:
                    pos.strategy = strat.name()
                    pos.signal_source = f"{strat.name()}:{asset}"
                    pos.entry_confidence = confidence
                    pos.stop_loss = stop_price
                    pos.component_sources = list(meta.get("component_sources", []))
                    pos.regime = regime.value if hasattr(regime, "value") else str(regime)
                    pos.entry_regime = pos.regime
                self.risk.record_position_open(asset)
                asyncio.ensure_future(self.notifier.position_opened(
                    asset, side.value.upper(), entry_price, qty, lev, confidence, strat.name(),
                ))
                logger.info(
                    "PAPER %s %s qty=%.4f @ %g stop=%g lev=%.1fx risk=$%.0f conf=%.2f altfins=%d",
                    side.value.upper(), asset, qty, entry_price, stop_price, lev,
                    risk_dollars, confidence,
                    len(altfins_sigs),
                )
                break

            if self._suggested_params:
                self.store.put_state("pending_param_changes", self._suggested_params)
                logger.info("PENDING PARAM CHANGES: %d suggestions", len(self._suggested_params))

    async def _process_external_intents(self, exchange: PaperPerpExchange, hl: ExchangeAdapter):
        for row in self.store.pending_intents(limit=25):
            try:
                intent = TradeIntent.from_row(row)
                ok, reason = await self._execute_intent(intent, exchange, hl)
                self.store.update_intent_status(intent.id, "accepted" if ok else "rejected", reason)
                if not ok:
                    self.store.record_delegation_metric(intent.source if intent.source else "freqtrade", False)
            except Exception as e:
                self.store.update_intent_status(int(row["id"]), "rejected", f"invalid_intent: {e}")

    async def _execute_intent(self, intent: TradeIntent, exchange: PaperPerpExchange, hl: ExchangeAdapter) -> tuple[bool, str]:
        now = datetime.now(timezone.utc)
        if now >= intent.expires_at:
            return False, "expired"
        if intent.asset not in self.assets:
            return False, "asset_not_allowed"
        if intent.confidence < MIN_ENTRY_CONFIDENCE:
            return False, f"confidence_below_gate: {intent.confidence:.2f}"

        existing = await exchange.fetch_position(intent.asset)
        if existing:
            return False, "position_already_open"

        price = await exchange.fetch_price(intent.asset)
        entry_price = price if price > 0 else intent.intended_entry_price
        if entry_price <= 0:
            return False, "no_price"

        risk_ok, risk_msg = self.risk.allow_entry(exchange.gross_exposure, exchange.effective_leverage)
        if not risk_ok:
            logger.info("ENTRY_DIAG %s: skip -- risk: %s", asset, risk_msg)
            return False, risk_msg
        oi_ok, oi_msg = self.risk.oi_gate_allows(intent.asset)
        if not oi_ok:
            self._last_entry_diag_cycle = self._cycle_count
            logger.info("ENTRY_DIAG %s: skip -- OI: %s", asset, oi_msg)
            return False, oi_msg
        funding_rate = await hl.get_funding_rate(intent.asset)
        funding_ok, funding_msg = self.risk.funding_gate(funding_rate)
        if not funding_ok:
            self._last_entry_diag_cycle = self._cycle_count
            logger.info("ENTRY_DIAG %s: skip -- funding: %s", asset, funding_msg)
            return False, funding_msg

        stop_price = intent.requested_stop_price
        if stop_price is None or stop_price <= 0:
            candles = self.candle_cache.get(intent.asset, [])
            stop_pct, _ = self.risk.compute_stop_distance(intent.asset, candles)
            stop_price = entry_price * (1 - stop_pct / 100) if intent.side == Side.LONG else entry_price * (1 + stop_pct / 100)

        stop_pct = abs(entry_price - stop_price) / entry_price * 100
        if stop_pct < self.risk.stop_min_pct or stop_pct > self.risk.stop_max_pct:
            return False, f"stop_distance_out_of_bounds: {stop_pct:.2f}%"
        if intent.side == Side.LONG and stop_price >= entry_price:
            return False, "invalid_long_stop"
        if intent.side == Side.SHORT and stop_price <= entry_price:
            return False, "invalid_short_stop"

        candles = self.candle_cache.get(intent.asset, [])
        safe_lev, lev_reason = self.risk.compute_leverage(intent.asset, candles, intent.side)
        leverage = max(1.0, min(intent.requested_leverage, safe_lev, self.risk.max_portfolio_leverage))
        qty, risk_dollars, _ = self.risk.position_size(
            intent.asset, exchange.equity, stop_pct, entry_price, exchange.gross_exposure
        )
        if qty <= 0:
            return False, "no_remaining_exposure_capacity"
        projected_exposure = exchange.gross_exposure + (qty * entry_price)
        projected_lev = projected_exposure / exchange.equity if exchange.equity > 0 else 999
        if projected_lev > self.risk.max_portfolio_leverage:
            return False, f"projected_leverage: {projected_lev:.2f}x"

        order_id = await exchange.place_order(Order(
            asset=intent.asset,
            side=intent.side,
            order_type=OrderType.MARKET,
            quantity=qty,
            stop_price=stop_price,
            reduce_only=False,
            leverage=leverage,
            metadata={"component_sources": intent.components, "intent_key": intent.idempotency_key},
        ))
        if not order_id:
            return False, "order_rejected"

        pos = exchange.positions.get(intent.asset)
        if pos:
            pos.strategy = intent.strategy or "freqtrade_intent"
            pos.signal_source = f"intent:{intent.source}:{intent.asset}"
            pos.entry_confidence = intent.confidence
            pos.stop_loss = stop_price
            pos.component_sources = list(intent.components)

        # Delegation Gap tracking (NotebookLM)
        impl_shortfall = abs(entry_price - intent.intended_entry_price) / intent.intended_entry_price * 100 if intent.intended_entry_price > 0 else 0
        self.store.record_delegation_metric(intent.source if intent.source else "freqtrade", True, impl_shortfall)
        self.risk.record_position_open(intent.asset)
        logger.info(
            "INTENT ACCEPTED %s %s qty=%.4f @ %.2f stop=%.2f lev=%.1fx risk=$%.0f conf=%.2f %s",
            intent.side.value.upper(), intent.asset, qty, entry_price, stop_price, leverage,
            risk_dollars, intent.confidence, lev_reason,
        )
        return True, "accepted"

    @staticmethod
    def _aggregate_to_4h(candles_1h: list[PerpCandle]) -> list[PerpCandle]:
        """Aggregate 1h candles into 4h candles (4 × 1h = 1 × 4h)."""
        if not candles_1h or len(candles_1h) < 4:
            return []
        out: list[PerpCandle] = []
        for i in range(0, len(candles_1h) - 3, 4):
            group = candles_1h[i : i + 4]
            o = group[0].open
            h = max(c.high for c in group)
            l = min(c.low for c in group)
            c = group[-1].close
            v = sum(candle.volume for candle in group)
            ts = group[0].timestamp
            out.append(PerpCandle(open=o, high=h, low=l, close=c, volume=v, timestamp=ts))
        return out

    def _infer_regime(self, asset: str, candles: list[PerpCandle], max_lookback: int = 50) -> RegimeType:
        if len(candles) < max_lookback:
            max_lookback = len(candles)
        if max_lookback < 30:
            return RegimeType.RANDOM_WALK
        candles = candles[-max_lookback:]
        closes = [c.close for c in candles]
        last = closes[-1]

        # Normalized volatility (ATR_14 / price)
        trs = []
        for i in range(-14, 0):
            h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs) / len(trs) if trs else 0
        norm_vol = atr / last if last > 0 else 0

        if norm_vol < 0.0015:
            return RegimeType.DEAD_MARKET
        if norm_vol > 0.03:
            return RegimeType.HIGH_VOL

        # CUSUM regime detection (replaces ADX as primary — less lag)
        log_rets = []
        for i in range(1, len(closes)):
            if closes[i-1] > 0:
                log_rets.append(math.log(closes[i] / closes[i-1]))

        if len(log_rets) < 10:
            return RegimeType.RANDOM_WALK

        key = f"{asset}_cr_{max_lookback}"
        s = self._cusum.get(key, {"S_high": 0, "S_low": 0, "mean": None, "std": None})

        if s["mean"] is None:
            recent = log_rets[-min(30, len(log_rets)):]
            s["mean"] = sum(recent) / len(recent) if recent else 0
            s["std"] = max(statistics.stdev(recent) if len(recent) >= 3 else 0.01, 0.005)

        decay = 0.99
        for r in log_rets[-1:]:
            s["mean"] = decay * s["mean"] + (1 - decay) * r
            s["var"] = decay * s.get("var", s["std"]**2) + (1 - decay) * (r - s["mean"])**2
            s["std"] = max(math.sqrt(s["var"]), 0.005)

        k_val = 0.2
        h_val = 2.5
        Z = (log_rets[-1] - s["mean"]) / s["std"] if s["std"] > 0 else 0

        s["S_high"] = max(0, s["S_high"] + Z - k_val)
        s["S_low"] = max(0, s["S_low"] - Z - k_val)

        self._cusum[key] = s

        if s["S_high"] > h_val * 2:
            s["S_high"] = 0; s["S_low"] = 0
            return RegimeType.STRONGLY_TRENDING
        if s["S_low"] > h_val * 2:
            s["S_high"] = 0; s["S_low"] = 0
            return RegimeType.STRONGLY_TRENDING
        if s["S_high"] > h_val:
            return RegimeType.TRENDING
        if s["S_low"] > h_val:
            return RegimeType.TRENDING

        return RegimeType.RANDOM_WALK

    @staticmethod
    def _efficiency_ratio(closes: list[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 0.5
        direction = abs(closes[-1] - closes[-period - 1])
        volatility = sum(abs(closes[i] - closes[i - 1]) for i in range(-period, 0))
        if volatility == 0:
            return 0.5
        return direction / volatility

    @staticmethod
    def _hurst(prices: list[float]) -> float:
        n = len(prices)
        if n < 30:
            return 0.5
        max_lag = min(n // 2, 100)
        log_lags = []
        log_tau = []
        for lag in range(2, max_lag):
            diffs = [prices[i] - prices[i - lag] for i in range(lag, n)]
            if not diffs:
                continue
            var = sum(d * d for d in diffs) / len(diffs)
            if var <= 0:
                continue
            std = math.sqrt(var)
            log_lags.append(math.log(lag))
            log_tau.append(math.log(std))
        if len(log_lags) < 3:
            return 0.5
        n_pts = len(log_lags)
        sum_x = sum(log_lags)
        sum_y = sum(log_tau)
        sum_xy = sum(x * y for x, y in zip(log_lags, log_tau))
        sum_xx = sum(x * x for x in log_lags)
        denom = n_pts * sum_xx - sum_x * sum_x
        if denom == 0:
            return 0.5
        slope = (n_pts * sum_xy - sum_x * sum_y) / denom
        return slope / 2

    @staticmethod
    def _adx(candles: list[PerpCandle], period: int = 14) -> float:
        if len(candles) < period * 2 + 5:
            return 0.0
        tr_vals, plus_dm, minus_dm = [], [], []
        for i in range(-period * 2 + 1, 0):
            h, l, pc, ph, pl = candles[i].high, candles[i].low, candles[i-1].close, candles[i-1].high, candles[i-1].low
            tr_vals.append(max(h - l, abs(h - pc), abs(l - pc)))
            up_move = h - ph
            down_move = pl - l
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        atr = sum(tr_vals[-period:]) / period
        if atr <= 0:
            return 0.0
        pdi = sum(plus_dm[-period:]) / period / atr * 100
        ndi = sum(minus_dm[-period:]) / period / atr * 100
        dx = abs(pdi - ndi) / (pdi + ndi) * 100 if (pdi + ndi) > 0 else 0.0
        return dx


    @staticmethod
    def _ema(closes: list[float], period: int) -> float:
        if len(closes) < period + 1:
            return closes[-1] if closes else 0.0
        k = 2.0 / (period + 1)
        ema_val = sum(closes[:period]) / period
        for price in closes[period:]:
            ema_val = price * k + ema_val * (1 - k)
        return ema_val
    async def _close(
        self,
        asset: str,
        pos: PerpPosition,
        price: float,
        reason: str,
        exchange: PaperPerpExchange,
        close_pct: float = 1.0,
    ):
        close_qty = pos.size * close_pct
        await exchange.place_order(Order(
            asset=asset,
            side=pos.side.opposite,
            order_type=OrderType.MARKET,
            quantity=close_qty,
            reduce_only=True,
        ))

        pnl_dollars = pos.unrealized_pnl * close_pct
        r_mult = ((price - pos.entry_price) / pos.entry_price * pos.leverage) if pos.entry_price > 0 else 0.0

        if close_pct < 1.0:
            # Partial close (scale-out): close_pct of position, leave the rest
            self.risk.record_trade(asset, pos.pnl_pct, pnl_dollars, reason)
            trade = {
                "asset": asset,
                "side": pos.side.value,
                "entry_price": pos.entry_price,
                "exit_price": price,
                "size": close_qty,
                "leverage": pos.leverage,
                "pnl_pct": round(pos.pnl_pct, 2),
                "pnl_dollars": round(pnl_dollars, 2),
                "fees": 0.0,
                "funding_paid": 0.0,
                "exit_reason": reason,
                "strategy": pos.strategy or "",
                "signal_source": pos.signal_source or "",
                "entry_confidence": pos.entry_confidence or 0.0,
                "entry_time": pos.entry_time.isoformat(),
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "r_multiple": round(r_mult, 3),
                "mae_pct": round(getattr(pos, 'mae_pct', 0.0), 2),
        "mfe_pct": round(getattr(pos, 'mfe_pct', 0.0), 2),
                "mfe_pct": round(getattr(pos, 'mfe_pct', 0.0), 2),
                "regime": getattr(pos, "regime", "") or "",
                "entry_regime": getattr(pos, "entry_regime", "") or getattr(pos, "regime", "") or "",
            }
            self.store.save_trade(trade)
            # Reduce position size; move stop to breakeven
            pos.size -= close_qty
            pos.stop_loss = pos.entry_price
            asyncio.ensure_future(self.notifier.position_closed(
                asset, pos.side.value.upper(), pos.entry_price, price, pnl_dollars, reason, pos.strategy or "",
            ))
            return

        # Full close (original logic)
        pnl_pct = pos.pnl_pct
        pnl_dollars = pos.unrealized_pnl

        self.risk.record_trade(asset, pnl_pct, pnl_dollars, reason)
        self.risk.record_position_close(asset)
        self.signal_tracker.record(pos.signal_source, pnl_pct > 0)
        for source in pos.component_sources:
            self.signal_tracker.record(source, pnl_pct > 0)

        if pos.strategy:
            for strat in self.strategies:
                if strat.name() == pos.strategy and hasattr(strat, "on_exit"):
                    strat.on_exit(asset)
                    break

        trade = {
            "asset": asset,
            "side": pos.side.value,
            "entry_price": pos.entry_price,
            "exit_price": price,
            "size": pos.size,
            "leverage": pos.leverage,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_dollars": round(pnl_dollars, 2),
            "fees": 0.0,
            "funding_paid": 0.0,
            "exit_reason": reason,
            "strategy": pos.strategy or "",
            "signal_source": pos.signal_source or "",
            "entry_confidence": pos.entry_confidence or 0.0,
            "entry_time": pos.entry_time.isoformat(),
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "r_multiple": round(r_mult, 3),
            "mae_pct": round(getattr(pos, 'mae_pct', 0.0), 2),
            "regime": getattr(pos, "regime", "") or "",
            "entry_regime": getattr(pos, "entry_regime", "") or getattr(pos, "regime", "") or "",
        }
        self.store.save_trade(trade)

        asyncio.ensure_future(self.notifier.position_closed(
            asset, pos.side.value.upper(), pos.entry_price, price, pnl_dollars, reason, pos.strategy or "",
        ))

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
            self.store.insert_param_change(
                s["parameter"], str(s["current_value"]), str(s["suggested_value"]),
                "pending" if s["confidence"] < 0.6 else "suggested"
            )
        if reflection["needs_human_review"]:
            logger.info("  ⚠ Human review recommended for low-confidence suggestions")
        logger.info("===========================")
