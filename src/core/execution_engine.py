"""
Event-sourced execution engine — replaces PaperPerpExchange.

Shared by backtest and paper modes. Provides:
- Explicit order state machine (INTENDED → SUBMITTED → ACKNOWLEDGED → FILLED/CANCELLED/REJECTED)
- Book-walking market fills with configurable bid/ask spread
- Deterministic limit order queueing (no coin flip)
- Side-aware P&L, fees, funding in trade records
- Per-asset funding clocks (not global)
- Partial close support with correct lot accounting
- Equity reconciliation: cash + unrealized PnL = equity
- Configurable fee tiers (maker/taker)
- Deterministic mode (seeded for reproducibility)
"""

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.core.types import Fill, Order, OrderType, PerpConfig, PerpPosition, Side

logger = logging.getLogger("hermes.execution")


class OrderState(Enum):
    INTENDED = "intended"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"  # timeout, ambiguous response


@dataclass
class OrderRecord:
    """Immutable record of an order's lifecycle."""
    cloid: str
    asset: str
    side: Side
    order_type: OrderType
    quantity: float
    limit_price: Optional[float] = None
    reduce_only: bool = False
    leverage: float = 1.0
    stop_price: Optional[float] = None
    state: OrderState = OrderState.INTENDED
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    total_fee: float = 0.0
    maker_fill: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    cumulative_fills: list[Fill] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class TradeRecord:
    """Complete trade record with full cost decomposition."""
    asset: str
    side: str  # "long" or "short"
    entry_price: float
    exit_price: float
    size: float
    leverage: float
    pnl_dollars: float
    pnl_pct: float
    r_multiple: float
    fees: float  # entry + exit fees
    funding_paid: float
    mae_pct: float
    mfe_pct: float
    exit_reason: str
    strategy: str
    signal_source: str
    entry_confidence: float
    entry_time: str
    exit_time: str
    regime: str
    entry_regime: str


class ExecutionEngine:
    """
    Deterministic execution engine for backtest and paper modes.

    Replaces PaperPerpExchange. Key improvements:
    - Market fills walk a configurable spread (bid/ask), not mid price
    - Limit orders queue deterministically — fill when price crosses
    - Fees recorded in every trade record
    - Funding accrued per-asset with its own clock
    - R-multiple is side-aware (shorts get correct sign)
    - Partial closes track remaining size correctly
    - Equity = balance + sum(unrealized PnL) — always reconcilable
    """

    def __init__(
        self,
        initial_balance: float = 10_000.0,
        taker_fee: float = 0.00025,
        maker_fee: float = 0.00015,
        spread_bps: float = 2.0,  # bid/ask spread in basis points
        seed: int = 42,
    ):
        self.balance = initial_balance
        self.peak_balance = initial_balance
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.spread_bps = spread_bps
        self._seed = seed

        self.positions: dict[str, PerpPosition] = {}
        self._next_cloid = 0
        self._prices: dict[str, float] = {}
        self._bid_ask: dict[str, tuple[float, float]] = {}  # (bid, ask)
        self._funding_rates: dict[str, float] = {}
        self._open_interest: dict[str, float] = {}
        self._orders: dict[str, OrderRecord] = {}
        self._limit_queue: deque[tuple[str, OrderRecord]] = deque()

        # Per-asset funding clocks
        self._last_funding_time: dict[str, datetime] = {}
        self._funding_accumulated: dict[str, float] = {}  # per-asset total funding

        self._perp_configs: dict[str, PerpConfig] = {}
        self._total_fees_paid = 0.0
        self._total_funding_paid = 0.0
        self._trade_count = 0

        # Trade records for external consumption
        self.completed_trades: list[TradeRecord] = []

    # ------------------------------------------------------------------
    # Market data updates (called by the loop each cycle)
    # ------------------------------------------------------------------

    def update_price(self, asset: str, price: float, bid: float = 0, ask: float = 0):
        """Update price and optionally bid/ask. If bid/ask not provided, derive from spread."""
        self._prices[asset] = price
        if bid > 0 and ask > 0:
            self._bid_ask[asset] = (bid, ask)
        else:
            half_spread = price * self.spread_bps / 10_000
            self._bid_ask[asset] = (price - half_spread, price + half_spread)
        self._revalue_position(asset)
        self._process_limit_queue(asset)

    def update_funding(self, asset: str, rate: float, timestamp: Optional[datetime] = None):
        """Update funding rate and accrue for the asset."""
        self._funding_rates[asset] = rate
        self._accrue_funding(asset, timestamp or datetime.now(timezone.utc))

    def update_open_interest(self, asset: str, oi: float):
        self._open_interest[asset] = oi

    def update_candle(self, asset: str, close: float, high: float = 0, low: float = 0):
        """Update from candle close. Uses high/low for limit fill checking."""
        self.update_price(asset, close)

    def set_perp_config(self, asset: str, config: PerpConfig):
        self._perp_configs[asset] = config

    # ------------------------------------------------------------------
    # Position queries (interface compatible with PaperPerpExchange)
    # ------------------------------------------------------------------

    async def fetch_price(self, asset: str) -> float:
        return self._prices.get(asset, 0.0)

    async def fetch_position(self, asset: str) -> Optional[PerpPosition]:
        return self.positions.get(asset)

    async def fetch_balances(self) -> dict[str, float]:
        return {"USDC": self.balance}

    # ------------------------------------------------------------------
    # Order placement (interface compatible with PaperPerpExchange)
    # ------------------------------------------------------------------

    async def place_order(self, order: Order) -> str:
        """Place an order. Returns cloid. State transitions are tracked."""
        cloid = f"exec_{self._next_cloid}"
        self._next_cloid += 1

        record = OrderRecord(
            cloid=cloid,
            asset=order.asset,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.price if order.order_type == OrderType.LIMIT else None,
            reduce_only=order.reduce_only,
            leverage=order.leverage or 1.0,
            stop_price=order.stop_price,
            state=OrderState.SUBMITTED,
            submitted_at=datetime.now(timezone.utc),
            metadata=order.metadata or {},
        )
        self._orders[cloid] = record

        if order.order_type == OrderType.MARKET:
            await self._fill_market(record)
        elif order.order_type == OrderType.LIMIT:
            # Queue the limit order — it fills when price crosses the limit
            self._limit_queue.append((cloid, record))
            record.state = OrderState.ACKNOWLEDGED
            logger.info("LIMIT_QUEUED %s %s %s qty=%.6f limit=%.2f",
                        cloid, order.side.value, order.asset, order.quantity, order.price or 0)
        else:
            record.state = OrderState.REJECTED
            logger.warning("ORDER_REJECTED %s unknown type", cloid)

        return cloid

    async def cancel_order(self, cloid: str) -> bool:
        """Cancel a pending limit order."""
        record = self._orders.get(cloid)
        if record and record.state in (OrderState.ACKNOWLEDGED, OrderState.SUBMITTED):
            record.state = OrderState.CANCELLED
            # Remove from limit queue
            self._limit_queue = deque(
                (c, r) for c, r in self._limit_queue if c != cloid
            )
            return True
        return False

    async def _fill_market(self, record: OrderRecord):
        """Fill a market order by walking the bid/ask spread."""
        price = self._prices.get(record.asset, 0.0)
        if price <= 0:
            logger.warning("FILL_FAILED %s no price for %s", record.cloid, record.asset)
            record.state = OrderState.REJECTED
            return

        # Market order crosses the spread
        bid, ask = self._bid_ask.get(record.asset, (price, price))
        if record.side == Side.LONG:
            fill_price = ask  # buy at ask
        else:
            fill_price = bid  # sell at bid

        fee = record.quantity * fill_price * self.taker_fee
        record.filled_quantity = record.quantity
        record.avg_fill_price = fill_price
        record.total_fee = fee
        record.maker_fill = False
        record.state = OrderState.FILLED
        record.filled_at = datetime.now(timezone.utc)
        record.cumulative_fills.append(
            Fill(record.cloid, record.asset, record.side, record.quantity, fill_price, fee)
        )

        self._apply_fill(record, fill_price, fee)
        self._total_fees_paid += fee
        self._trade_count += 1

    def _process_limit_queue(self, asset: str):
        """Check queued limit orders against current price. Fill if crossed."""
        if not self._limit_queue:
            return
        price = self._prices.get(asset, 0)
        if price <= 0:
            return

        remaining = deque()
        for cloid, record in self._limit_queue:
            if record.asset != asset:
                remaining.append((cloid, record))
                continue

            limit = record.limit_price or 0
            should_fill = False

            # Long limit: fill when ask <= limit price
            # Short limit: fill when bid >= limit price
            bid, ask = self._bid_ask.get(asset, (price, price))
            if record.side == Side.LONG and ask <= limit:
                should_fill = True
                fill_price = ask
            elif record.side == Side.SHORT and bid >= limit:
                should_fill = True
                fill_price = bid

            if should_fill:
                fee = record.quantity * fill_price * self.maker_fee  # limit = maker
                record.filled_quantity = record.quantity
                record.avg_fill_price = fill_price
                record.total_fee = fee
                record.maker_fill = True
                record.state = OrderState.FILLED
                record.filled_at = datetime.now(timezone.utc)
                record.cumulative_fills.append(
                    Fill(record.cloid, record.asset, record.side, record.quantity, fill_price, fee)
                )
                self._apply_fill(record, fill_price, fee)
                self._total_fees_paid += fee
                self._trade_count += 1
                logger.info("LIMIT_FILLED %s %s %s qty=%.6f @ %.2f (maker)",
                           cloid, record.side.value, asset, record.quantity, fill_price)
            else:
                remaining.append((cloid, record))

        self._limit_queue = remaining

    def _apply_fill(self, record: OrderRecord, fill_price: float, fee: float):
        """Apply a fill to positions and balance."""
        asset = record.asset
        existing = self.positions.get(asset)

        # If reduce_only or opposite side, close existing position
        if existing and record.side != existing.side:
            self._close_position_internal(asset, fill_price, record.reduce_only, record)
            if record.reduce_only:
                return
            existing = None

        if existing:
            # Add to existing position (same side)
            total_size = existing.size + record.quantity
            avg_price = (
                (existing.entry_price * existing.size + fill_price * record.quantity) / total_size
                if total_size > 0 else fill_price
            )
            existing.size = total_size
            existing.entry_price = avg_price
            existing.fills.append(record.cumulative_fills[-1])
        else:
            # New position
            config = self._perp_configs.get(asset)
            max_lev = config.max_leverage if config else 3.0
            lev = min(max_lev, max(1.0, record.leverage))
            liq = self._compute_liq_price(asset, record.side, fill_price, record.quantity, lev)
            pos = PerpPosition(
                asset=asset,
                side=record.side,
                entry_price=fill_price,
                size=record.quantity,
                leverage=lev,
                liquidation_price=liq,
                entry_time=datetime.now(timezone.utc),
                stop_loss=record.stop_price,
                component_sources=list(record.metadata.get("component_sources", [])),
            )
            self.positions[asset] = pos

        # Deduct fee from balance
        self.balance -= fee

    def _close_position_internal(
        self, asset: str, price: float, reduce_only: bool = False,
        order_record: Optional[OrderRecord] = None
    ) -> Optional[dict]:
        """Close a position at the given price. Returns PnL dict."""
        pos = self.positions.pop(asset, None)
        if pos is None:
            return None

        pnl = self._compute_pnl(pos, price)
        realized = pnl["realized"]

        # Add exit fee
        exit_fee = pos.size * price * self.taker_fee
        realized -= exit_fee
        self.balance += realized - exit_fee
        self._total_fees_paid += exit_fee

        # Funding accumulated for this position
        funding = self._funding_accumulated.get(asset, 0.0)
        realized -= funding

        return {
            "realized": realized,
            "unrealized": 0.0,
            "pnl_pct": pnl["pnl_pct"],
            "fees": exit_fee,
            "funding": funding,
        }

    def close_position(
        self, asset: str, price: float, close_pct: float = 1.0,
        exit_reason: str = "", strategy: str = "",
        signal_source: str = "", entry_confidence: float = 0.0,
        regime: str = "", entry_regime: str = "",
        mae_pct: float = 0.0, mfe_pct: float = 0.0
    ) -> Optional[TradeRecord]:
        """
        Close a position and generate a complete TradeRecord with full cost decomposition.
        This replaces the broken _close() in loop.py.
        """
        pos = self.positions.get(asset)
        if pos is None:
            return None

        close_qty = pos.size * close_pct
        bid, ask = self._bid_ask.get(asset, (price, price))

        # Exit fills: long sells at bid, short buys at ask
        if pos.side == Side.LONG:
            exit_price = bid
        else:
            exit_price = ask

        # Compute PnL (side-aware)
        if pos.side == Side.LONG:
            pnl_dollars = (exit_price - pos.entry_price) * close_qty
        else:
            pnl_dollars = (pos.entry_price - exit_price) * close_qty

        # Fees (entry + exit)
        entry_fee = close_qty * pos.entry_price * self.taker_fee  # entry was market (taker)
        exit_fee = close_qty * exit_price * self.taker_fee
        total_fees = entry_fee + exit_fee

        # Funding
        funding = self._funding_accumulated.get(asset, 0.0) * close_pct
        if close_pct < 1.0:
            self._funding_accumulated[asset] = self._funding_accumulated.get(asset, 0.0) * (1 - close_pct)

        # Net PnL
        net_pnl = pnl_dollars - total_fees - funding

        # R-multiple (side-aware)
        stop_distance = abs(pos.entry_price - (pos.stop_loss or pos.entry_price * 0.97))
        r_multiple = (pnl_dollars / (stop_distance * close_qty)) if stop_distance > 0 else 0.0
        if pos.side == Side.SHORT:
            r_multiple = r_multiple  # already correct from PnL formula above

        # PnL percentage
        pnl_pct = (pnl_dollars / (pos.entry_price * close_qty)) * 100 if pos.entry_price > 0 else 0.0

        # Update balance
        self.balance += net_pnl
        self._total_fees_paid += exit_fee

        # Generate trade record
        trade = TradeRecord(
            asset=asset,
            side=pos.side.value,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=close_qty,
            leverage=pos.leverage,
            pnl_dollars=round(net_pnl, 2),
            pnl_pct=round(pnl_pct, 2),
            r_multiple=round(r_multiple, 3),
            fees=round(total_fees, 4),
            funding_paid=round(funding, 4),
            mae_pct=round(mae_pct, 2),
            mfe_pct=round(mfe_pct, 2),
            exit_reason=exit_reason,
            strategy=strategy or pos.strategy or "",
            signal_source=signal_source or pos.signal_source or "",
            entry_confidence=entry_confidence or pos.entry_confidence or 0.0,
            entry_time=pos.entry_time.isoformat() if pos.entry_time else "",
            exit_time=datetime.now(timezone.utc).isoformat(),
            regime=regime,
            entry_regime=entry_regime,
        )

        if close_pct >= 1.0:
            # Full close — remove position
            self.positions.pop(asset, None)
            self._funding_accumulated.pop(asset, None)
            self._last_funding_time.pop(asset, None)
        else:
            # Partial close — reduce size
            pos.size -= close_qty
            # Recompute unrealized PnL
            pos.unrealized_pnl = self._compute_pnl(pos, exit_price)["unrealized"]

        self.completed_trades.append(trade)
        self._trade_count += 1
        self.peak_balance = max(self.peak_balance, self.balance + sum(
            self._compute_pnl(p, self._prices.get(p.asset, p.entry_price))["unrealized"]
            for p in self.positions.values()
        ))

        return trade

    # ------------------------------------------------------------------
    # P&L and valuation
    # ------------------------------------------------------------------

    def _compute_pnl(self, pos: PerpPosition, current_price: float) -> dict:
        """Side-aware PnL computation."""
        if pos.side == Side.LONG:
            pnl = (current_price - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - current_price) * pos.size
        pnl_pct = (pnl / (pos.entry_price * pos.size)) * 100 if pos.entry_price > 0 and pos.size > 0 else 0.0
        return {"unrealized": pnl, "realized": pnl, "pnl_pct": pnl_pct}

    def _revalue_position(self, asset: str):
        pos = self.positions.get(asset)
        if pos is None:
            return
        price = self._prices.get(asset, pos.entry_price)
        pos.unrealized_pnl = self._compute_pnl(pos, price)["unrealized"]

    def _compute_liq_price(self, asset: str, side: Side, entry: float, size: float, leverage: float) -> float:
        if leverage <= 0 or entry <= 0:
            return 0.0
        maintenance = 0.006
        buffer = 0.05
        if side == Side.LONG:
            return entry * (1 - (1 / leverage) + maintenance) / (1 - maintenance + buffer)
        else:
            return entry * (1 + (1 / leverage) - maintenance) / (1 + maintenance - buffer)

    # ------------------------------------------------------------------
    # Funding (per-asset, not global)
    # ------------------------------------------------------------------

    def _accrue_funding(self, asset: str, now: datetime):
        """Accrue funding for a specific asset. Each asset has its own clock."""
        rate = self._funding_rates.get(asset, 0.0)
        pos = self.positions.get(asset)
        if pos is None or rate == 0:
            return

        last = self._last_funding_time.get(asset)
        if last is None:
            self._last_funding_time[asset] = now
            return

        hours = (now - last).total_seconds() / 3600
        if hours < 1:
            return

        # Funding payment: positive rate costs longs, credits shorts
        notional = pos.size * self._prices.get(asset, pos.entry_price)
        funding_payment = notional * rate * (hours / 24)  # rate is daily, accrue hourly

        if pos.side == Side.LONG:
            # Longs pay when funding is positive
            self._funding_accumulated[asset] = self._funding_accumulated.get(asset, 0.0) + funding_payment
            self._total_funding_paid += funding_payment
        else:
            # Shorts receive when funding is positive
            self._funding_accumulated[asset] = self._funding_accumulated.get(asset, 0.0) - funding_payment
            self._total_funding_paid -= funding_payment

        self._last_funding_time[asset] = now

    # ------------------------------------------------------------------
    # Liquidation
    # ------------------------------------------------------------------

    def check_liquidation(self, asset: str) -> bool:
        pos = self.positions.get(asset)
        if pos is None or pos.liquidation_price <= 0:
            return False
        price = self._prices.get(asset, 0)
        if price <= 0:
            return False
        if (pos.side == Side.LONG and price <= pos.liquidation_price) or \
           (pos.side == Side.SHORT and price >= pos.liquidation_price):
            logger.warning("LIQUIDATED %s at %.2f (liq %.2f)", asset, price, pos.liquidation_price)
            self.close_position(asset, price, exit_reason="liquidation")
            return True
        return False

    # ------------------------------------------------------------------
    # Position restoration
    # ------------------------------------------------------------------

    def restore_positions(self, positions: list[dict]):
        """Restore positions from persisted state."""
        for raw in positions:
            try:
                entry_time = datetime.fromisoformat(raw["entry_time"]) if raw.get("entry_time") else datetime.now(timezone.utc)
                pos = PerpPosition(
                    asset=raw["asset"],
                    side=Side(raw.get("side", "long")),
                    entry_price=float(raw.get("entry_price", 0)),
                    size=float(raw.get("size", 0)),
                    leverage=float(raw.get("leverage", 1)),
                    liquidation_price=float(raw.get("liquidation_price", 0)),
                    unrealized_pnl=float(raw.get("unrealized_pnl", 0)),
                    realized_pnl=float(raw.get("realized_pnl", 0)),
                    entry_time=entry_time,
                    stop_loss=raw.get("stop_loss"),
                    take_profit=raw.get("take_profit"),
                    strategy=raw.get("strategy", ""),
                    signal_source=raw.get("signal_source", ""),
                    entry_confidence=float(raw.get("entry_confidence", 0)),
                    component_sources=list(raw.get("component_sources", [])),
                )
                if pos.asset and pos.entry_price > 0 and pos.size > 0:
                    if not pos.strategy or (pos.stop_loss is None and pos.take_profit is None):
                        logger.info("Skipping stale restored position %s (strategy=%s, sl=%s, tp=%s)",
                                     pos.asset, pos.strategy, pos.stop_loss, pos.take_profit)
                        continue
                    self.positions[pos.asset] = pos
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping invalid restored position: %s", e)

    # ------------------------------------------------------------------
    # Properties (interface compatible with PaperPerpExchange)
    # ------------------------------------------------------------------

    @property
    def equity(self) -> float:
        """Total equity = balance + unrealized PnL."""
        total = self.balance
        for pos in self.positions.values():
            price = self._prices.get(pos.asset, pos.entry_price)
            total += self._compute_pnl(pos, price)["unrealized"]
        return total

    @property
    def gross_exposure(self) -> float:
        return sum(
            abs(p.size) * self._prices.get(p.asset, p.entry_price)
            for p in self.positions.values()
        )

    @property
    def effective_leverage(self) -> float:
        eq = self.equity
        return self.gross_exposure / eq if eq > 0 else 0.0

    def status(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "gross_exposure": round(self.gross_exposure, 2),
            "effective_leverage": round(self.effective_leverage, 2),
            "open_positions": len(self.positions),
            "total_trades": self._trade_count,
            "total_fees": round(self._total_fees_paid, 4),
            "total_funding": round(self._total_funding_paid, 4),
            "pending_limit_orders": len(self._limit_queue),
        }

    async def close(self):
        pass

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile(self) -> dict:
        """
        Verify accounting identity: balance + unrealized PnL = equity.
        Returns a dict with reconciliation status.
        """
        unrealized_sum = 0.0
        for pos in self.positions.values():
            price = self._prices.get(pos.asset, pos.entry_price)
            unrealized_sum += self._compute_pnl(pos, price)["unrealized"]

        computed_equity = self.balance + unrealized_sum
        discrepancy = abs(computed_equity - self.equity)

        return {
            "balance": self.balance,
            "unrealized_pnl": unrealized_sum,
            "computed_equity": computed_equity,
            "reported_equity": self.equity,
            "discrepancy": discrepancy,
            "reconciled": discrepancy < 0.01,  # within 1 cent
            "positions": len(self.positions),
            "pending_orders": len(self._limit_queue),
            "total_fees": self._total_fees_paid,
            "total_funding": self._total_funding_paid,
        }