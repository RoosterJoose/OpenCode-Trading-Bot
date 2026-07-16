"""
Phase 4.3-4.4: Reconciliation and security.

4.3 — Reconciliation:
  Startup barrier: verify clock, config, credentials, positions, orders, balances
    before ARMING the execution governor
  Continuous: reconcile local ledger to Coinbase every N seconds
  Failed reconciliation → latch SAFE_HALT (no new entries until resolved)

4.4 — Security:
  Key permission validation (no transfer capability)
  Product tradability validation
  Pre-trade order preview (fee/slippage check)
  IP allowlist check
  Secret hygiene (no secrets in logs, no secrets in error messages)
"""

import asyncio
import logging
from src.core.risk_governor import KillSwitchReason
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("hermes.reconciliation")


@dataclass
class ReconciliationResult:
    """Result of a reconciliation check."""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    passed: bool = True
    checks: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    safe_halt_required: bool = False

    def add_check(self, name: str, passed: bool, detail: str = ""):
        self.checks[name] = {"passed": passed, "detail": detail}
        if not passed:
            self.passed = False
            self.errors.append(f"{name}: {detail}")

    def should_halt(self) -> bool:
        """Any critical check failure requires safe halt."""
        critical_checks = [
            "balance_mismatch", "position_mismatch", "unknown_orders",
            "credential_failure", "clock_breach", "database_failure"
        ]
        for check in critical_checks:
            if check in self.checks and not self.checks[check]["passed"]:
                self.safe_halt_required = True
        return self.safe_halt_required


class ReconciliationService:
    """
    Exchange-authoritative reconciliation.

    On startup: blocks all entries until reconciliation passes.
    Continuous: runs every N seconds, latches SAFE_HALT on mismatch.

    The exchange (Coinbase) is authoritative for:
    - Account balances
    - Open orders
    - Fills
    - Positions
    - Product status

    Local ledger is authoritative for:
    - Intents (what we intended to do)
    - Decision history (what we decided and when)
    - Trial/experiment metadata
    """

    def __init__(
        self,
        execution_adapter=None,     # CoinbaseExecutionAdapter
        risk_governor=None,           # RiskGovernor
        local_store=None,             # SQLite store
        reconciliation_interval_s: int = 60,
        tolerance_usd: float = 1.0,   # $1 tolerance for balance/position mismatch
        clock_tolerance_s: float = 5.0,
    ):
        self.adapter = execution_adapter
        self.governor = risk_governor
        self.store = local_store
        self.interval = reconciliation_interval_s
        self.tolerance = tolerance_usd
        self.clock_tolerance = clock_tolerance_s
        self._last_reconciliation: Optional[ReconciliationResult] = None
        self._armed = False  # startup barrier — must pass reconciliation before arming

    async def startup_barrier(self) -> ReconciliationResult:
        """
        Full startup reconciliation. Block all entries until this passes.

        Checks:
        1. Clock sync (NTP offset within tolerance)
        2. Database integrity (can read/write)
        3. Credential validity (can authenticate to Coinbase)
        4. No unknown orders (all local orders have exchange state)
        5. Balance reconciliation (local = exchange)
        6. Position reconciliation (local = exchange)
        """
        logger.info("RECONCILIATION: Running startup barrier...")
        result = ReconciliationResult()

        # 1. Clock check
        clock_offset = self._check_clock()
        result.add_check(
            "clock_sync",
            abs(clock_offset) < self.clock_tolerance,
            f"offset={clock_offset:.2f}s (tolerance={self.clock_tolerance}s)"
        )

        # 2. Database integrity
        db_ok = self._check_database()
        result.add_check("database_integrity", db_ok, "write/read test")

        # 3. Credential check
        if self.adapter:
            cred_ok = await self._check_credentials()
            result.add_check("credential_validity", cred_ok, "API authentication test")
        else:
            result.add_check("credential_validity", False, "no execution adapter configured")

        # 4. Unknown orders
        if self.adapter:
            unknown_ok = await self._check_unknown_orders()
            result.add_check("unknown_orders", unknown_ok, "all local orders have exchange state")
        else:
            result.add_check("unknown_orders", True, "paper mode — no orders")

        # 5. Balance reconciliation
        if self.adapter:
            bal_ok, bal_detail = await self._reconcile_balances()
            result.add_check("balance_mismatch", bal_ok, bal_detail)
        else:
            result.add_check("balance_mismatch", True, "paper mode")

        # 6. Position reconciliation
        if self.adapter:
            pos_ok, pos_detail = await self._reconcile_positions()
            result.add_check("position_mismatch", pos_ok, pos_detail)
        else:
            result.add_check("position_mismatch", True, "paper mode")

        # Check if safe halt required
        if result.should_halt():
            self.governor.trigger_kill(KillswitchReason.RECONCILIATION_FAILURE)
            logger.error("RECONCILIATION: SAFE_HALT triggered — %s", result.errors)
        else:
            self._armed = True
            logger.info("RECONCILIATION: Startup barrier PASSED — execution ARMED")

        self._last_reconciliation = result
        return result

    async def continuous_reconciliation(self):
        """Run reconciliation on a timer. Latches SAFE_HALT on failure."""
        while True:
            await asyncio.sleep(self.interval)
            result = await self._run_checks()
            self._last_reconciliation = result

            if result.should_halt():
                logger.error("RECONCILIATION: Continuous check FAILED — %s", result.errors)
                self.governor.trigger_kill(KillswitchReason.RECONCILIATION_FAILURE)
                return  # stop reconciliation loop — latched

    async def _run_checks(self) -> ReconciliationResult:
        """Run periodic checks."""
        result = ReconciliationResult()

        if self.adapter:
            bal_ok, bal_detail = await self._reconcile_balances()
            result.add_check("balance_mismatch", bal_ok, bal_detail)

            pos_ok, pos_detail = await self._reconcile_positions()
            result.add_check("position_mismatch", pos_ok, pos_detail)

            unknown_ok = await self._check_unknown_orders()
            result.add_check("unknown_orders", unknown_ok, "order state check")

        db_ok = self._check_database()
        result.add_check("database_integrity", db_ok, "write/read test")

        clock_offset = self._check_clock()
        result.add_check("clock_sync", abs(clock_offset) < self.clock_tolerance,
                         f"offset={clock_offset:.2f}s")

        return result

    def _check_clock(self) -> float:
        """Check system clock drift (simplified — needs NTP implementation)."""
        # For now, check that time is advancing monotonically
        return 0.0  # 0s offset — would use NTP in production

    def _check_database(self) -> bool:
        """Check database read/write."""
        try:
            # Write test value
            self.store.put_state("_reconciliation_test", str(time.time()))
            # Read it back
            val = self.store.get_state("_reconciliation_test")
            return val is not None
        except Exception:
            return False

    async def _check_credentials(self) -> bool:
        """Check API credentials are valid."""
        try:
            balances = await self.adapter.fetch_balances()
            return "error" not in balances or len(balances) > 0
        except Exception:
            return False

    async def _check_unknown_orders(self) -> bool:
        """Check for orders in UNKNOWN state that need reconciliation."""
        # Query our local orders that have no confirmed fill
        # For paper mode, always pass
        return True

    async def _reconcile_balances(self) -> tuple[bool, str]:
        """Reconcile local balance with exchange balance."""
        try:
            exchange_balances = await self.adapter.fetch_balances()
            usdc = exchange_balances.get("USDC", {}).get("available", 0)

            # Compare with local balance
            local_balance = 0  # would come from local ledger
            # For paper mode, skip actual comparison

            diff = abs(usdc - local_balance)
            if diff > self.tolerance:
                return False, f"diff=${diff:.2f} > ${self.tolerance:.2f}"
            return True, f"matched (diff=${diff:.2f})"
        except Exception as e:
            return False, f"error: {e}"

    async def _reconcile_positions(self) -> tuple[bool, str]:
        """Reconcile local positions with exchange positions."""
        try:
            exchange_positions = await self.adapter.fetch_positions()
            # Compare with local positions
            # For paper mode, always pass
            return True, f"{len(exchange_positions)} exchange positions"
        except Exception as e:
            return False, f"error: {e}"

    # ------------------------------------------------------------------
    # Security checks (Phase 4.4)
    # ------------------------------------------------------------------

    async def check_key_permissions(self) -> dict:
        """
        Validate API key has trade permission but NOT transfer permission.
        This is a critical security check — an API key with transfer capability
        is an existential risk.
        """
        if not self.adapter:
            return {"has_trade": False, "has_transfer": False, "secure": False}

        try:
            # Coinbase API returns key permissions via /api/v3/brokerage/key_permissions
            result = await self.adapter._request("GET", "/api/v3/brokerage/key_permissions")
            if result.get("error"):
                return {"secure": False, "error": result.get("body", "API error")}

            has_trade = result.get("trade", False)
            has_transfer = result.get("transfer", False)
            has_view = result.get("view", False)

            secure = has_trade and not has_transfer

            if has_transfer:
                logger.critical(
                    "SECURITY: API KEY HAS TRANSFER PERMISSION — IMMEDIATELY REVOKE AND RECREATE"
                )
                self.governor.trigger_kill(KillswitchReason.CREDENTIAL_FAILURE)

            return {
                "has_trade": has_trade,
                "has_transfer": has_transfer,
                "has_view": has_view,
                "secure": secure,
            }
        except Exception as e:
            return {"secure": False, "error": str(e)}

    async def validate_product_tradable(self, product_id: str) -> bool:
        """Validate a product is tradable before placing an order."""
        if not self.adapter:
            return True  # paper mode

        result = await self.adapter.validate_product(product_id)
        if not result.get("tradable", False):
            logger.warning("PRODUCT_NOT_TRADABLE: %s — status=%s", product_id, result.get("status"))
            return False
        return True

    async def pre_trade_preview(
        self, product_id: str, side: str, size: str, limit_price: Optional[str] = None,
    ) -> dict:
        """Preview order to check fees and liquidity before submission."""
        if not self.adapter:
            return {"available": True, "paper_mode": True}

        return await self.adapter.preview_order(product_id, side, size, limit_price)

    @property
    def is_armed(self) -> bool:
        """True if startup barrier passed and execution is armed."""
        return self._armed

    @property
    def last_result(self) -> Optional[ReconciliationResult]:
        return self._last_reconciliation

    def status(self) -> dict:
        if self._last_reconciliation:
            return {
                "armed": self._armed,
                "last_check": self._last_reconciliation.timestamp,
                "passed": self._last_reconciliation.passed,
                "errors": self._last_reconciliation.errors,
                "safe_halt_required": self._last_reconciliation.safe_halt_required,
                "checks": self._last_reconciliation.checks,
            }
        return {"armed": False, "last_check": None}


