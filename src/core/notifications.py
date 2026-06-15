"""
Notification service for Hermes — sends Telegram messages on key events.

Telegram is the simplest push notification for a solo trader:
- Zero infrastructure to host (just a webhook URL)
- Instant push to phone
- Works on mobile data, no email clutter
- Can send alerts, daily summaries, error reports

Usage:
  export HERMES_TELEGRAM_WEBHOOK="https://api.telegram.org/bot<token>/sendMessage"
  export HERMES_TELEGRAM_CHAT_ID="<chat_id>"
"""
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self):
        self.webhook_url = (
            os.environ.get("HERMES_TELEGRAM_WEBHOOK", "")
            or os.environ.get("HERMES_TELEGRAM_BOT_TOKEN", "")
        )
        self.chat_id = os.environ.get("HERMES_TELEGRAM_CHAT_ID", "")
        self._last_alert_type: dict[str, datetime] = {}
        self._min_interval_seconds = 300  # 5 min dedup per alert type

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url) and bool(self.chat_id)

    def _should_send(self, alert_type: str) -> bool:
        """Deduplicate: same alert type at most once per 5 minutes"""
        now = datetime.now(timezone.utc)
        last = self._last_alert_type.get(alert_type)
        if last and (now - last).total_seconds() < self._min_interval_seconds:
            return False
        self._last_alert_type[alert_type] = now
        return True

    async def send(self, message: str, alert_type: str = "general") -> None:
        """Send a Telegram message (async via httpx)"""
        if not self.enabled:
            logger.debug("Notification disabled: no webhook configured")
            return
        if not self._should_send(alert_type):
            return

        try:
            import httpx

            payload = {
                "chat_id": self.chat_id,
                "text": f"[Hermes] {message}",
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook_url, json=payload)
                if resp.status_code != 200:
                    logger.warning("Telegram send failed: %d %s", resp.status_code, resp.text[:200])
                else:
                    logger.info("Telegram alert: %s", message[:80])
        except Exception as e:
            logger.debug("Telegram send error: %s", e)

    async def position_opened(self, asset: str, side: str, entry_price: float,
                               size: float, leverage: float, confidence: float,
                               strategy: str) -> None:
        await self.send(
            f"{side.upper()} {asset} @ ${entry_price:.0f} sz={size:.4f} "
            f"lev={leverage:.1f}x conf={confidence:.2f} [{strategy}]",
            alert_type=f"position_open_{asset}",
        )

    async def position_closed(self, asset: str, side: str, entry_price: float,
                               exit_price: float, pnl_dollars: float, reason: str,
                               strategy: str) -> None:
        emoji = "🟢" if pnl_dollars >= 0 else "🔴"
        await self.send(
            f"{emoji} CLOSE {asset} {side.upper()} @ ${exit_price:.0f} "
            f"PnL=${pnl_dollars:.2f} ({reason}) [{strategy}]",
            alert_type=f"position_close_{asset}",
        )

    async def bot_paused(self, reason: str) -> None:
        await self.send(
            f"⚠️ BOT PAUSED\nReason: {reason}",
            alert_type="bot_paused",
        )

    async def bot_started(self, equity: float) -> None:
        await self.send(
            f"✅ Bot started — equity=${equity:.0f}",
            alert_type="bot_started",
        )

    async def daily_drawdown(self, current_equity: float, peak_equity: float,
                              drawdown_pct: float) -> None:
        if drawdown_pct > 3.0:
            await self.send(
                f"📉 Daily drawdown: {drawdown_pct:.1f}% "
                f"(${peak_equity:.0f} → ${current_equity:.0f})",
                alert_type="daily_drawdown",
            )
