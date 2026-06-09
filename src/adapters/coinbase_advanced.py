"""
Coinbase Advanced Trade adapter — REST API for US-regulated perpetual-style futures.

API docs: https://docs.cdp.coinbase.com/advanced-trade-api/docs/welcome
Products are on FCM venue (Coinbase Financial Markets).
Perpetual-style products expire Dec 30, 2030 (4.5 year duration).
"""

import asyncio
import logging
import secrets
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
BASE_RETRY_DELAY = 1.0

# CDE (Coinbase Derivatives Exchange) product IDs for perp-style futures.
# Perpetual-style products (20DEC30 expiry) are functionally perpetuals with 4.5 year duration.
# Standard futures use the farthest available monthly expiry.
ASSET_TO_PRODUCT: dict[str, str] = {
    "BTC": "BIP-20DEC30-CDE",
    "ETH": "ETP-20DEC30-CDE",
    "SOL": "SLP-20DEC30-CDE",
    "XRP": "XPP-20DEC30-CDE",
    "DOGE": "DOP-20DEC30-CDE",
    "ADA": "ADA-31JUL26-CDE",
    "AVAX": "AVP-20DEC30-CDE",
    "LINK": "LNP-20DEC30-CDE",
    "DOT": "POP-20DEC30-CDE",
    "AAVE": "AVE-20DEC30-CDE",
    "LTC": "LCP-20DEC30-CDE",
    "NEAR": "NER-20DEC30-CDE",
    "SUI": "SUP-20DEC30-CDE",
    "BNB": "BNB-20DEC30-CDE",
    "XLM": "XLP-20DEC30-CDE",
    "HBAR": "HEP-20DEC30-CDE",
    "BCH": "BCP-20DEC30-CDE",
    "ZEC": "ZEC-20DEC30-CDE",
    "PEPE": "PEP-20DEC30-CDE",
    "SHIB": "SHB-31JUL26-CDE",
}
PRODUCT_TO_ASSET: dict[str, str] = {v: k for k, v in ASSET_TO_PRODUCT.items()}


class CoinbaseAdvancedAdapter(ExchangeAdapter):
    def __init__(
        self,
        api_key_id: str = "",
        private_key: str = "",
        portfolio_uuid: str = "",
    ):
        self._api_key_id = api_key_id
        self._private_key = private_key.replace("\\n", "\n") if private_key else private_key
        self._http = httpx.AsyncClient(timeout=60.0)
        self._running = False

        self._latest_prices: dict[str, float] = {}
        self._latest_funding: dict[str, float] = {}
        self._latest_oi: dict[str, float] = {}
        self._latest_changes_24h: dict[str, float] = {}
        self._perp_configs: dict[str, PerpConfig] = {}
        self._candle_cache: dict[str, list[PerpCandle]] = defaultdict(list)

        self._request_count: int = 0
        self._last_429_time: float = 0.0
        self._consecutive_429s: int = 0

    def _make_jwt(self, uri: str) -> str:
        now = int(time.time())
        payload = {
            "iss": "cdp",
            "sub": self._api_key_id,
            "nbf": now,
            "exp": now + 120,
            "uri": uri,
        }
        return pyjwt.encode(
            payload,
            self._private_key,
            algorithm="ES256",
            headers={
                "kid": self._api_key_id,
                "nonce": secrets.token_hex(16),
            },
        )

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
                    continue
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                delay = BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning("Coinbase request failed (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, e)
                await asyncio.sleep(delay)

        if last_exc:
            raise last_exc
        raise httpx.HTTPStatusError(f"Exhausted retries for {method} {path}", request=None, response=resp)

    def _product_id(self, asset: str) -> str:
        if asset in ASSET_TO_PRODUCT:
            return ASSET_TO_PRODUCT[asset]
        return f"{asset}-20DEC30-CDE"

    def _asset_name(self, product_id: str) -> str:
        if product_id in PRODUCT_TO_ASSET:
            return PRODUCT_TO_ASSET[product_id]
        base = product_id.split("-")[0]
        return base

    async def _fetch_all_products(self) -> list[dict]:
        path = "/products"
        params = {
            "product_type": "FUTURE",
            "contract_expiry_type": "EXPIRING",
            "limit": 250,
        }
        data = await self._request("GET", path, params)
        return data.get("products", [])

    async def connect_ws(self):
        logger.info("Coinbase WS not yet implemented — using REST polling")

    async def close(self):
        self._running = False
        await self._http.aclose()

    async def fetch_candles(
        self, asset: str, interval: str = "1h", limit: int = 200
    ) -> list[PerpCandle]:
        granularity_map = {
            "1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE",
            "30m": "THIRTY_MINUTE", "1h": "ONE_HOUR", "2h": "TWO_HOUR",
            "4h": "FOUR_HOUR", "6h": "SIX_HOUR", "1d": "ONE_DAY",
        }
        gran = granularity_map.get(interval, "ONE_HOUR")
        pid = self._product_id(asset)
        path = f"/products/{pid}/candles"
        params = {"granularity": gran, "limit": limit}
        data = await self._request("GET", path, params)
        candles = data.get("candles", [])
        result: list[PerpCandle] = []
        for c in candles:
            ts = int(c.get("start", 0))
            result.append(PerpCandle(
                open=float(c.get("open", 0)),
                high=float(c.get("high", 0)),
                low=float(c.get("low", 0)),
                close=float(c.get("close", 0)),
                volume=float(c.get("volume", 0)),
                timestamp=ts,
            ))
        self._candle_cache[asset] = result
        return result

    async def fetch_price(self, asset: str) -> Optional[float]:
        return self._latest_prices.get(asset)

    async def fetch_funding(self) -> dict[str, float]:
        products = [p for p in (await self._fetch_all_products()) if p.get("product_id", "") in PRODUCT_TO_ASSET]
        result: dict[str, float] = {}
        has_funding = 0
        for p in products:
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET[pid]
            fut = p.get("future_product_details", {}) or {}
            perp = p.get("perpetual_product_details", {}) or {}
            fr = fut.get("funding_rate", perp.get("funding_rate", 0))
            try:
                rate = float(fr)
            except (ValueError, TypeError):
                rate = 0.0
            result[asset] = rate
            self._latest_funding[asset] = rate
            if rate != 0.0:
                has_funding += 1
        logger.info("fetch_funding: %d/%d monitored assets have funding", has_funding, len(ASSET_TO_PRODUCT))
        return result

    async def fetch_all_mids(self) -> dict[str, float]:
        products = [p for p in (await self._fetch_all_products()) if p.get("product_id", "") in PRODUCT_TO_ASSET]
        result: dict[str, float] = {}
        for p in products:
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET[pid]
            price_str = p.get("price", "0")
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                price = 0.0
            if price > 0:
                result[asset] = price
                self._latest_prices[asset] = price
            change_str = p.get("price_percentage_change_24h", "0")
            try:
                change = float(change_str.replace("%", ""))
            except (ValueError, TypeError):
                change = 0.0
            self._latest_changes_24h[asset] = change
        if not result:
            logger.warning("fetch_all_mids: 0/%d monitored assets priced", len(ASSET_TO_PRODUCT))
        return result

    async def fetch_open_interest(self) -> dict[str, float]:
        products = [p for p in (await self._fetch_all_products()) if p.get("product_id", "") in PRODUCT_TO_ASSET]
        result: dict[str, float] = {}
        for p in products:
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET[pid]
            fut = p.get("future_product_details", {}) or {}
            perp = p.get("perpetual_product_details", {}) or {}
            oi = float(fut.get("open_interest", perp.get("open_interest", 0)))
            result[asset] = oi
            self._latest_oi[asset] = oi
        return result

    async def fetch_metadata(self) -> dict[str, PerpConfig]:
        if self._perp_configs:
            return self._perp_configs
        products = [p for p in (await self._fetch_all_products()) if p.get("product_id", "") in PRODUCT_TO_ASSET]
        configs: dict[str, PerpConfig] = {}
        for p in products:
            pid = p.get("product_id", "")
            asset = PRODUCT_TO_ASSET[pid]
            fut = p.get("future_product_details", {}) or {}
            perp = p.get("perpetual_product_details", {}) or {}
            perpetual_details = fut.get("perpetual_details", {}) or {}
            max_lev = float(
                perp.get("max_leverage") or
                perpetual_details.get("max_leverage") or
                10
            )
            base_inc = float(p.get("base_increment", 0.001))
            base_min = float(p.get("base_min_size", 0))
            step_size = base_inc if base_inc > 0 else 0.001
            min_size_val = base_min if base_min > 0 else step_size
            configs[asset] = PerpConfig(
                asset=asset, max_leverage=max_lev, step_size=step_size,
                min_size=min_size_val,
            )
        self._perp_configs = configs
        return configs

    async def get_funding_rate(self, asset: str) -> float:
        return self._latest_funding.get(asset, 0.0)

    async def fetch_snapshot(self, asset: str) -> MarketSnapshot:
        if not self._latest_prices:
            await self.fetch_all_mids()
        if not self._latest_funding:
            await self.fetch_funding()
        if not self._latest_oi:
            await self.fetch_open_interest()
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

    async def fetch_positions(self) -> list[Any]:
        return []

    async def place_order(self, order: Order) -> Optional[str]:
        logger.debug("Coinbase place_order: not implemented (paper mode)")
        return None

    async def cancel_order(self, asset: str, order_id: str) -> bool:
        logger.debug("Coinbase cancel_order: not implemented (paper mode)")
        return False

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def rate_limited(self) -> bool:
        return self._consecutive_429s > 0
