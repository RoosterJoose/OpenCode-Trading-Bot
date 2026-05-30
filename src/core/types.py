from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self == Side.LONG else Side.LONG


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PerpCandle(Candle):
    funding_rate: float = 0.0
    open_interest: float = 0.0


@dataclass
class Signal:
    source: str
    asset: str
    direction: Side
    confidence: float
    timestamp: datetime
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    targets: list[float] = field(default_factory=list)
    bucket: str = "unknown"
    metadata: dict = field(default_factory=dict)


@dataclass
class Order:
    asset: str
    side: Side
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    cloid: Optional[str] = None


@dataclass
class Fill:
    order_id: str
    asset: str
    side: Side
    quantity: float
    price: float
    fee: float
    fee_asset: str = "USDC"
    filled_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PerpPosition:
    asset: str
    side: Side
    entry_price: float
    size: float
    leverage: float = 1.0
    liquidation_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    entry_time: datetime = field(default_factory=datetime.utcnow)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy: str = ""
    signal_source: str = ""
    entry_confidence: float = 0.0
    entry_funding_rate: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    @property
    def notional(self) -> float:
        return self.entry_price * abs(self.size)

    @property
    def margin(self) -> float:
        return self.notional / self.leverage if self.leverage > 0 else self.notional

    @property
    def pnl_pct(self) -> float:
        if self.margin == 0:
            return 0.0
        return (self.unrealized_pnl / self.margin) * 100

    @property
    def distance_to_liquidation(self) -> float:
        if self.liquidation_price <= 0 or self.entry_price <= 0:
            return 999.0
        if self.side == Side.LONG:
            return ((self.entry_price - self.liquidation_price) / self.entry_price) * 100
        else:
            return ((self.liquidation_price - self.entry_price) / self.entry_price) * 100


@dataclass
class MarketSnapshot:
    asset: str
    price: float
    mid_price: float
    mark_price: float
    funding_rate: float
    open_interest: float
    volume_24h: float
    timestamp: int


@dataclass
class PerpConfig:
    asset: str
    max_leverage: float
    step_size: float
    min_size: float


@dataclass
class TradeRecord:
    asset: str
    side: Side
    entry_price: float
    exit_price: float
    size: float
    leverage: float
    pnl_pct: float
    pnl_dollars: float
    fees: float
    funding_paid: float
    exit_reason: str
    strategy: str
    signal_source: str
    entry_confidence: float
    entry_time: datetime
    exit_time: datetime
    regime: str = ""
    r_multiple: float = 0.0

    @property
    def r(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        if self.side == Side.LONG:
            return (self.exit_price - self.entry_price) / self.entry_price * self.leverage
        else:
            return (self.entry_price - self.exit_price) / self.entry_price * self.leverage


@dataclass
class ParameterSuggestion:
    parameter: str
    current_value: float
    suggested_value: float
    reason: str
    confidence: float
    applies_to: str = ""


class RegimeType(Enum):
    STRONGLY_MR = "strongly_mean_reverting"
    MEAN_REVERTING = "mean_reverting"
    RANDOM_WALK = "random_walk"
    TRENDING = "trending"
    STRONGLY_TRENDING = "strongly_trending"
    HIGH_VOL = "high_volatility"
    DEAD_MARKET = "dead_market"
