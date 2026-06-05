"""
Coinbase Advanced Trade adapter — REST API for perpetual futures.

API docs: https://docs.cdp.coinbase.com/advanced-trade-api/docs/welcome
Products are on INTX venue with PERPETUAL expiry type.
"""

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Optional

import httpx
import jwt as pyjwt

from src.adapters.base import ExchangeAdapter
from src.core.types import MarketSnapshot, Order, OrderType, PerpCandle, PerpConfig, PerpPosition, Side

logger = logging.getLogger("hermes.coinbase")

API_BASE = "https://api.coinbase.com"
API_PREFIX = "/api/v3/brokerage"

MAX_RETRIES = 3
BASE_RETRY_DELAY = 1.0  # seconds

ASSET_TO_PRODUCT: dict[str, str] = {
    "BTC": "BTC-PERP-INTX",
    "ETH": "ETH-PERP-INTX",
    "SOL": "SOL-PERP-INTX",
    "BNB": "BNB-PERP-INTX",
    "XRP": "XRP-PERP-INTX",
    "DOGE": "DOGE-PERP-INTX",
    "ADA": "ADA-PERP-INTX",
    "AVAX": "AVAX-PERP-INTX",
    "LINK": "LINK-PERP-INTX",
    "AAVE": "AAVE-PERP-INTX",
    "FIL": "FIL-PERP-INTX",
    "INJ": "INJ-PERP-INTX",
    "LTC": "LTC-PERP-INTX",
    "NEAR": "NEAR-PERP-INTX",
    "SUI": "SUI-PERP-INTX",
}

PRODUCT_TO_ASSET = {v: k for k, v in ASSET_TO_PRODUCT.items()}


class CoinbaseAdvancedAdapter(ExchangeAdapter):
    def __init__(
        self,
        api_key_id: str = "",
        private_key: str = "",
        portfolio_uuid: str = "",
    ):
        self.api_key_id = api_key_id
        self.private_key = private_key
        self.portfolio_uuid = portfolio_uuid

        self._http = httpx.AsyncClient(timeout=30.0)
        self._running = False

        self._latest_prices: dict[str, float] = {}
        self._latest_funding: dict[str, float] = {}
        self._latest_oi: dict[str, float] = {}
        self._perp_configs: dict[str, PerpConfig] = {}
        self._candle_cache: dict[str, list[PerpCandle]] = defaultdict(list)

        # Rate limit tracking
        self._request_count: int = 0
        self._last_429_time: float = 0.0
        self._consecutive_429s: int = 0

    # ── JWT helper ────────────────────────────────────────────────────────

    def _make_jwt(self, uri: str) -> str:
        now = int(time.time())
        payload = {
            "iss": "cdp",
            "sub": self.api_key_id,
            "aud": ["cdp_service"],
            "nbf": now,
            "exp": now + 120,
            "uri": uri,
        }
        header = {"kid": self.api_key_id, "nonce": str(now)}
        key = self.private_key.replace("\\n", "\n")
        return pyjwt.encode(payload, key, algorithm="ES256", headers=header)

    async def _request(self, method: str, path: str, params: dict | None = None) -> Any:
        full_path = f"{API_PREFIX}{path}"
        uri = f"{method} api.coinbase.com{full_path}"
        token = self._make_jwt(uri)
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{API_BASE}{API_PREFIX}{path}"

        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._http.request(method, url, headers=headers, params=params)
                self._request_count += 1
                if resp.status_code == 429:
                    self._consecutive_429s += 1
                    self._last_429_time = time.time()
                    delay = BASE_RETRY_DELAY * (2 ** attempt) * min(self._consecutive_429s, 5)
                    logger.warning("Coinbase 429 (attempt %d/%d): retrying in %.1fs", attempt + 1, MAX_RETRIES, delay)
                    await asyncio.sleep(delay)
                    continue
                self._consecutive_429s = 0
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    continue  # handled above
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                delay = BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning("Coinbase request failed (attempt %d/%d): %s — retrying in %.1fs", attempt + 1, MAX_RETRIES, e, delay)
                await asyncio.sleep(delay)

        if last_exc:
            raise last_exc
        raise httpx.HTTPStatusError(f"Exhausted retries for {method} {path}", request=None, response=resp)

    def _product_id(self, asset: str) -> str:
        return ASSET_TO_PRODUCT.get(asset, f"{asset}-PERP-INTX")

    # ── Data fetching ─────────────────────────────────────────────────────

    async def _fetch_all_products(self) -> list[dict]:
        path = "/products"
        params = {
            "product_type": "FUTURE",
            "contract_expiry_type": "PERPETUAL",
            "limit": 100,
        }
        data = await self._request("GET", path, params)
        return data.get("products", [])

    async def connect_ws(self):
        logger.info("Coinbase WS not yet implemented — using REST polling")

    async def close(self):
        self._running = False
        await self._http.aclose()

    async def fetch_all_mids(self) -> dict[str, float]:
        products = await self._fetch_all_products()
        result: dict[str, float] = {}
        for p in products:
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET.get(pid)
            if not asset:
                continue
            price = float(p.get("price", 0))
            result[asset] = price
            self._latest_prices[asset] = price
        return result

    async def fetch_candles(
        self, asset: str, interval: str = "1h", limit: int = 200
    ) -> list[PerpCandle]:
        granularity_map = {
            "1m": "ONE_MINUTE",
            "5m": "FIVE_MINUTE",
            "15m": "FIFTEEN_MINUTE",
            "30m": "THIRTY_MINUTE",
            "1h": "ONE_HOUR",
            "2h": "TWO_HOUR",
            "4h": "FOUR_HOUR",
            "6h": "SIX_HOUR",
            "1d": "ONE_DAY",
        }
        gran = granularity_map.get(interval, "ONE_HOUR")
        interval_seconds = {
            "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400,
        }.get(interval, 3600)
        now_ts = int(time.time())
        product_id = self._product_id(asset)
        path = f"/products/{product_id}/candles"
        params = {
            "start": str(now_ts - limit * interval_seconds),
            "end": str(now_ts),
            "granularity": gran,
            "limit": min(limit, 350),
        }
        data = await self._request("GET", path, params)
        candles = []
        for raw in data.get("candles", []):
            candles.append(PerpCandle(
                timestamp=int(raw["start"]),
                open=float(raw["open"]),
                high=float(raw["high"]),
                low=float(raw["low"]),
                close=float(raw["close"]),
                volume=float(raw["volume"]),
            ))
        if candles:
            self._candle_cache[asset] = candles
        return candles

    async def fetch_funding(self) -> dict[str, float]:
        products = await self._fetch_all_products()
        result: dict[str, float] = {}
        for p in products:
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET.get(pid)
            if not asset:
                continue
            fpd = p.get("future_product_details", {})
            pd = fpd.get("perpetual_details", {}) or {}
            rate_str = fpd.get("funding_rate", pd.get("funding_rate", "0"))
            rate = float(rate_str) if rate_str else 0.0
            result[asset] = rate
            self._latest_funding[asset] = rate
        return result

    async def fetch_open_interest(self) -> dict[str, float]:
        products = await self._fetch_all_products()
        result: dict[str, float] = {}
        for p in products:
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET.get(pid)
            if not asset:
                continue
            fpd = p.get("future_product_details", {})
            pd = fpd.get("perpetual_details", {}) or {}
            oi_str = fpd.get("open_interest", pd.get("open_interest", "0"))
            oi = float(oi_str) if oi_str else 0.0
            result[asset] = oi
            self._latest_oi[asset] = oi
        return result

    async def fetch_metadata(self) -> dict[str, PerpConfig]:
        products = await self._fetch_all_products()
        configs: dict[str, PerpConfig] = {}
        for p in products:
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET.get(pid)
            if not asset:
                continue
            fpd = p.get("future_product_details", {})
            pd = fpd.get("perpetual_details", {}) or {}
            max_lev = float(pd.get("max_leverage", 3))
            base_inc = float(p.get("base_increment", "0.001"))
            base_min = float(p.get("base_min_size", "0.001"))
            configs[asset] = PerpConfig(
                asset=asset,
                max_leverage=max_lev,
                step_size=base_inc,
                min_size=base_min,
            )
        self._perp_configs = configs
        return configs

    async def get_funding_rate(self, asset: str) -> float:
        return self._latest_funding.get(asset, 0.0)

    async def fetch_price(self, asset: str) -> float:
        if self._latest_prices:
            return self._latest_prices.get(asset, 0.0)
        await self.fetch_all_mids()
        return self._latest_prices.get(asset, 0.0)

    async def fetch_snapshot(self, asset: str) -> MarketSnapshot:
        await asyncio.gather(
            self.fetch_all_mids(),
            self.fetch_funding(),
            self.fetch_open_interest(),
            return_exceptions=True,
        )
        price = self._latest_prices.get(asset, 0.0)
        return MarketSnapshot(
            asset=asset,
            price=price,
            mid_price=price,
            mark_price=price,
            funding_rate=self._latest_funding.get(asset, 0.0),
            open_interest=self._latest_oi.get(asset, 0.0),
            volume_24h=0.0,
            timestamp=int(time.time()),
        )

    async def fetch_positions(self) -> list[PerpPosition]:
        if not self.portfolio_uuid:
            return []
        path = f"/intx/positions/{self.portfolio_uuid}"
        try:
            data = await self._request("GET", path)
        except Exception as e:
            logger.warning("Failed to fetch positions: %s", e)
            return []
        positions = []
        for p in data.get("positions", []):
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET.get(pid)
            if not asset:
                continue
            net_str = p.get("net_size", "0")
            net = float(net_str)
            if net == 0:
                continue
            side = Side.LONG if net > 0 else Side.SHORT
            vwap_str = p.get("entry_vwap", {}).get("value", "0")
            upnl_str = p.get("unrealized_pnl", {}).get("value", "0")
            liq_str = p.get("liquidation_price", {}).get("value", "0")
            lev_str = p.get("leverage", "1")
            positions.append(PerpPosition(
                asset=asset,
                side=side,
                entry_price=float(vwap_str),
                size=abs(net),
                leverage=float(lev_str),
                liquidation_price=float(liq_str),
                unrealized_pnl=float(upnl_str),
            ))
        return positions

    async def place_order(self, order: Order) -> Optional[str]:
        if not self.api_key_id:
            return None
        product_id = self._product_id(order.asset)
        side = "BUY" if order.side == Side.LONG else "SELL"
        base_size = str(order.quantity)
        body = {
            "client_order_id": order.cloid or f"hermes_{int(time.time()*1000)}",
            "product_id": product_id,
            "side": side,
            "order_configuration": {
                "market_market_ioc": {
                    "base_size": base_size,
                },
            },
        }
        if order.leverage != 1.0:
            body["leverage"] = str(order.leverage)
            body["margin_type"] = "CROSS"

        if order.order_type == OrderType.LIMIT and order.price:
            body["order_configuration"] = {
                "limit_limit_gtc": {
                    "base_size": base_size,
                    "limit_price": str(order.price),
                },
            }

        path = "/orders"
        token = self._make_jwt(f"POST api.coinbase.com{API_PREFIX}{path}")
        url = f"{API_BASE}{API_PREFIX}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._http.post(url, json=body, headers=headers)
            data = resp.json()
            if data.get("success"):
                sr = data.get("success_response", {})
                return sr.get("order_id")
            logger.warning("Order failed: %s", data.get("error_response", {}).get("error", "unknown"))
        except Exception as e:
            logger.warning("Order placement error: %s", e)
        return None

    async def cancel_order(self, cloid: str) -> bool:
        if not self.api_key_id:
            return False
        path = "/orders/batch_cancel"
        token = self._make_jwt(f"POST api.coinbase.com{API_PREFIX}{path}")
        url = f"{API_BASE}{API_PREFIX}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._http.post(url, json={"order_ids": [cloid]}, headers=headers)
            return resp.status_code == 200
        except Exception:
            return False

    # ── Rate limit stats ─────────────────────────────────────────────

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def rate_limited(self) -> bool:
        return self._consecutive_429s > 0
