"""
Phase 1.4: InstrumentSpec — models CDE whole-contract multipliers and dynamic margin.

CDE futures trade in whole contracts with product-specific multipliers:
  BIP = 0.01 BTC per contract
  ETP = 0.1 ETH per contract
  SLP = 5 SOL per contract
  XPP = 500 XRP per contract
  PEP = 100000 PEPE per contract

This module provides:
- InstrumentSpec dataclass capturing all contract economics
- contract_count_from_notional: converts dollar notional → integer contracts
- underlying_from_contracts: converts contracts → underlying exposure
- margin_from_contracts: computes initial/maintenance margin using dynamic rates
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("hermes.instrument")


@dataclass
class MarginRates:
    """Dynamic margin rates from Coinbase CDE."""
    long_initial: float = 0.10        # 10% initial = 10x leverage
    long_maintenance: float = 0.04     # 4% maintenance
    short_initial: float = 0.10
    short_maintenance: float = 0.04
    intraday_long_initial: float = 0.05   # 5% intraday
    intraday_short_initial: float = 0.05
    overnight_long_initial: float = 0.10
    overnight_short_initial: float = 0.10
    retrieved_at: Optional[datetime] = None


@dataclass
class InstrumentSpec:
    """
    Full specification of a Coinbase tradable instrument.

    For CDE futures:
      - contract_size: underlying units per contract (e.g., 0.01 BTC for BIP)
      - order quantity is in INTEGER contract counts
      - underlying_exposure = contract_count * contract_size
      - notional = contract_count * contract_size * price

    For spot:
      - contract_size = 1.0 (fractional units)
      - order quantity is in fractional base units
    """
    product_id: str
    asset: str                 # canonical asset symbol (BTC, ETH, SOL, etc.)
    venue: str                 # "CBE" (spot), "FCM" (CDE futures), "INTX" (international perps)
    product_type: str          # "SPOT", "FUTURE", "PERPETUAL"
    contract_size: float = 1.0  # underlying units per contract
    is_whole_contract: bool = False  # True for CDE futures
    price_increment: float = 0.01
    size_increment: float = 0.001
    min_size: float = 0.001
    max_size: float = float("inf")
    quote_currency: str = "USD"
    funding_interval_hours: int = 1  # CDE = hourly
    margin: MarginRates = field(default_factory=MarginRates)
    status: str = "online"     # online, offline, limit_only, cancel_only, etc.
    retrieved_at: Optional[datetime] = None

    def contract_count_from_notional(self, notional: float, price: float) -> int:
        """
        Convert dollar notional → integer contract count.

        notional / price = underlying units
        underlying_units / contract_size = contract count (rounded down)

        For spot (is_whole_contract=False), returns 0 and caller should use
        underlying_quantity_from_notional instead.
        """
        if price <= 0 or self.contract_size <= 0:
            return 0
        underlying = notional / price
        if self.is_whole_contract:
            return int(underlying / self.contract_size)
        return 0  # Spot uses fractional quantities

    def underlying_quantity_from_notional(self, notional: float, price: float) -> float:
        """For spot: convert notional → fractional underlying units."""
        if price <= 0:
            return 0.0
        return notional / price

    def contracts_to_notional(self, contract_count: int, price: float) -> float:
        """Convert contract count → dollar notional."""
        return contract_count * self.contract_size * price

    def contracts_to_underlying(self, contract_count: int) -> float:
        """Convert contract count → underlying exposure."""
        return contract_count * self.contract_size

    def initial_margin(self, contract_count: int, price: float, side: str = "long") -> float:
        """
        Compute initial margin for a position.

        margin = notional * initial_margin_rate
        Uses overnight rates (conservative) by default.
        """
        notional = self.contracts_to_notional(contract_count, price)
        rate = self.margin.overnight_long_initial if side == "long" else self.margin.overnight_short_initial
        return notional * rate

    def maintenance_margin(self, contract_count: int, price: float, side: str = "long") -> float:
        """Compute maintenance margin."""
        notional = self.contracts_to_notional(contract_count, price)
        rate = self.margin.long_maintenance if side == "long" else self.margin.short_maintenance
        return notional * rate

    def liquidation_price(self, entry_price: float, contract_count: int, side: str = "long") -> float:
        """
        Compute approximate liquidation price using maintenance margin rate.

        For longs: liq = entry * (1 - (1/leverage) + maintenance)
        For shorts: liq = entry * (1 + (1/leverage) - maintenance)
        """
        notional = self.contracts_to_notional(contract_count, entry_price)
        initial = self.initial_margin(contract_count, entry_price, side)
        if initial <= 0 or notional <= 0:
            return 0.0
        leverage = notional / initial
        maintenance = self.margin.long_maintenance if side == "long" else self.margin.short_maintenance

        if side == "long":
            return entry_price * (1 - (1 / leverage) + maintenance) / (1 - maintenance + 0.05)
        else:
            return entry_price * (1 + (1 / leverage) - maintenance) / (1 + maintenance - 0.05)

    def round_to_increment(self, quantity: float) -> float:
        """Round quantity to the size increment."""
        import math
        return math.floor(quantity / self.size_increment) * self.size_increment

    def is_tradable(self) -> bool:
        """Check if instrument is currently tradable."""
        return self.status in ("online",)

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "asset": self.asset,
            "venue": self.venue,
            "product_type": self.product_type,
            "contract_size": self.contract_size,
            "is_whole_contract": self.is_whole_contract,
            "price_increment": self.price_increment,
            "size_increment": self.size_increment,
            "min_size": self.min_size,
            "status": self.status,
            "funding_interval_hours": self.funding_interval_hours,
        }


# Known CDE contract sizes keyed by CANONICAL asset symbol
# (from Coinbase public catalog 2026-07-15)
CDE_CONTRACT_SIZES = {
    "BTC": 0.01,      # BIP: 0.01 BTC per contract
    "ETH": 0.1,       # ETP: 0.1 ETH per contract
    "SOL": 5.0,       # SLP: 5 SOL per contract
    "XRP": 500.0,     # XPP: 500 XRP per contract
    "PEPE": 100000.0, # PEP: 100000 PEPE per contract
    "DOGE": 1000.0,   # DGP: 1000 DOGE per contract
    "ADA": 100.0,     # ADP: 100 ADA per contract
    "AVAX": 1.0,      # AVP: 1 AVAX per contract
    "LINK": 1.0,      # LIP: 1 LINK per contract
    "LTC": 1.0,       # LCP: 1 LTC per contract
    "BCH": 0.1,       # BCP: 0.1 BCH per contract
    "SHIB": 100000.0, # SHP: 100000 SHIB per contract
    "NEAR": 1.0,      # NEP: 1 NEAR per contract
    "SUI": 1.0,       # 1 SUI per contract
    "APT": 1.0,       # 1 APT per contract
    "HYPE": 1.0,      # 1 HYPE per contract
    "ENA": 1000.0,    # 1000 ENA per contract
    "ONDO": 10.0,     # 10 ONDO per contract
    "ARB": 1.0,       # 1 ARB per contract
    "OP": 1.0,        # 1 OP per contract
    "AAVE": 0.1,      # 0.1 AAVE per contract
    "INJ": 1.0,       # 1 INJ per contract
    "ATOM": 1.0,      # 1 ATOM per contract
}


def build_cde_spec(asset: str, product_id: str, price_increment: float = 0.01,
                   size_increment: float = 1.0, min_size: float = 1.0) -> InstrumentSpec:
    """Build an InstrumentSpec for a CDE futures product."""
    contract_size = CDE_CONTRACT_SIZES.get(asset, 1.0)
    return InstrumentSpec(
        product_id=product_id,
        asset=asset,
        venue="FCM",
        product_type="FUTURE",
        contract_size=contract_size,
        is_whole_contract=True,
        price_increment=price_increment,
        size_increment=size_increment,
        min_size=max(min_size, 1.0),  # min 1 contract for CDE
        margin=MarginRates(),
        retrieved_at=datetime.now(timezone.utc),
    )


def build_spot_spec(asset: str, product_id: str, price_increment: float = 0.01,
                     size_increment: float = 0.001, min_size: float = 0.001) -> InstrumentSpec:
    """Build an InstrumentSpec for a spot product."""
    return InstrumentSpec(
        product_id=product_id,
        asset=asset,
        venue="CBE",
        product_type="SPOT",
        contract_size=1.0,
        is_whole_contract=False,
        price_increment=price_increment,
        size_increment=size_increment,
        min_size=min_size,
        funding_interval_hours=0,  # no funding for spot
        retrieved_at=datetime.now(timezone.utc),
    )


def size_position_with_contracts(
    spec: InstrumentSpec,
    risk_dollars: float,
    stop_distance_pct: float,
    current_price: float,
) -> tuple[int, float]:
    """
    Compute position size respecting whole-contract granularity.

    For CDE futures:
      1. Compute desired notional: risk_dollars / (stop_distance_pct / 100)
      2. Convert to underlying: notional / price
      3. Convert to contracts: underlying / contract_size (floor to int)
      4. Verify: contract_count * contract_size * stop_pct * price <= risk_dollars

    For spot:
      Returns (0, underlying_quantity) since spot uses fractional units.

    Returns (contract_count, actual_risk_dollars)
    """
    if stop_distance_pct <= 0 or current_price <= 0:
        return (0, 0.0)

    max_notional = risk_dollars / (stop_distance_pct / 100)

    if spec.is_whole_contract:
        underlying = max_notional / current_price
        contracts = int(underlying / spec.contract_size)
        if contracts < spec.min_size:
            # One contract might exceed risk budget
            single_contract_notional = spec.contract_size * current_price
            single_contract_risk = single_contract_notional * (stop_distance_pct / 100)
            if single_contract_risk > risk_dollars * 1.5:
                logger.warning(
                    "CONTRACT_TOO_LARGE %s: 1 contract = %.4f underlying, risk=%.2f > budget=%.2f",
                    spec.asset, spec.contract_size, single_contract_risk, risk_dollars
                )
                return (0, 0.0)
            # Minimum 1 contract
            contracts = max(contracts, 1)

        actual_notional = contracts * spec.contract_size * current_price
        actual_risk = actual_notional * (stop_distance_pct / 100)
        return (contracts, actual_risk)
    else:
        # Spot: fractional
        qty = max_notional / current_price
        qty = spec.round_to_increment(qty)
        actual_risk = qty * current_price * (stop_distance_pct / 100)
        return (0, actual_risk)  # contract_count=0 for spot, caller uses qty separately