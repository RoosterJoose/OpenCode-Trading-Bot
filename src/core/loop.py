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
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from src.adapters.altfins import AltfinsAdapter
from src.adapters.base import ExchangeAdapter
from src.adapters.coinbase_advanced import CoinbaseAdvancedAdapter
from src.adapters.kalshi import KalshiAdapter
from src.core.execution_engine import ExecutionEngine
from src.core.risk_governor import RiskGovernor
from src.core.walk_forward import WalkForwardEngine
from src.core.reconciliation import ReconciliationService
from src.core.experiment_registry import ExperimentRegistry
from src.core.event_kill import EventKillSwitch
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
from src.strategies.xs_momentum import CrossSectionalMomentum
from src.strategies.trend_4h import Trend4h
from src.strategies.fade_5m import Fade5m
from src.core.regime import RegimeDetector

logger = logging.getLogger("hermes.loop")

def _compute_buy_hold_benchmark() -> float:
    try:
        import sqlite3
        db = sqlite3.connect("/opt/hermes-trading-bot/data/hermes.db")
        row = db.execute("SELECT equity FROM equity_snapshots ORDER BY id ASC LIMIT 1").fetchone()
        start_equity = row[0] if row else 5000.0
        db.close()
        return start_equity
    except:
        return 5000.0


ASSET_UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "AAVE", "LTC", "NEAR", "SUI", "XLM", "HBAR", "BCH", "ZEC", "PEPE", "SHIB", "HYPE", "ONDO", "ENA"]
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
            CrossSectionalMomentum(signal_tracker=self.signal_tracker),
            Trend4h(signal_tracker=self.signal_tracker),
            Fade5m(signal_tracker=self.signal_tracker),
        ]

        self.assets = list(
            config.get("strategies", {})
            .get("mean_reversion", {})
            .get("assets", ASSET_UNIVERSE)
        )
        self.candle_cache: dict[str, list[PerpCandle]] = {}
        self.candle_4h_cache: dict[str, list[PerpCandle]] = {}
        self.candle_5m_cache: dict[str, list[PerpCandle]] = {}
        self._5m_refresh_cycle: int = 0
        self._restore_candle_cache()  # 4h aggregated for regime detection
        self._asset_coverage_warned: bool = False
        self.signal_cache: dict[str, list[Signal]] = defaultdict(list)
        self._stop_event = asyncio.Event()
        self._suggested_params: list[dict] = []
        self._last_auto_apply_ts: float = 0.0
        self._daily_signals_log: list[dict] = []
        self.governor = RiskGovernor(
            self.store,
            initial_capital=eq,
            max_drawdown_pct=30.0,
            max_daily_loss_pct=4.0,
        )
        self.experiments = ExperimentRegistry(data_dir / "experiments.db")
        self.reconciliation: Optional[ReconciliationService] = None
        self._altfins_cycle = 0
        self._cusum: dict[str, dict] = {}  # per-asset CUSUM state: {"S_high": 0, "S_low": 0, "mean": 0, "std": 1, "n": 0}
        self._altfins = None
        self._kalshi = None
        self._kalshi_funding = {}
        self._strategy_budget = {}
        self._ic_budget_cycle = 0
        self._event_kill = EventKillSwitch()
        self._block_reasons: dict[str, int] = {}
        self._loss_cooldowns: dict[str, float] = {}  # f"{asset}_{side}" -> expiry_ts
        if not hasattr(self, 'telegram') or not self.telegram:
            token = self.config.get("telegram", {}).get("bot_token") or os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
            chat_id = self.config.get("telegram", {}).get("chat_id") or os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
            self.telegram = TelegramBot(token, chat_id, self.store)
            self.regime = RegimeDetector()
            self.notifier = self.telegram
        self._sent_alerts: dict[str, float] = {}  # rate-limit alerts (key -> timestamp)

    def _restore_paper_positions(self, exchange: ExecutionEngine):
        positions = self.store.get_state("positions") or []
        exchange.restore_positions(positions)
        for pos in exchange.positions.values():
            self.risk.record_position_open(pos.asset)
        # Reconcile _active_positions with exchange state
        self.risk._active_positions = set(exchange.positions.keys())
        if exchange.positions:
            logger.info("Restored %d paper position(s)", len(exchange.positions))

    async def start(self):
        self.running = True
        saved_eq = self.store.get_state("paper_equity")
        initial = float(saved_eq) if saved_eq else self.config.get("exchange", {}).get("initial_balance", 10_000.0)
        exchange = ExecutionEngine(
            initial_balance=initial,
            spread_bps=3,
            taker_fee=0.0006,
            seed=42,
        )
        self._restore_paper_positions(exchange)
        self._restore_risk_state()
        # Fix 1: Auto-apply pending param changes from weekly reflection
        try:
            pending_raw = self.store.get_state("pending_param_changes")
            if pending_raw:
                pending = json.loads(pending_raw) if isinstance(pending_raw, str) else pending_raw
                if isinstance(pending, list):
                    for change in pending:
                        strat_name = change.get("strategy", "")
                        param = change.get("parameter", change.get("param", ""))
                        value = change.get("suggested_value", change.get("value", 0))
                        if not strat_name or not param:
                            continue
                        found = False
                        for s in self.strategies:
                            if s.name() == strat_name and hasattr(s, "set_param"):
                                s.set_param(param, value)
                                found = True
                        if found:
                            logger.info("AUTO-APPLIED: %s.%s = %s", strat_name, param, value)
                            self._last_auto_apply_ts = time.time()
                self.store.put_state("pending_param_changes", "")  # clear after apply
        except Exception as e:
            logger.debug("auto-apply pending params: %s", e)

        # RiskGovernor: check latched kill state
        if self.governor.is_killed():
            kill_info = self.governor.get_kill_info()
            logger.warning("RISK_GOVERNOR: latched kill -- reason=%s, ts=%s",
                           kill_info.get("reason"), kill_info.get("timestamp"))

        # Experiment registry: write run manifest
        try:
            import subprocess
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.data_dir.parent,
                text=True, stderr=subprocess.DEVNULL
            ).strip() if (self.data_dir.parent / ".git").exists() else "unknown"
        except Exception:
            commit = "unknown"
        self.experiments.create_run_manifest(
            deployment_id=os.environ.get("HERMES_DEPLOYMENT", "DEP-CONS"),
            config={
                "initial_equity": initial,
                "mode": "paper",
                "strategies": [s.name() for s in self.strategies],
                "assets": self.assets,
            },
        )

        # Reconciliation service (no adapter = paper mode, skips exchange checks)
        self.reconciliation = ReconciliationService(
            local_store=self.store,
            risk_governor=self.governor,
            tolerance_usd=1.0,
        )

        # Reconciliation startup barrier (blocks entries until checks pass)
        barrier = await self.reconciliation.startup_barrier()
        if not barrier.passed and barrier.safe_halt_required:
            logger.warning("RECONCILIATION: startup barrier FAILED -- %s", barrier.errors)
            self.governor.trigger_kill(KillSwitchReason.RECONCILIATION_FAILURE)
        asyncio.ensure_future(self.reconciliation.continuous_reconciliation())

        # RiskGovernor auto-trigger: check daily drawdown from trades
        try:
            import json
            today_trades = self.store.trades(limit=50)
            if today_trades:
                today_entries = [t for t in today_trades if t.get("exit_time","").startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))]
                if today_entries:
                    total_pnl = sum(float(t.get("pnl_dollars",0)) for t in today_entries if t.get("pnl_dollars"))
                    initial_eq = float(self.store.get_state("paper_equity") or self.config.get("exchange",{}).get("initial_balance",10000))
                    if initial_eq > 0:
                        daily_dd = total_pnl / initial_eq
                        if daily_dd < -0.04:
                            from src.core.risk_governor import KillSwitchReason
                            self.governor.trigger_kill(KillSwitchReason.DAILY_LOSS_LIMIT)
                            logger.warning("RiskGovernor auto-triggered: daily drawdown %.2f%% exceeds 4%% threshold", daily_dd * 100)
        except Exception as e:
            logger.debug("RiskGovernor auto-trigger check: %s", e)

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

        altfins_keys = []
        key1 = self.config.get("altfins", {}).get("api_key", "") or os.environ.get("ALTFINS_API_KEY", "")
        if key1:
            altfins_keys.append(key1)
        key2 = self.config.get("altfins", {}).get("api_key_2", "") or os.environ.get("ALTFINS_API_KEY_2", "")
        if key2:
            altfins_keys.append(key2)
        if altfins_keys:
            self._altfins = AltfinsAdapter(altfins_keys)
            logger.info("Altfins: enabled with %d key(s)", len(altfins_keys))
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
        try:
            self._event_kill.refresh()
        except Exception as e:
            logger.warning("EventKill: init failed (%s), continuing without it", e)


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
            # Do NOT restore _recent_outcomes — circuit breakers rebuild naturally from live trades.
            # Stale WR halts persisted in DB have blocked the bot for hours multiple times.
            self.risk._recent_outcomes = []
            self.store.put_state("risk_recent_outcomes", "[]")
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

    def _restore_loss_cooldowns(self) -> None:
        try:
            raw = self.store.get_state("loss_cooldowns")
            if raw:
                saved = json.loads(raw) if isinstance(raw, str) else raw
                now = time.time()
                self._loss_cooldowns = {k: v for k, v in saved.items() if v > now}
                expired = len(saved) - len(self._loss_cooldowns)
                if expired:
                    logger.info("Loss cooldowns restored: %d active, %d expired", len(self._loss_cooldowns), expired)
                elif self._loss_cooldowns:
                    logger.info("Loss cooldowns restored: %d active", len(self._loss_cooldowns))
        except Exception as e:
            logger.debug("restore loss cooldowns: %s", e)
            self._loss_cooldowns = {}

    def _save_loss_cooldowns(self) -> None:
        try:
            now = time.time()
            active = {k: v for k, v in self._loss_cooldowns.items() if v > now}
            self.store.put_state("loss_cooldowns", json.dumps(active))
        except Exception as e:
            logger.debug("save loss cooldowns: %s", e)

    def _send_alert_ratelimited(self, key: str, message: str, min_interval: float = 3600.0) -> None:
        now = time.time()
        last = self._sent_alerts.get(key, 0.0)
        if now - last >= min_interval:
            self._sent_alerts[key] = now
            asyncio.ensure_future(self.notifier.send(message))

    async def _cycle(self, hl: ExchangeAdapter, exchange: ExecutionEngine):
        # Gap 1: Mid-cycle suggestion apply — read pending changes every cycle
        try:
            raw = self.store.get_state("pending_param_changes")
            if raw and isinstance(raw, str) and raw.startswith("["):
                pending = __import__('json').loads(raw)
                if isinstance(pending, list) and pending:
                    for change in pending:
                        strat_name = change.get("strategy", "")
                        param = change.get("parameter", change.get("param", ""))
                        value = change.get("suggested_value", change.get("value", 0))
                        if not strat_name or not param:
                            continue
                        for s in self.strategies:
                            if s.name() == strat_name and hasattr(s, "set_param"):
                                s.set_param(param, value)
                    self.store.put_state("pending_param_changes", "")
        except Exception as e:
            __import__('logging').getLogger(__name__).debug("mid-cycle apply: %s", e)

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
        # Load per-strategy pause flags from sharpe_tracker
        try:
            raw_ps = self.store.get_state("paused_strategies")
            self._paused_strategies = json.loads(raw_ps) if raw_ps else []
        except Exception as e:
            self._paused_strategies = []
            logger.debug("paused strategies load failed: %s", e)
        # Load dynamic thresholds from closed_loop.py and inject into strategies
        try:
            raw = self.store.get_state("dynamic_thresholds")
            if raw and (time.time() - getattr(self, '_last_auto_apply_ts', 0.0)) >= 3600:
                thresholds = json.loads(raw) if isinstance(raw, str) else raw
                for strat in self.strategies:
                    if hasattr(strat, "set_dynamic_thresholds"):
                        strat.set_dynamic_thresholds(thresholds)
        except Exception as e:
            logger.debug("dynamic thresholds: %s", e)
        # Load strategy budget from strategy_budget.py
        self._strategy_budget = {}
        self._strategy_budget = {}      # restored from DB below
        token = self.config.get("telegram", {}).get("bot_token") or os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
        chat_id = self.config.get("telegram", {}).get("chat_id") or os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
        self.telegram = TelegramBot(token, chat_id, self.store)
        self.regime = RegimeDetector()
        self.notifier = self.telegram
        try:
            raw = self.store.get_state("strategy_budget")
            if raw:
                sb = json.loads(raw) if isinstance(raw, str) else raw
                self._strategy_budget = sb.get("weights", {})
        except Exception as e:
            logger.debug("strategy budget: %s", e)
        # Safety clamp: normalize budget fractions if sum deviates from 1.0
        if self._strategy_budget:
            total_budget = sum(self._strategy_budget.get(s, 0) for s in ["xs_momentum","trend_4h","fade_5m"])
            if abs(total_budget - 1.0) > 0.05 and total_budget > 0:
                for k in self._strategy_budget:
                    self._strategy_budget[k] = self._strategy_budget[k] / total_budget
        self._restore_strategy_cooldowns()
        self._restore_loss_cooldowns()
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

        # 5m candle fetch for Fade5m strategy (every 5 cycles)
        if self._cycle_count % 5 == 0:
            async def _fetch_5m(asset: str) -> None:
                async with _candle_sema:
                    try:
                        candles_5m = await asyncio.wait_for(hl.fetch_candles(asset, "5m", 100), timeout=15.0)
                        if candles_5m:
                            self.candle_5m_cache[asset] = candles_5m
                            try:
                                self.store.save_candles(asset, candles_5m)
                            except Exception as e:
                                logger.debug("save 5m candles %s: %s", asset, e)
                        elif asset not in self.candle_5m_cache:
                            logger.warning("5m fetch %s: empty", asset)
                    except asyncio.TimeoutError:
                        logger.debug("5m fetch %s: timeout", asset)
                    except Exception as e:
                        logger.debug("5m fetch %s: %s", asset, e)
            await asyncio.gather(*[_fetch_5m(a) for a in self.assets])

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
            if self._altfins_cycle % 192 == 0:
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
                    if self._cycle_count % 5 == 0:
                        try:
                            self.store.save_altfins_signal(
                                s.asset, s.source, s.direction.value, s.confidence, s.bucket or "",
                                s.timestamp.isoformat() if s.timestamp else datetime.now(timezone.utc).isoformat(),
                            )
                        except Exception:
                            pass
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
                if self._altfins_cycle % 360 == 0: permit_info = await self._altfins.check_permit_usage()
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
        except Exception as e:
            logger.debug("xs returns: %s", e)

        # 5a. BTC falling-knife guard + bear-market flag (once per cycle)
        btc_candles = self.candle_cache.get("BTC", [])
        self._btc_knife_block = False
        self._btc_bear_market = False
        if len(btc_candles) >= 60:
            btc_adx = TradingLoop._adx(btc_candles)
            btc_closes = [c.close for c in btc_candles]
            btc_ema50 = TradingLoop._ema(btc_closes, 50)
            btc_price = btc_closes[-1]
            self._btc_bear_market = btc_price < btc_ema50
            if btc_adx > 28 and btc_price < btc_ema50:
                self._btc_knife_block = True
                logger.info("BTC knife guard: ADX=%.1f price=%.0f < EMA50=%.0f",
                             btc_adx, btc_price, btc_ema50)
            elif self._btc_bear_market:
                logger.info("BTC bear market: price=%.0f < EMA50=%.0f (longs blocked)",
                             btc_price, btc_ema50)

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
        self.governor.update_equity(eq)
        self.risk.update_equity(eq, ge)
        self.risk.set_gross_exposure(ge)
        self.store.save_equity_snapshot(eq, self.risk.peak_equity)
        buy_hold = _compute_buy_hold_benchmark()
        self.store.put_state("buy_hold_equity", str(round(buy_hold, 2)))

        # Walk-forward validation (daily)
        if self._cycle_count % 1440 == 0 and self._cycle_count > 0:
            try:
                wf_trades = self.store.trades(limit=100)
                if len(wf_trades) >= 40:
                    from src.core.walk_forward import WalkForwardEngine
                    splitter = WalkForwardEngine(n_trials=min(5, len(wf_trades) // 20))
                    folds = list(splitter.split(wf_trades))
                    if folds:
                        sharpes = []
                        import statistics
                        for _, test_idx in folds[:3]:
                            test_r = [t.get("r_multiple", 0) for i, t in enumerate(wf_trades) if i in test_idx and t.get("r_multiple", 0) != 0]
                            if len(test_r) >= 5:
                                mu = sum(test_r) / len(test_r)
                                sigma = statistics.stdev(test_r) if len(test_r) > 1 else 1.0
                                sharpes.append(mu / sigma if sigma > 0 else 0)
                        if sharpes:
                            wf_sharpe = sum(sharpes) / len(sharpes)
                            self.store.put_state("wf_forward_sharpe", str(round(wf_sharpe, 4)))
            except Exception as e:
                logger.debug("walk-forward: %s", e)
        self.store.put_state("paper_equity", str(exchange.balance))
        self.store.put_state("paper_peak_equity", str(self.risk.peak_equity))
        await self.notifier.daily_drawdown(eq, self.risk.peak_equity,
            (self.risk.peak_equity - eq) / self.risk.peak_equity * 100 if self.risk.peak_equity > 0 else 0)

        # IC budget refresh every 30 cycles
        self._ic_budget_cycle += 1
        if self._ic_budget_cycle % 30 == 0:
            try:
                from src.core.ic_allocator import compute_weights
                weights = compute_weights(db_path=str(self.data_dir / 'hermes.db'), strategies=self.strategies)
                budget = {"weights": weights, "source": "ic_rollingsharpe", "timestamp": time.time()}
                self.store.put_state("strategy_budget", json.dumps(budget))
                self._strategy_budget = weights
                # Safety clamp: normalize budget fractions if sum deviates from 1.0
                total_budget = sum(self._strategy_budget.get(s, 0) for s in ["xs_momentum","trend_4h","fade_5m"])
                if abs(total_budget - 1.0) > 0.05 and total_budget > 0:
                    for k in self._strategy_budget:
                        self._strategy_budget[k] = self._strategy_budget[k] / total_budget
                logger.info("IC_BUDGET: %s", {k: round(v,3) for k, v in weights.items()})
            except Exception as e:
                logger.error("IC budget failed: %s", e)
        # Persist risk state + strategy cooldowns (survive restarts)
        # Self-heal: detect stale blocking state — if WR halt active >60 cycles, auto-clear
        ro_key = self.store.get_state("risk_recent_outcomes")
        if ro_key:
            try:
                outcomes = json.loads(ro_key)
                if len(outcomes) >= 10:
                    wr = sum(outcomes) / len(outcomes)
                    if wr < 0.30 and self._cycle_count > 60:
                        stall_cycles = self._cycle_count - getattr(self, "_wr_halt_first_seen", 0)
                        if not getattr(self, "_wr_halt_first_seen", None):
                            self._wr_halt_first_seen = self._cycle_count
                        if stall_cycles > 60:
                            logger.warning("SELF-HEAL: WR halt %.0f%% stale for %d cycles — clearing", wr*100, stall_cycles)
                            self.risk._recent_outcomes = []
                            self.store.put_state("risk_recent_outcomes", "[]")
                            self._wr_halt_first_seen = 0
            except Exception:
                pass

        # Self-heal: loss streak auto-reset — if stale and blocked, clear
        gs = getattr(self.risk, '_global_loss_streak', 0)
        if gs >= 5 and self._cycle_count - getattr(self, '_last_entry_diag_cycle', 0) > 120:
            logger.warning("SELF-HEAL: GS=%d stale for 120+ cycles — clearing", gs)
            self.risk._global_loss_streak = 0
            self.risk.global_loss_streak = 0
            self.store.put_state("risk_global_loss_streak", "0")
        self._save_risk_state()
        self._save_strategy_cooldowns()
        self._save_loss_cooldowns()
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
        # Position quality sweep: close stale flat/negative positions to free slots
        now_ts = time.time()
        # Periodic WAL checkpoint every 5 cycles to prevent WAL bloat
        if self._cycle_count % 5 == 0:
            try:
                self.store._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass

    def _compute_sma200(self, asset: str) -> tuple[float, float, str]:
        import httpx
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        cache_key = f"sma200_{asset}"
        cached = getattr(self, "_sma200_cache", {})
        if cache_key in cached:
            cached_date, cached_result = cached[cache_key]
            if cached_date == today:
                return cached_result
        pid = f"{asset}-USD"
        try:
            resp = httpx.get(
                f"https://api.exchange.coinbase.com/products/{pid}/candles",
                params={"granularity": 86400, "limit": 210},
                timeout=10
            )
            if resp.status_code != 200:
                return 0.0, 0.0, "flat"
            candles = sorted(resp.json(), key=lambda x: x[0])
            import calendar
            utc_now = datetime.now(timezone.utc)
            last_start = candles[-1][0]
            midnight = int(calendar.timegm(utc_now.date().timetuple()))
            if last_start >= midnight and len(candles) >= 201:
                closes = [c[4] for c in candles[-201:-1]]
            else:
                closes = [c[4] for c in candles[-200:]]
            if len(closes) < 200:
                return 0.0, 0.0, "flat"
            sma = sum(closes) / 200
            last_close = closes[-1]
            side = "long" if last_close > sma else "short"
            result = (sma, last_close, side)
            if not hasattr(self, "_sma200_cache"):
                self._sma200_cache = {}
            self._sma200_cache[cache_key] = (today, result)
            return result
        except Exception:
            return 0.0, 0.0, "flat"

    def _get_daily_candles_for_regime(self, asset: str) -> list:
        today = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime("%Y-%m-%d")
        cache = getattr(self, "_regime_daily_cache", {})
        if cache.get(asset, {}).get("date") == today:
            return cache[asset]["candles"]
        import httpx
        try:
            resp = httpx.get(
                f"https://api.exchange.coinbase.com/products/{asset}-USD/candles",
                params={"granularity": 86400, "limit": 210},
                timeout=10
            )
            if resp.status_code != 200:
                return []
            raw = sorted(resp.json(), key=lambda x: x[0])
            from src.core.types import Candle
            candles = []
            for c in raw:
                candle = Candle(
                    timestamp=int(c[0]),
                    open=float(c[3]),
                    high=float(c[2]),
                    low=float(c[1]),
                    close=float(c[4]),
                    volume=float(c[5]),
                )
                candles.append(candle)
            if not hasattr(self, "_regime_daily_cache"):
                self._regime_daily_cache = {}
            self._regime_daily_cache[asset] = {"date": today, "candles": candles}
            return candles
        except Exception:
            return []

    def _compute_atr(self, asset: str, period: int = 14) -> float:
        """Compute 14-period ATR — 4h preferred, fallback 1h."""
        candles = self.candle_4h_cache.get(asset, [])
        if len(candles) < period + 1:
            candles = self.candle_cache.get(asset, [])
        if len(candles) < period + 1:
            return 0.0
        sorted_c = sorted(candles, key=lambda x: x.timestamp)
        tr_sum = 0.0
        for i in range(-period, 0):
            h = sorted_c[i].high
            l = sorted_c[i].low
            pc = sorted_c[i - 1].close
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_sum += tr
        return tr_sum / period

    _smakill_cache: float = 0.0
    _smakill_cache_cycle: int = -999

    def _sma200_net_pnl_28d(self) -> float:
        """Net PnL of SMA200 perp trades in last 28 days (cached 60 cycles)."""
        if hasattr(self, '_cycle_count') and self._cycle_count - getattr(self, '_smakill_cache_cycle', -999) < 60:
            return self._smakill_cache
        import sqlite3
        db_path = str(self.data_dir / "hermes.db")
        try:
            db = sqlite3.connect(db_path)
            row = db.execute(
                "SELECT COALESCE(SUM(pnl_dollars), 0) FROM trades WHERE strategy='sma200_perp' AND exit_time >= datetime('now', '-28 days')"
            ).fetchone()
            db.close()
            val = row[0] if row else 0.0
            self._smakill_cache = val
            self._smakill_cache_cycle = self._cycle_count
            return val
        except Exception:
            return 0.0

    _kelly_reset_version = 3  # class-level: increment to reset Kelly after major strategy changes

    def _kelly_factor_for_strategy(self, strategy: str) -> float:
        trades = self.store.trades(limit=200)
        strat_trades = [t for t in trades if t.get("strategy") == strategy and t.get("r_multiple", 0) != 0]
        # Kelly reset: only count trades from current strategy version (after last code overhaul)
        # Filter trades with exit_time >= epoch when _kelly_reset_version was last incremented
        if self._kelly_reset_version >= 3:
            strat_trades = [t for t in strat_trades if t.get("exit_time", "") >= "2026-07-30"]
        if len(strat_trades) < 20:
            return 0.25  # lower risk while building trade history
        wins = [abs(t.get("r_multiple", 0)) for t in strat_trades if t.get("r_multiple", 0) > 0]
        losses = [abs(t.get("r_multiple", 0)) for t in strat_trades if t.get("r_multiple", 0) < 0]
        wr = len(wins) / len(strat_trades)
        avg_r_win = sum(wins) / len(wins) if wins else 0.5
        avg_r_loss = sum(losses) / len(losses) if losses else 1.0
        if avg_r_win <= 0 or avg_r_loss <= 0:
            return 1.0
        kelly = wr - (1 - wr) / (avg_r_win / avg_r_loss)
        if kelly <= 0:
            return 0.0
        quarter = kelly * 0.25
        return max(0.0, quarter)

    async def _process_asset(
        self,
        asset: str,
        candles: list[PerpCandle],
        hl: ExchangeAdapter,
        exchange: ExecutionEngine,
    ):
        self._last_entry_diag_cycle = self._cycle_count
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

        # Track MAE, MFE, and peak unrealized PnL
        if pos:
            if not getattr(pos, 'mae_pct', None): pos.mae_pct = 0.0
            if not getattr(pos, 'mfe_pct', None): pos.mfe_pct = 0.0
            if not getattr(pos, 'peak_upnl', None): pos.peak_upnl = 0.0
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
            upnl_val = float(getattr(pos, 'unrealized_pnl', 0) or 0)
            if upnl_val > (getattr(pos, 'peak_upnl', 0) or 0):
                pos.peak_upnl = upnl_val

        if pos and exchange.check_liquidation(asset):
            logger.warning("%s liquidated", asset)
            self.risk.record_position_close(asset)
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
                        close_pct = 0.5 if reason == "tp1" else 1.0
                        await self._close(asset, pos, price, reason, exchange, close_pct)
                        if close_pct >= 1.0:
                            return
                        pos.tp1_scaled = True

                # BTC knife-guard time-stop: close longs held >60min during confirmed downtrend
                if self._btc_knife_block and pos.side == Side.LONG:
                    age = datetime.now(timezone.utc) - pos.entry_time
                    if age.total_seconds() > 3600:
                        logger.warning("KNIFE_TIMESTOP %s: long held %.0f min >60 min during BTC knife guard -- closing",
                                       asset, age.total_seconds() / 60)
                        await self._close(asset, pos, price, "knife_guard_time_exit", exchange)
                        return

                # Portfolio health sweep: close positions past their edge
                if getattr(pos, "entry_time", None):
                    age_min = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 60
                    upnl = float(getattr(pos, "unrealized_pnl", 0) or 0)
                    peak = float(getattr(pos, "peak_upnl", 0) or 0)

                    # Resolve position strategy for SMA200 exemption
                    strategy = getattr(pos, 'strategy', '') or getattr(pos, 'signal_source', '')
                    # Skip max_age for TP1-scaled positions (chandelier manages) and SMA200 macro positions
                    tp1_or_sma200 = getattr(pos, 'tp1_scaled', False) or strategy == 'sma200_perp'
                    if tp1_or_sma200:
                        pass
                    # A. Max age: any position open >12h gets closed
                    elif age_min > 720:
                        logger.info("PORTFOLIO_SWEEP %s: %.0f min max age exceeded -- closing", asset, age_min)
                        await self._close(asset, pos, price, "portfolio_max_age", exchange)
                        return

                    # B. Peak decay: profitable position decayed >N%% from peak (strategy-specific)
                    # Research: momentum (XS/Trend) = 75%% retracement, mean-rev = 50%%
                    # Skip peak decay for macro SMA200 positions — they need days/weeks to develop
                    if strategy != 'sma200_perp' and peak > 3:
                        _decay_threshold = 75 if strategy in ("xs_momentum", "trend_4h") else 50
                        decay_pct = (peak - upnl) / peak * 100
                        if upnl > 0 and decay_pct > _decay_threshold:
                            logger.info("PORTFOLIO_SWEEP %s: upnl=$%.1f peak=$%.1f decay=%.0f%% > %d%% -- closing",
                                        asset, upnl, peak, decay_pct, _decay_threshold)
                            await self._close(asset, pos, price, "portfolio_peak_decay", exchange)
                            return

                    # SMA200 crossover exit: close SMA200 positions when signal reverses
                    if strategy == 'sma200_perp':
                        _, _, current_signal = self._compute_sma200(asset)
                        cs = current_signal.upper()
                        if (pos.side == Side.LONG and cs == 'SHORT') or                            (pos.side == Side.SHORT and cs == 'LONG'):
                            logger.info("SMA200_CROSSOVER %s: %s at $%.2f signal flipped to %s -- closing",
                                        asset, pos.side.name, price, current_signal)
                            await self._close(asset, pos, price, "sma200_crossover", exchange)
                            return
                    # SMA200 stop-loss: close if stop_price breached for shorts
                    if strategy == 'sma200_perp':
                        sl = getattr(pos, 'stop_loss', None) or getattr(pos, 'stop_price', None)
                        if sl and sl > 0:
                            if pos.side == Side.SHORT and price > sl:
                                logger.info("SMA200_STOP_LOSS %s: short at $%.4f stop=$%.4f -- closing",
                                            asset, price, sl)
                                await self._close(asset, pos, price, "sma200_stop_loss", exchange)
                                return
                            elif pos.side == Side.LONG and price < sl:
                                logger.info("SMA200_STOP_LOSS %s: long at $%.4f stop=$%.4f -- closing",
                                            asset, price, sl)
                                await self._close(asset, pos, price, "sma200_stop_loss", exchange)
                                return

                    # SMA200 distance-banded time exit: stronger signal = longer hold
                    if strategy == 'sma200_perp':
                        sma_val, _, _ = self._compute_sma200(asset)
                        if sma_val > 0:
                            dist_pct = abs(price - sma_val) / sma_val * 100
                            if dist_pct > 15:
                                max_age_min = 10080
                            elif dist_pct > 10:
                                max_age_min = 8640
                            else:
                                max_age_min = 7200
                            if age_min > max_age_min:
                                logger.info("SMA200_TIME_EXIT %s: age=%.0fh dist=%.1f%% max=%.0fh -- closing",
                                            asset, age_min/60, dist_pct, max_age_min/60)
                                await self._close(asset, pos, price, "sma200_time_exit", exchange)
                                return
                    # C. Stale: open >60min with zero or negative PnL (non-SMA200 only)
                    elif age_min > 720 and upnl <= 0:  # stale exit removed
                        logger.info("PORTFOLIO_SWEEP %s: stale %.0f min upnl=$%.1f -- closing", asset, age_min, upnl)
                        await self._close(asset, pos, price, "portfolio_stale", exchange)
                        return

        # Position concentration check: max 50% equity in one position (after sweep)
        if pos:
            notional = abs(pos.size) * (pos.entry_price if pos.entry_price > 0 else 1)
            max_notional = exchange.equity * 0.50 if hasattr(exchange, 'equity') else 999999
            if notional > max_notional:
                logger.warning("CONCENTRATION_HALT %s: notional=%.0f > 50%%%% eq=%.0f", asset, notional, max_notional)
                return

        # Dual regime (NotebookLM): primary (200-period) for sizing/risk,
        # secondary (50-period) for entry direction
        # Regime detection: use 4h aggregated candles (NotebookLM round 10)
        # 1h Hurst only covers 2 days; 4h covers 8 days — matches trend timeframe
        candles_4h = self.candle_4h_cache.get(asset, [])
        if len(candles_4h) >= 30:
            regime = self._infer_regime(asset, candles_4h, 30)
        else:
            regime = self._infer_regime(asset, candles, 30)

        # Reset CUSUM accumulators on TRENDING to prevent stuck state
        if regime == RegimeType.TRENDING:
            self._cusum.get(asset, {}).pop("S_high", None)
            self._cusum.get(asset, {}).pop("S_low", None)

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
        self._oi_blocked = not oi_ok  # flag for per-strategy check

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
            logger.info("ENTRY_DIAG %s: skip -- consecutive_loss: %s", asset, cl_msg)
            return

        # Event kill: block entries around high-impact economic events
        blocked = self._event_kill.should_block()
        if blocked:
            logger.info("ENTRY_DIAG %s: skip -- event: %s (%s)", asset, blocked["event"], blocked["currency"])
            return

        # Evaluate entries
        for strat in self.strategies:
            # Skip if strategy is paused by sharpe_tracker
            if self._paused_strategies:
                ps_names = [p.split()[0] for p in self._paused_strategies if p]
                if strat.name() in ps_names:
                    continue
            sig_bucket = f"{strat.name()}:{asset}"
            all_signals = altfins_sigs + self.signal_cache.get("all", [])
            # Candle routing per strategy
            if strat.name() == "trend_4h":
                strat_candles = self.candle_4h_cache.get(asset, candles)
            elif strat.name() == "fade_5m":
                strat_candles = self.candle_5m_cache.get(asset, candles)
            else:
                strat_candles = candles
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
            result = strat.should_enter(asset, strat_candles, all_signals, regime, pos, funding_rate)
            if result is None:
                continue

            side, confidence, meta = result
            min_threshold = self.risk.get_confidence_threshold(strat.name())
            if strat.name() == "xs_momentum":
                min_threshold = 0.55
            # Loss cooldown: skip this side if a large loss (R < -2.0) happened in last 6h
            cd_key = f"{asset}_{side.name}"
            cd_expiry = self._loss_cooldowns.get(cd_key, 0.0)
            if cd_expiry > time.time():
                remaining = (cd_expiry - time.time()) / 60
                logger.info("ENTRY_DIAG %s %s: skip -- loss_cooldown %s (%.0fm remaining)", asset, strat.name(), side.name, remaining)
                continue

            # CUSUM regime gate: Trend4h needs trending, Fade5m needs ranging
            if regime in (RegimeType.TRENDING, RegimeType.STRONGLY_TRENDING):
                if strat.name() == "fade_5m":
                    logger.info("ENTRY_DIAG %s %s: skip -- regime=%s blocks fade in trending", asset, strat.name(), regime.name)
                    continue
            else:
                if strat.name() == "trend_4h":
                    logger.info("ENTRY_DIAG %s %s: skip -- regime=%s blocks trend in non-trending", asset, strat.name(), regime.name)
                    continue

            if confidence < min_threshold:
                logger.info("ENTRY_DIAG %s %s: skip -- confidence %.3f < %.3f", asset, strat.name(), confidence, min_threshold)
                continue
            # Per-strategy OI check (confidence-discounted — high conf overrides)
            if getattr(self, '_oi_blocked', False) and confidence < 0.60:
                logger.debug("%s %s: skip -- OI (conf=%.2f < 0.60)", strat.name(), asset, confidence)
                continue
            # Absolute floor for non-MR strategies (trend/need >= 0.70 regardless of global MIN)
            if self._btc_bear_market and side == Side.LONG:
                logger.info("ENTRY_DIAG %s %s: skip -- BTC bear market (longs blocked)", strat.name(), asset)
                continue

            # Leverage + stop sizing
            lev, lev_reason = self.risk.compute_leverage(asset, candles, side)
            stop_pct, stop_reason = self.risk.compute_stop_distance(asset, candles)

            if stop_pct <= 0:
                continue

            entry_price = price
            strat_name = strat.name()
            kelly = self._kelly_factor_for_strategy(strat_name)
            qty, risk_dollars, max_notional = self.risk.position_size(
                asset, exchange.equity, stop_pct, entry_price, exchange.gross_exposure,
                kelly_fraction=kelly
            )

            if qty <= 0:
                continue

            # Strategy budget scaling (based on 30d Sharpe)
            budget_weight = self._strategy_budget.get(strat_name, 1.0)
            if budget_weight <= 0:
                continue
            if budget_weight < 1.0:
                qty = qty * budget_weight
                logger.debug(
                    "%s %s: budget=%s qty=%s -> %s",
                    strat_name, asset, budget_weight, qty / budget_weight, qty,
                )

            # Minimum position size: 20% equity for MR (scales with account)
            mr_min_notional = max(1000.0, exchange.equity * 0.20)
            if strat_name == "mr" and qty * entry_price < mr_min_notional:
                logger.debug("%s %s: notional $%.0f < $%.0f min — skipping", strat_name, asset, qty * entry_price, mr_min_notional)
                continue
            # Friction cost floor: all strategies need minimum notional so fees don't dominate edge
            # Research: fees > 5%% of risk kills edge. Fee ~$0.63/trade, require notional > $300
            # to keep fee < 2%% of risk on a 1%% risk trade
            _min_notional = max(300.0, exchange.equity * 0.06)
            if qty * entry_price < _min_notional:
                logger.debug("%s %s: notional $%.0f < $%.0f — skipping (friction floor)", strat_name, asset, qty * entry_price, _min_notional)
                continue
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
            if strat.name() == "mr":
                logger.info("ENTRY_ATTEMPT %s %s conf=%.2f qty=%.4f stop=%.2f entry=%.2f",
                             asset, side.value, confidence, qty, stop_price, entry_price)
            # Fade5m uses limit orders to avoid filling at the wick
            if strat.name() == "fade_5m":
                _order_type = OrderType.LIMIT
                _price = entry_price * 0.999 if side == Side.SHORT else entry_price * 1.001
            else:
                _order_type = OrderType.MARKET
                _price = None
            # RiskGovernor: account-level entry check before committing
            notional = qty * entry_price
            current_positions = len(getattr(exchange, '_active_positions', {}))
            logger.info("GOV_CHECK %s: notional=$%.0f equity=$%.0f gross=$%.0f pos=%d kelly=%.2f stop=%.1f%% qty=%.4f",
                        asset, notional, exchange.equity, exchange.gross_exposure, current_positions,
                        kelly, stop_pct, qty)
            gov_ok, gov_msg = self.governor.check_entry(
                asset=asset,
                notional=notional,
                current_gross=exchange.gross_exposure,
                current_equity=exchange.equity,
                current_positions=current_positions,
            )
            if not gov_ok:
                logger.info("ENTRY_DIAG %s: skip -- governor: %s", asset, gov_msg)
                continue

            order = Order(
                asset=asset,
                side=side,
                order_type=_order_type,
                quantity=qty,
                price=_price,
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
                # Trade notifications suppressed — individual trade messages cause Telegram spam

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

        # Regime score [0-3] — gate SMA200 on trending regimes only
        if hasattr(self, 'regime') and hasattr(self, '_get_daily_candles_for_regime'):
            regime_candles = self._get_daily_candles_for_regime(asset)
            if regime_candles:
                regime_score, regime_hurst, regime_adx, regime_atrp = self.regime.score(asset, regime_candles)
                if regime_score < 2:
                    logger.info("ENTRY_DIAG %s: REGIME score=%d H=%.2f ADX=%.1f — SMA200 blocked (not trending)",
                                asset, regime_score, regime_hurst, regime_adx)
            else:
                regime_score = 1
        else:
            regime_score = 1

        # SMA200 kill criterion: permanently halt if 28d net PnL is negative
        sma200_pnl = self._sma200_net_pnl_28d()
        kill_key = "sma200_killed"
        killed_raw = self.store.get_state(kill_key)
        killed = killed_raw.get("value") if isinstance(killed_raw, dict) else bool(killed_raw) if isinstance(killed_raw, (bool, int)) else False
        kill_threshold = -600.0
        if killed and sma200_pnl < kill_threshold:
            if self._cycle_count > getattr(self, '_smakill_logged_cycle', -1):
                logger.warning("SMA200_KILLED: 28d pnl=$%.2f — SMA200 entries halted until PnL improves", sma200_pnl)
                self._smakill_logged_cycle = self._cycle_count
        elif sma200_pnl < kill_threshold:
            if self._cycle_count > getattr(self, '_smakill_logged_cycle', -1):
                logger.warning("SMA200_KILL: 28d pnl=$%.2f — halting SMA200 entries", sma200_pnl)
                self._smakill_logged_cycle = self._cycle_count
            self.store.put_state(kill_key, {"value": True, "pnl_28d": round(sma200_pnl, 2)})
            killed = True
        # SMA200 kill recovery: auto-clear if 28d PnL recovers above -$100
        if killed and sma200_pnl >= -100.0:
            logger.info("SMA200_KILL_RECOVER: 28d pnl=$%.2f — auto-clearing kill state", sma200_pnl)
            self.store.put_state(kill_key, {"value": False, "pnl_28d": round(sma200_pnl, 2)})
            killed = False

        # SMA200 macro trend entry: if no strategy entered but SMA200 has clear direction
        if not killed and regime_score >= 2 and not pos and len(exchange.positions) < self.risk.max_concurrent_positions and price > 0:
            pass
            sma, last_close, signal_side = self._compute_sma200(asset)
            if sma > 0 and signal_side != "flat":
                side = Side.LONG if signal_side == "long" else Side.SHORT
                stop_pct, _ = self.risk.compute_stop_distance(asset, self.candle_4h_cache.get(asset, self.candle_cache.get(asset, [])))
                lev = 2.0
                entry_price = price
                qty, risk_dollars, max_not = self.risk.position_size(
                    asset, exchange.equity, stop_pct, entry_price, exchange.gross_exposure,
                    kelly_fraction=self._kelly_factor_for_strategy("sma200_perp")
                )
                if qty > 0:
                    notional = qty * entry_price
                    if notional >= 200:
                        # RiskGovernor: account-level check before SMA200 entry
                        _gov_ok, _gov_msg = self.governor.check_entry(
                            asset=asset, notional=notional,
                            current_gross=exchange.gross_exposure,
                            current_equity=exchange.equity,
                            current_positions=len(getattr(exchange, '_active_positions', {})),
                        )
                        if not _gov_ok:
                            logger.info("ENTRY_DIAG %s: skip -- governor: %s", asset, _gov_msg)
                            return
                        stop_price = (entry_price * (1 - stop_pct/100)) if side == Side.LONG else (entry_price * (1 + stop_pct/100))
                        order = Order(
                            asset=asset, side=side, order_type=OrderType.MARKET,
                            quantity=qty, price=None, stop_price=stop_price,
                            reduce_only=False, leverage=lev,
                            metadata={"component_sources": ["sma200_perp"]},
                        )
                        order_id = await exchange.place_order(order)
                        if order_id:
                            p = exchange.positions.get(asset)
                            if p:
                                p.strategy = "sma200_perp"
                                p.signal_source = "sma200_perp"
                                p.entry_confidence = 0.80
                                p.stop_loss = stop_price
                            self.risk.record_position_open(asset)
                            logger.info("SMA200_PERP %s %s qty=%.4f @ %.0f stop=%.0f lev=%.1fx risk=$%.0f",
                                side.value.upper(), asset, qty, entry_price, stop_price, lev, risk_dollars)

    async def _process_external_intents(self, exchange: ExecutionEngine, hl: ExchangeAdapter):
        for row in self.store.pending_intents(limit=25):
            try:
                intent = TradeIntent.from_row(row)
                ok, reason = await self._execute_intent(intent, exchange, hl)
                self.store.update_intent_status(intent.id, "accepted" if ok else "rejected", reason)
                if not ok:
                    self.store.record_delegation_metric(intent.source if intent.source else "freqtrade", False)
            except Exception as e:
                self.store.update_intent_status(int(row["id"]), "rejected", f"invalid_intent: {e}")

    async def _execute_intent(self, intent: TradeIntent, exchange: ExecutionEngine, hl: ExchangeAdapter) -> tuple[bool, str]:
        now = datetime.now(timezone.utc)
        if now >= intent.expires_at:
            return False, "expired"
        if intent.asset not in self.assets:
            return False, "asset_not_allowed"
        conf_gate = self.risk.get_confidence_threshold(intent.strategy or "")
        if intent.confidence < conf_gate:
            return False, f"confidence_below_gate: {intent.confidence:.2f} >= {conf_gate:.2f}"

        # Paused_strategies check: reject intents for paused strategies
        self._paused_strategies = self._paused_strategies or []
        _ps_names = [p.split()[0] for p in self._paused_strategies if p]
        if intent.strategy in _ps_names:
            self._last_entry_diag_cycle = self._cycle_count
            logger.info("INTENT_DIAG %s: skip -- paused_strategy %s", intent.asset, intent.strategy)
            return False, f"paused: {intent.strategy}"

        existing = await exchange.fetch_position(intent.asset)
        if existing:
            return False, "position_already_open"

        price = await exchange.fetch_price(intent.asset)
        entry_price = price if price > 0 else intent.intended_entry_price
        if entry_price <= 0:
            return False, "no_price"

        risk_ok, risk_msg = self.risk.allow_entry(exchange.gross_exposure, exchange.effective_leverage)
        if not risk_ok:
            logger.info("INTENT_DIAG %s: skip -- risk: %s", intent.asset, risk_msg)
            return False, risk_msg
        oi_ok, oi_msg = self.risk.oi_gate_allows(intent.asset)
        if not oi_ok:
            self._last_entry_diag_cycle = self._cycle_count
            logger.info("INTENT_DIAG %s: skip -- OI: %s", intent.asset, oi_msg)
            return False, oi_msg
        funding_rate = await hl.get_funding_rate(intent.asset)
        funding_ok, funding_msg = self.risk.funding_gate(funding_rate)
        if not funding_ok:
            self._last_entry_diag_cycle = self._cycle_count
            logger.info("INTENT_DIAG %s: skip -- funding: %s", intent.asset, funding_msg)
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
            intent.asset, exchange.equity, stop_pct, entry_price, exchange.gross_exposure,
            kelly_fraction=self._kelly_factor_for_strategy(intent.strategy)
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
        exchange: ExecutionEngine,
        close_pct: float = 1.0,
    ):
        if price <= 0:
            logger.warning("_close %s: invalid price %.2f -- skipping", asset, price)
            return

        trade = exchange.close_position(
            asset=asset,
            price=price,
            close_pct=close_pct,
            exit_reason=reason,
            strategy=pos.strategy or "",
            signal_source=pos.signal_source or "",
            entry_confidence=pos.entry_confidence or 0.0,
            regime=getattr(pos, "regime", "") or "",
            entry_regime=getattr(pos, "entry_regime", "") or getattr(pos, "regime", "") or "",
            mae_pct=getattr(pos, 'mae_pct', 0.0),
            mfe_pct=getattr(pos, 'mfe_pct', 0.0),
        )
        if trade is None:
            logger.warning("_close %s: close_position returned None", asset)
            return

        # Record trade in risk manager
        if close_pct < 1.0:
            # Scale down peak_upnl to match reduced position size
            if hasattr(pos, "peak_upnl"):
                pos.peak_upnl = (pos.peak_upnl or 0) * close_pct
            self.store.save_trade(asdict(trade))
            return

        # Full close
        pnl_pct = trade.pnl_pct if abs(trade.pnl_pct) < 1e6 else 0.0
        pnl_dollars = trade.pnl_dollars

        self.risk.record_trade(asset, pnl_pct, pnl_dollars, reason)
        self.risk.record_position_close(asset)
        self.risk.record_sleeve_outcome(pos.strategy or "", pnl_dollars)
        # Loss cooldown: R < -2.0 = regime mismatch, block same asset+side for 6h
        if hasattr(trade, 'r_multiple') and trade.r_multiple < -2.0:
            side_label = "LONG" if pos.side == Side.LONG else "SHORT"
            cd_key = f"{asset}_{side_label}"
            expiry = time.time() + 21600  # 6 hours
            self._loss_cooldowns[cd_key] = expiry
            logger.warning("LOSS_COOLDOWN %s %s: R=%.2f < -2.0 — blocked until %s",
                          asset, side_label, trade.r_multiple,
                          datetime.fromtimestamp(expiry, tz=timezone.utc).strftime("%H:%M UTC"))
        self.signal_tracker.record(trade.signal_source, pnl_dollars > 0)
        for source in getattr(pos, "component_sources", []):
            self.signal_tracker.record(source, pnl_dollars > 0)

        if pos.strategy:
            for strat in self.strategies:
                if strat.name() == pos.strategy and hasattr(strat, "on_exit"):
                    strat.on_exit(asset)
                    break

        self.store.save_trade(asdict(trade))


    async def _maybe_reflect(self, exchange: ExecutionEngine):
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
        # Persist suggestions immediately so they survive restart
        if self._suggested_params:
            self.store.put_state("pending_param_changes", json.dumps(self._suggested_params))
            logger.info("PENDING PARAM CHANGES: %d suggestions persisted", len(self._suggested_params))

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