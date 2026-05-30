"""
Paper perp exchange — models Hyperliquid perp trading with funding, liquidation, and leverage.
No real I/O. State is persisted to SQLite for crash recovery.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from src.core.types import (
    Fill,
    Order,
    OrderType,
    PerpConfig,
    PerpPosition,
    Side,
)

logger = logging.getLogger("hermes.paper")


class PaperPerpExchange:
    """Paper exchange that models perp mechanics:
    - Leverage with liquidation price
    - 8h funding rate accrual
    - Taker/maker fees (Hyperliquid standard)
    - Position tracking
    """

    TAKER_FEE = 0.00025
    MAKER_FEE = 0.00015
    LIQUIDATION_BUFFER = 0.05

    def __init__(self, initial_balance: float = 10_000.0):
        self.balance = initial_balance
        self.peak_balance = initial_balance
        self.positions: dict[str, PerpPosition] = {}
        self._next_cloid = 0
        self._prices: dict[str, float] = {}
        self._funding_rates: dict[str, float] = {}
        self._open_interest: dict[str, float] = {}
        self._orders: dict[str, Order] = {}
        self._last_funding_time: Optional[datetime] = None
        self._perp_configs: dict[str, PerpConfig] = {}
        self._total_fees_paid = 0.0
        self._total_funding_paid = 0.0
        self._trade_count = 0

    def update_price(self, asset: str, price: float):
        self._prices[asset] = price
        self._revalue_position(asset)

    def update_funding(self, asset: str, rate: float):
        self._funding_rates[asset] = rate
        self._accrue_funding(asset)

    def update_open_interest(self, asset: str, oi: float):
        self._open_interest[asset] = oi

    def update_candle(self, asset: str, close: float, high: float = 0, low: float = 0):
        self.update_price(asset, close)

    def set_perp_config(self, asset: str, config: PerpConfig):
        self._perp_configs[asset] = config

    async def fetch_price(self, asset: str) -> float:
        return self._prices.get(asset, 0.0)

    async def fetch_position(self, asset: str) -> Optional[PerpPosition]:
        return self.positions.get(asset)

    async def fetch_balances(self) -> dict[str, float]:
        return {"USDC": self.balance}

    async def place_order(self, order: Order) -> str:
        cloid = f"paper_{self._next_cloid}"
        self._next_cloid += 1
        if order.order_type == OrderType.MARKET:
            return await self._fill_market(cloid, order)
        self._orders[cloid] = order
        return cloid

    async def _fill_market(self, cloid: str, order: Order) -> str:
        price = self._prices.get(order.asset, order.price or 0.0)
        if price <= 0:
            logger.warning("No price for %s, using 0", order.asset)
            return cloid

        config = self._perp_configs.get(order.asset)
        max_lev = config.max_leverage if config else 3.0

        fill_price = price
        fee = order.quantity * fill_price * self.TAKER_FEE

        existing = self.positions.get(order.asset)

        if existing and order.side != existing.side:
            self._close_position(order.asset, fill_price)
            existing = None

        if existing:
            total_size = existing.size + order.quantity
            avg_price = (
                (existing.entry_price * existing.size) + (fill_price * order.quantity)
            ) / total_size if total_size > 0 else fill_price
            existing.size = total_size
            existing.entry_price = avg_price
            existing.leverage = min(max_lev, existing.leverage)
            existing.liquidation_price = self._compute_liq_price(existing.asset, existing.side, avg_price, total_size, existing.leverage)
            existing.fills.append(Fill(cloid, order.asset, order.side, order.quantity, fill_price, fee))
        else:
            lev = min(max_lev, max(1.0, order.leverage or 1.0))
            liq = self._compute_liq_price(order.asset, order.side, fill_price, order.quantity, lev)
            pos = PerpPosition(
                asset=order.asset,
                side=order.side,
                entry_price=fill_price,
                size=order.quantity,
                leverage=lev,
                liquidation_price=liq,
                entry_time=datetime.now(timezone.utc),
                stop_loss=order.stop_price,
                component_sources=list(order.metadata.get("component_sources", [])),
            )
            self.positions[order.asset] = pos

        margin_req = (order.quantity * fill_price) / self._perp_configs.get(order.asset, PerpConfig("", 3, 0.001, 0.001)).max_leverage if order.asset in self._perp_configs else 0
        margin_req = max(margin_req, 0)

        self.balance -= fee
        self._total_fees_paid += fee
        self._trade_count += 1

        return cloid

    async def cancel_order(self, cloid: str) -> bool:
        return self._orders.pop(cloid, None) is not None

    def _close_position(self, asset: str, price: float) -> Optional[dict]:
        pos = self.positions.pop(asset, None)
        if pos is None:
            return None
        pnl = self._compute_pnl(pos, price)
        self.balance += pnl["realized"]
        return pnl

    def restore_positions(self, positions: list[dict]):
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
                    self.positions[pos.asset] = pos
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping invalid restored position: %s", e)

    def _revalue_position(self, asset: str):
        pos = self.positions.get(asset)
        if pos is None:
            return
        price = self._prices.get(asset, pos.entry_price)
        pos.unrealized_pnl = self._compute_pnl(pos, price)["unrealized"]

    def _compute_pnl(self, pos: PerpPosition, current_price: float) -> dict:
        if pos.side == Side.LONG:
            pnl = (current_price - pos.entry_price) * pos.size
        else:
            pnl = (pos.entry_price - current_price) * pos.size
        pnl_pct = (pnl / pos.margin * 100) if pos.margin > 0 else 0.0
        return {"unrealized": pnl, "realized": pnl, "pnl_pct": pnl_pct}

    def _compute_liq_price(self, asset: str, side: Side, entry: float, size: float, leverage: float) -> float:
        if leverage <= 0 or entry <= 0:
            return 0.0
        maintenance = 0.006
        if side == Side.LONG:
            return entry * (1 - (1 / leverage) + maintenance) / (1 - maintenance + self.LIQUIDATION_BUFFER)
        else:
            return entry * (1 + (1 / leverage) - maintenance) / (1 + maintenance - self.LIQUIDATION_BUFFER)

    def _accrue_funding(self, asset: str):
        rate = self._funding_rates.get(asset, 0.0)
        pos = self.positions.get(asset)
        if pos is None or rate == 0:
            return
        now = datetime.now(timezone.utc)
        if self._last_funding_time is None:
            self._last_funding_time = now
            return
        hours = (now - self._last_funding_time).total_seconds() / 3600
        if hours < 1:
            return
        funding_payment = pos.notional * rate * hours
        if pos.side == Side.SHORT:
            funding_payment = -funding_payment
        pos.realized_pnl -= funding_payment
        self._total_funding_paid += funding_payment

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
            self._close_position(asset, price)
            return True
        return False

    @property
    def equity(self) -> float:
        total = self.balance
        for pos in self.positions.values():
            total += pos.unrealized_pnl
        return total

    @property
    def gross_exposure(self) -> float:
        return sum(pos.notional for pos in self.positions.values())

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
        }

    async def close(self):
        pass
