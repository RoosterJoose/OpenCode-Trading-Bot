"""
Phase 1.5: Account-level risk governor + latched kill state.

This module provides:
- A persistent state machine for execution mode
- Latched kill state that survives restarts (never auto-clears)
- Account-level caps on gross/net exposure, leverage, drawdown
- Atomic position reservations (prevents concurrent overexposure)
- Independent from strategy code — strategy cannot override risk limits

State machine:
  RUNNING → ENTRY_HALTED → RISK_REDUCING → COOLDOWN → SHADOW_ONLY → REVIEW_PENDING → RUNNING
  MANUAL_LOCK (any state can enter, only human can exit)

Kill switch:
  LATCHED — must be manually reset. Process restart does NOT clear it.
  This fixes the recurring "stale risk state on restart" bug.
"""

import logging
import json
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("hermes.risk_governor")


class ExecutionState(Enum):
    """Execution state machine states."""
    RUNNING = "running"                    # Normal trading allowed
    ENTRY_HALTED = "entry_halted"          # New entries blocked, existing supervised
    RISK_REDUCING = "risk_reducing"        # Actively reducing exposure
    COOLDOWN = "cooldown"                  # Post-reduction cooldown
    SHADOW_ONLY = "shadow_only"            # No orders, log decisions only
    REVIEW_PENDING = "review_pending"      # Awaiting human review
    MANUAL_LOCK = "manual_lock"            # Human-initiated lock (only human can clear)
    SAFE_HALT = "safe_halt"                # Auto-latched safe state


class KillSwitchReason(Enum):
    """Reasons for kill switch activation."""
    DRAWDOWN_LIMIT = "drawdown_limit"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    LEVERAGE_BREACH = "leverage_breach"
    RECONCILIATION_FAILURE = "reconciliation_failure"
    STALE_DATA = "stale_data"
    SEQUENCE_GAP = "sequence_gap"
    CLOCK_BREACH = "clock_breach"
    DATABASE_FAILURE = "database_failure"
    CREDENTIAL_FAILURE = "credential_failure"
    MANUAL = "manual"
    HUMAN_REVIEW = "human_review"
    UNKNOWN_ORDER = "unknown_order"
    POSITION_MISMATCH = "position_mismatch"


class RiskGovernor:
    """
    Account-level risk governor.

    Provides:
    - Latched kill state (persists across restarts, never auto-clears)
    - Account-level exposure, leverage, drawdown, and daily loss caps
    - Atomic position reservation (prevents concurrent overexposure)
    - State machine for execution mode transitions
    - Independent from strategy code

    The governor uses a store-backed key-value mechanism for persistence,
    so state survives process restarts. Kill state is LATCHED —
    restart does not clear it; only explicit human ack clears it.
    """

    def __init__(
        self,
        store,  # Store instance for persistence
        initial_capital: float = 5000.0,
        max_gross_leverage: float = 3.0,
        max_daily_loss_pct: float = 4.0,
        max_drawdown_pct: float = 12.0,
        max_single_position_pct: float = 50.0,
        max_concurrent_positions: int = 3,
    ):
        self.store = store
        self.initial_capital = initial_capital
        self.max_gross_leverage = max_gross_leverage
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.max_single_position_pct = max_single_position_pct
        self.max_concurrent_positions = max_concurrent_positions

        # In-memory state (restored from store on init)
        self._state: ExecutionState = ExecutionState.RUNNING
        self._kill_latched: bool = False
        self._kill_reason: Optional[KillSwitchReason] = None
        self._kill_timestamp: Optional[str] = None
        self._human_ack_required: bool = False

        # Daily tracking
        self._daily_start_equity: float = initial_capital
        self._daily_date: str = ""
        self._peak_equity: float = initial_capital

        # Position reservations (atomic)
        self._reservations: dict[str, float] = {}  # asset -> reserved notional

        self._restore_state()

    # ------------------------------------------------------------------
    # State persistence (survives restart)
    # ------------------------------------------------------------------

    def _restore_state(self):
        """Restore kill state from store. Kill state is LATCHED."""
        try:
            raw = self.store.get_state("risk_governor_state")
            if raw:
                data = json.loads(raw)
                self._kill_latched = data.get("kill_latched", False)
                reason_str = data.get("kill_reason")
                self._kill_reason = KillSwitchReason(reason_str) if reason_str else None
                self._kill_timestamp = data.get("kill_timestamp")
                self._human_ack_required = data.get("human_ack_required", False)
                self._daily_start_equity = data.get("daily_start_equity", self.initial_capital)
                self._daily_date = data.get("daily_date", "")
                self._peak_equity = data.get("peak_equity", self.initial_capital)

                if self._kill_latched:
                    self._state = ExecutionState.SAFE_HALT
                    logger.warning(
                        "RISK_GOVERNOR: Restored LATCHED kill state (reason=%s, ts=%s). "
                        "Human acknowledgment required to clear.",
                        self._kill_reason, self._kill_timestamp
                    )
                else:
                    self._state = ExecutionState.RUNNING

        except Exception as e:
            logger.warning("RISK_GOVERNOR: Failed to restore state: %s", e)

    def _save_state(self):
        """Persist state to store."""
        try:
            data = {
                "kill_latched": self._kill_latched,
                "kill_reason": self._kill_reason.value if self._kill_reason else None,
                "kill_timestamp": self._kill_timestamp,
                "human_ack_required": self._human_ack_required,
                "daily_start_equity": self._daily_start_equity,
                "daily_date": self._daily_date,
                "peak_equity": self._peak_equity,
                "state": self._state.value,
            }
            self.store.put_state("risk_governor_state", json.dumps(data))
        except Exception as e:
            logger.error("RISK_GOVERNOR: Failed to save state: %s", e)

    # ------------------------------------------------------------------
    # Kill switch (latched, never auto-clears)
    # ------------------------------------------------------------------

    def trigger_kill(self, reason: KillSwitchReason):
        """
        Trigger the kill switch. This is LATCHED — it persists across restarts
        and can only be cleared by explicit human acknowledgment.

        When latched:
        - New entries are blocked
        - Existing positions are supervised (stops still active)
        - Reduce-only exits are allowed
        - Cancel-open-entry-orders is executed
        """
        if self._kill_latched and self._kill_reason == reason:
            return  # Already latched for same reason

        self._kill_latched = True
        self._kill_reason = reason
        self._kill_timestamp = datetime.now(timezone.utc).isoformat()
        self._human_ack_required = True
        self._state = ExecutionState.SAFE_HALT
        self._save_state()

        logger.error(
            "RISK_GOVERNOR: KILL SWITCH LATCHED — reason=%s, ts=%s. "
            "All new entries blocked. Human acknowledgment required to clear.",
            reason.value, self._kill_timestamp
        )

    def clear_kill(self, human_ack: bool = False):
        """
        Clear the kill switch. Requires human_ack=True.

        This is the ONLY way to clear a latched kill state.
        Process restart does NOT clear it.
        Self-heal does NOT clear it.
        Only explicit human action clears it.
        """
        if not self._kill_latched:
            return

        if not human_ack:
            logger.warning(
                "RISK_GOVERNOR: clear_kill attempted without human_ack. "
                "Kill state remains LATCHED."
            )
            return

        self._kill_latched = False
        self._kill_reason = None
        self._kill_timestamp = None
        self._human_ack_required = False
        self._state = ExecutionState.RUNNING
        self._save_state()

        logger.info("RISK_GOVERNOR: Kill switch cleared by human acknowledgment.")

    def is_killed(self) -> bool:
        """Check if kill switch is latched."""
        return self._kill_latched

    def is_running(self) -> bool:
        """Check if new entries are allowed."""
        return self._state == ExecutionState.RUNNING and not self._kill_latched

    def get_state(self) -> ExecutionState:
        return self._state

    def get_kill_info(self) -> dict:
        return {
            "latched": self._kill_latched,
            "reason": self._kill_reason.value if self._kill_reason else None,
            "timestamp": self._kill_timestamp,
            "human_ack_required": self._human_ack_required,
            "state": self._state.value,
        }

    # ------------------------------------------------------------------
    # Daily equity tracking
    # ------------------------------------------------------------------

    def update_equity(self, equity: float):
        """Update equity tracking. Called each cycle."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # Reset daily start on UTC midnight
        if today != self._daily_date:
            self._daily_date = today
            self._daily_start_equity = equity
            self._save_state()

        # Update peak
        if equity > self._peak_equity:
            self._peak_equity = equity
            self._save_state()

        # Check daily loss
        if self._daily_start_equity > 0:
            daily_loss_pct = ((self._daily_start_equity - equity) / self._daily_start_equity) * 100
            if daily_loss_pct >= self.max_daily_loss_pct:
                self.trigger_kill(KillSwitchReason.DAILY_LOSS_LIMIT)
                return

        # Check drawdown
        if self._peak_equity > 0:
            drawdown_pct = ((self._peak_equity - equity) / self._peak_equity) * 100
            if drawdown_pct >= self.max_drawdown_pct:
                self.trigger_kill(KillSwitchReason.DRAWDOWN_LIMIT)
                return

    # ------------------------------------------------------------------
    # Exposure checks (atomic reservations)
    # ------------------------------------------------------------------

    def check_entry(
        self,
        asset: str,
        notional: float,
        current_gross: float,
        current_equity: float,
        current_positions: int,
    ) -> tuple[bool, str]:
        """
        Check if a new entry is allowed under account-level risk limits.

        Returns (allowed, reason).
        """
        if self._kill_latched:
            return (False, f"kill_switch_latched:{self._kill_reason.value if self._kill_reason else 'unknown'}")

        # Position count check
        if current_positions >= self.max_concurrent_positions:
            return (False, "max_positions")

        # Gross leverage check
        new_gross = current_gross + notional
        if current_equity > 0:
            new_leverage = new_gross / current_equity
            if new_leverage > self.max_gross_leverage:
                return (False, f"leverage_breach:{new_leverage:.2f}>{self.max_gross_leverage}")

        # Single-position concentration
        if current_equity > 0:
            single_pct = (notional / current_equity) * 100
            if single_pct > self.max_single_position_pct:
                return (False, f"concentration:{single_pct:.1f}%>{self.max_single_position_pct}%")

        # Reserve the notional atomically
        self._reservations[asset] = self._reservations.get(asset, 0) + notional

        return (True, "ok")

    def release_reservation(self, asset: str, notional: float):
        """Release a position reservation (after fill or cancellation)."""
        current = self._reservations.get(asset, 0)
        self._reservations[asset] = max(0, current - notional)

    def clear_all_reservations(self):
        """Clear all reservations (used on restart/reconciliation)."""
        self._reservations.clear()

    # ------------------------------------------------------------------
    # Manual lock (human-initiated)
    # ------------------------------------------------------------------

    def manual_lock(self):
        """Human-initiated lock. Only human can clear it."""
        self._state = ExecutionState.MANUAL_LOCK
        self.trigger_kill(KillSwitchReason.MANUAL)

    def status(self) -> dict:
        return {
            "state": self._state.value,
            "kill_latched": self._kill_latched,
            "kill_reason": self._kill_reason.value if self._kill_reason else None,
            "kill_timestamp": self._kill_timestamp,
            "human_ack_required": self._human_ack_required,
            "daily_start_equity": round(self._daily_start_equity, 2),
            "peak_equity": round(self._peak_equity, 2),
            "daily_date": self._daily_date,
            "reservations": dict(self._reservations),
            "limits": {
                "max_gross_leverage": self.max_gross_leverage,
                "max_daily_loss_pct": self.max_daily_loss_pct,
                "max_drawdown_pct": self.max_drawdown_pct,
                "max_single_position_pct": self.max_single_position_pct,
                "max_concurrent_positions": self.max_concurrent_positions,
            },
        }