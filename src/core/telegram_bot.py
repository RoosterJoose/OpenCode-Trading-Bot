"""
Two-way Telegram bot for Hermes — command and control via chat.

Commands:
  /status     — equity, trades, Sharpe, positions
  /positions  — open positions with PnL
  /pause      — pause trading
  /resume     — resume trading
  /help       — list commands

Polling-based (no public webhook needed). Runs as background asyncio task.
"""
import json
import logging
import time
import asyncio
import sqlite3
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, token: str, authorized_chat_id: str, store):
        self.token = token
        self.authorized_chat_id = str(authorized_chat_id)
        self.store = store
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._last_update_id = 0
        self._last_sent = {}
        self._running = False

    @property
    def enabled(self) -> bool:
        return bool(self.token) and bool(self.authorized_chat_id)

    async def start_polling(self):
        if not self.enabled:
            logger.info("TelegramBot: disabled")
            return
        self._running = True
        logger.info("TelegramBot: polling started for chat %s", self.authorized_chat_id)
        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("TelegramBot poll: %s", e)
            await asyncio.sleep(5)

    def stop(self):
        self._last_sent = {}
        self._running = False

    async def _get_updates(self):
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/getUpdates",
                params={"offset": self._last_update_id + 1, "timeout": 5},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") and data.get("result"):
                    updates = data["result"]
                    self._last_update_id = max(u["update_id"] for u in updates)
                    return updates
            return []

    async def _handle_update(self, update: dict):
        msg = update.get("message") or update.get("callback_query", {}).get("message")
        text = msg.get("text", "").strip() if msg else ""
        chat_id = str(msg.get("chat", {}).get("id")) if msg else ""
        if not text or not chat_id:
            return
        if chat_id != self.authorized_chat_id:
            logger.debug("TelegramBot: unauthorized chat %s", chat_id)
            return

        cmd = text.lower().split()[0]
        logger.info("TelegramBot: command %s from %s", cmd, chat_id)

        try:
            if cmd == "/status":
                await self._cmd_status()
            elif cmd in ("/positions", "/pos"):
                await self._cmd_positions()
            elif cmd == "/pause":
                await self._cmd_pause()
            elif cmd == "/resume":
                await self._cmd_resume()
            elif cmd == "/help":
                await self._cmd_help()
            elif cmd in ("/balance", "/bal"):
                await self._cmd_balance()
            elif cmd == "/audit":
                await self._cmd_audit()
            elif cmd == "/close_all":
                await self._cmd_close_all()
            else:
                await self._send("Unknown command. Try /help")
        except Exception as e:
            logger.exception("TelegramBot: error handling %s: %s", cmd, e)
            await self._send(f"Error: {type(e).__name__}: {str(e)[:100]}")

    async def _cmd_status(self):
        eq = self.store.get_state("paper_equity") or "?"
        peak = self.store.get_state("paper_peak_equity") or "?"
        positions_raw = self.store.get_state("positions") or []
        n_pos = len(positions_raw)

        try:
            eq_f = float(eq)
            peak_f = float(peak)
            dd = ((peak_f - eq_f) / peak_f * 100) if peak_f > 0 else 0
            dd_str = f" DD={dd:.1f}%"
        except (ValueError, TypeError):
            dd_str = ""

        msg = f"Equity: ${eq}{dd_str}\nPeak: ${peak}\nOpen positions: {n_pos}"

        snapshots = self.store.recent_equity(limit=2)
        if len(snapshots) >= 2:
            try:
                pnl_pct = (eq_f - snapshots[-1]["equity"]) / snapshots[-1]["equity"] * 100
                msg += f"\nSession PnL: {pnl_pct:+.2f}%"
            except (IndexError, KeyError, ValueError, TypeError):
                pass

        await self._send(msg)

    async def _cmd_positions(self):
        positions = self.store.get_state("positions") or []
        if not positions:
            await self._send("No open positions")
            return

        lines = []
        for p in positions:
            pnl = p.get("unrealized_pnl", 0)
            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
            lines.append(
                f"{emoji} {p['side']} {p['asset']} "
                f"@ ${p['entry_price']:.0f} "
                f"sz={p.get('size', 0):.4f} "
                f"lev={p.get('leverage', 1):.1f}x "
                f"PnL=${pnl:.0f}"
            )
        await self._send("\n".join(lines))

    async def _cmd_pause(self):
        self.store.put_state("bot_paused", "true")
        reasons_raw = self.store.get_state("pause_reasons") or "[]"
        if isinstance(reasons_raw, str):
            reasons = json.loads(reasons_raw)
        else:
            reasons = list(reasons_raw)
        reasons.append(f"manual_pause_telegram_{datetime.now(timezone.utc).isoformat()}")
        self.store.put_state("pause_reasons", json.dumps(reasons))
        logger.warning("TelegramBot: manual pause via /pause")
        await self._send("Bot paused.")

    async def _cmd_resume(self):
        self.store.put_state("bot_paused", "false")
        self.store.put_state("pause_reasons", json.dumps([]))
        logger.warning("TelegramBot: manual resume via /resume")
        await self._send("Bot resumed.")

    async def _cmd_balance(self):
        eq = self.store.get_state("paper_equity") or "0"
        peak = self.store.get_state("paper_peak_equity") or "0"
        try:
            eq_f = float(eq)
            peak_f = float(peak)
            dd = (peak_f - eq_f) / peak_f * 100 if peak_f > 0 else 0
        except (ValueError, TypeError):
            eq_f = 0; dd = 0

        # Aggressive bot
        agg_eq = "?"
        try:
            c = sqlite3.connect("/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db")
            raw = c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()
            if raw:
                agg_eq = f"${float(raw[0].strip(chr(34))):.2f}"
            c.close()
        except Exception as e:
            agg_eq = f"error: {e}"

        msg = 'Conservative: ${:.2f} (DD {:.1f}%)\nAggressive: {}'.format(eq_f, dd, agg_eq)
        await self._send(msg)

    async def _cmd_audit(self):
        await self._send("Running full audit...")
        proc = await asyncio.create_subprocess_exec(
            "/opt/hermes-trading-bot/.venv/bin/python",
            "/opt/hermes-trading-bot/scripts/telegram_audit.py",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        result = stdout.decode().strip()
        await self._send(result)

    async def _cmd_close_all(self):
        positions = self.store.get_state("positions") or []
        if not positions:
            await self._send("No open positions to close")
            return
        self.store.put_state("close_all_pending", '"true"')
        logger.warning("TelegramBot: /close_all — marking %d positions for close", len(positions))
        await self._send(f"Close all pending for {len(positions)} positions. Bot will close them on next cycle.")

    async def _cmd_help(self):
        await self._send(
            "/status — equity, drawdown, positions\n"
            "/positions — open positions with PnL\n"
            "/pause — pause trading immediately\n"
            "/resume — resume trading\n"
            "/balance — see both bot balances\n"
            "/audit — run full bot audit\n"
            "/close_all — emergency close all positions\n"
            "/help — this message"
        )

    async def send(self, text: str):
        await self._send(text)

    # One-way alert helpers (replacing NotificationService)
    async def bot_started(self, equity: float) -> None:
        await self.send(f"Bot started — equity=${equity:.0f}")

    async def position_opened(self, asset: str, side: str, entry_price: float,
                                size: float, leverage: float, confidence: float,
                                strategy: str) -> None:
        await self.send(
            f"{side.upper()} {asset} @ ${entry_price:.0f} "
            f"sz={size:.4f} lev={leverage:.1f}x conf={confidence:.2f} [{strategy}]"
        )

    async def position_closed(self, asset: str, side: str, entry_price: float,
                                exit_price: float, pnl_dollars: float, reason: str,
                                strategy: str) -> None:
        emoji = "\U0001f7e2" if pnl_dollars >= 0 else "\U0001f534"
        await self.send(
            f"{emoji} CLOSE {asset} {side.upper()} @ ${exit_price:.0f} "
            f"PnL=${pnl_dollars:.2f} ({reason}) [{strategy}]"
        )

    async def bot_paused(self, reason: str) -> None:
        await self.send(f"Bot paused\nReason: {reason[:200]}")

    async def daily_drawdown(self, current_equity: float, peak_equity: float,
                                drawdown_pct: float) -> None:
        pass

    def _rate_limit(self, key: str, min_interval: float) -> bool:
        now = time.time()
        last = self._last_sent.get(key, 0.0)
        if now - last < min_interval:
            return False
        self._last_sent[key] = now
        return True

    async def _send(self, text: str):
        if not self.enabled:
            return
        if not self._rate_limit("global", 30):
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.authorized_chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code != 200:
                    logger.warning("TelegramBot: send failed %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.debug("TelegramBot: send error %s", e)
