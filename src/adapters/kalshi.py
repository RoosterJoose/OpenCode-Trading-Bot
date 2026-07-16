"""
Kalshi Perpetual Futures (Margin) adapter.

Uses the Kalshi Trade API v2 /margin/* namespace.
Auth: RSA-PSS SHA-256 signing (key ID + private key).
"""

import asyncio
import base64
import logging
import time
from typing import Any, Optional

import httpx

from src.adapters.base import ExchangeAdapter
from src.core.types import MarketSnapshot, Order, OrderType, PerpCandle, PerpConfig, PerpPosition, Side

logger = logging.getLogger(__name__)

ASSET_TO_TICKER: dict[str, str] = {
    "BTC": "KXBTCPERP",
    "ETH": "KXETHPERP",
    "SOL": "KXSOLPERP",
    "XRP": "KXXRPPERP",
    "DOGE": "KXDOGEPERP",
    "LINK": "KXLINKPERP",
    "LTC": "KXLTCPERP",
    "SUI": "KXSUIPERP",
    "HYPE": "KXHYPEPERP",
    "BCH": "KXBCHPERP",
    "SHIB": "KXKSHIBPERP",
}

TICKER_TO_ASSET: dict[str, str] = {v: k for k, v in ASSET_TO_TICKER.items()}

CONTRACT_SIZES: dict[str, float] = {
    "BTC": 0.0001,
    "ETH": 0.001,
    "SOL": 0.1,
    "XRP": 1.0,
    "DOGE": 100.0,
    "LINK": 1.0,
    "LTC": 0.1,
    "SUI": 10.0,
    "HYPE": 0.1,
    "BCH": 0.01,
    "SHIB": 1000.0,
}


class KalshiAdapter(ExchangeAdapter):
    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        base_url: str = "https://external-api.kalshi.com",
    ):
        self._api_key_id = api_key_id.strip()
        self._base_url = base_url.rstrip("/")
        self._api_prefix = "/trade-api/v2"

        pem = private_key_pem.strip()
        if "\\n" in pem:
            pem = pem.replace("\\n", "\n")
        self._private_key_pem = pem

        self._http = httpx.AsyncClient(timeout=30.0)
        self._latest_prices: dict[str, float] = {}
        self._latest_funding: dict[str, float] = {}
        self._latest_oi: dict[str, float] = {}
        self._perp_configs: dict[str, PerpConfig] = {}

        from cryptography.hazmat.primitives import serialization
        self._private_key = serialization.load_pem_private_key(
            self._private_key_pem.encode("utf-8"),
            password=None,
        )

    def _contract_to_asset_price(self, asset: str, contract_price: float) -> float:
        size = CONTRACT_SIZES.get(asset, 1.0)
        return contract_price / size if size > 0 else contract_price

    def _sign(self, method: str, path: str, timestamp_ms: int) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        path_clean = path.split("?")[0]
        msg = f"{timestamp_ms}{method}{path_clean}"
        signature = self._private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        full_path = self._api_prefix + path
        ts = int(time.time() * 1000)
        sig = self._sign(method, full_path, ts)
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        auth: bool = False,
    ) -> Any:
        url = f"{self._base_url}{self._api_prefix}{path}"
        headers = {"Content-Type": "application/json"}
        if auth:
            headers.update(self._auth_headers(method, path))
        for attempt in range(3):
            try:
                resp = await self._http.request(method, url, params=params, json=body, headers=headers)
                if resp.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error("Kalshi %s %s: %d %s", method, path, e.response.status_code, e.response.text[:200])
                return {}
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {}
        return {}

    async def _fetch_markets_bulk(self) -> list[dict]:
        """Fetch ALL active markets in one call."""
        data = await self._request("GET", "/margin/markets", params={"status": "active"})
        return data.get("markets", [])

    async def fetch_all_mids(self) -> dict[str, float]:
        markets = await self._fetch_markets_bulk()
        result: dict[str, float] = {}
        for m in markets:
            ticker = m.get("ticker", "")
            asset = TICKER_TO_ASSET.get(ticker)
            if not asset:
                continue
            price_str = m.get("price", "0")
            try:
                contract_price = float(price_str)
            except (ValueError, TypeError):
                contract_price = 0.0
            if contract_price > 0:
                asset_price = self._contract_to_asset_price(asset, contract_price)
                result[asset] = asset_price
                self._latest_prices[asset] = asset_price
        return result

    async def fetch_candles(
        self, asset: str, interval: str = "1h", limit: int = 200
    ) -> list[PerpCandle]:
        ticker = ASSET_TO_TICKER.get(asset)
        if not ticker:
            return []
        period_map = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
        period = period_map.get(interval, 60)
        now_ts = int(time.time())
        start_ts = now_ts - (limit * period * 60) - 120
        data = await self._request(
            "GET",
            f"/margin/markets/{ticker}/candlesticks",
            params={
                "start_ts": str(start_ts),
                "end_ts": str(now_ts),
                "period_interval": str(period),
            },
        )
        raw = data.get("candlesticks", [])
        result: list[PerpCandle] = []
        for c in raw:
            price = c.get("price", {})
            try:
                close_str = price.get("close", "0")
                if close_str is None:
                    continue
                close_val = float(close_str)
            except (ValueError, TypeError):
                continue
            try:
                open_val = float(price.get("open", close_str))
                high_val = float(price.get("high", close_str))
                low_val = float(price.get("low", close_str))
            except (ValueError, TypeError):
                continue
            ts = int(c.get("end_period_ts", 0))
            if not ts:
                continue
            volume = float(c.get("volume", "0"))
            result.append(PerpCandle(
                open=self._contract_to_asset_price(asset, open_val),
                high=self._contract_to_asset_price(asset, high_val),
                low=self._contract_to_asset_price(asset, low_val),
                close=self._contract_to_asset_price(asset, close_val),
                volume=volume,
                timestamp=ts,
            ))
        return result[-limit:]

    async def fetch_funding(self) -> dict[str, float]:
        async def _funding_for(asset: str, ticker: str) -> tuple[str, float]:
            data = await self._request(
                "GET", "/margin/funding_rates/estimate",
                params={"ticker": ticker},
            )
            rate_str = data.get("funding_rate", "0")
            try:
                return asset, float(rate_str)
            except (ValueError, TypeError):
                return asset, 0.0
        tasks = [asyncio.create_task(_funding_for(a, t)) for a, t in ASSET_TO_TICKER.items()]
        for task in asyncio.as_completed(tasks):
            asset, rate = await task
            self._latest_funding[asset] = rate
        return dict(self._latest_funding)

    async def fetch_open_interest(self) -> dict[str, float]:
        markets = await self._fetch_markets_bulk()
        result: dict[str, float] = {}
        for m in markets:
            ticker = m.get("ticker", "")
            asset = TICKER_TO_ASSET.get(ticker)
            if not asset:
                continue
            oi_str = m.get("open_interest", "0")
            try:
                oi = float(oi_str)
            except (ValueError, TypeError):
                oi = 0.0
            self._latest_oi[asset] = oi
            result[asset] = oi
        return result

    async def fetch_metadata(self) -> dict[str, PerpConfig]:
        if self._perp_configs:
            return self._perp_configs
        markets = await self._fetch_markets_bulk()
        configs: dict[str, PerpConfig] = {}
        for m in markets:
            ticker = m.get("ticker", "")
            asset = TICKER_TO_ASSET.get(ticker)
            if not asset:
                continue
            lev = m.get("leverage_estimate", 5) or 5
            configs[asset] = PerpConfig(
                asset=asset,
                max_leverage=float(lev),
                step_size=0.0001,
                min_size=0.0001,
            )
        self._perp_configs = configs
        return configs

    async def get_funding_rate(self, asset: str) -> float:
        return self._latest_funding.get(asset, 0.0)
    async def get_spread(self, asset: str) -> float:
        return 0.001

    async def fetch_price(self, asset: str) -> float:
        return self._latest_prices.get(asset, 0.0)

    async def fetch_snapshot(self, asset: str) -> MarketSnapshot:
        price = await self.fetch_price(asset)
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
        data = await self._request("GET", "/margin/positions", auth=True)
        raw = data.get("positions", [])
        positions: list[PerpPosition] = []
        for p in raw:
            ticker = p.get("ticker", "")
            asset = TICKER_TO_ASSET.get(ticker)
            if not asset:
                continue
            amount = float(p.get("position_amount", "0"))
            entry = float(p.get("entry_price", "0"))
            mark = float(p.get("mark_price", "0"))
            positions.append(PerpPosition(
                asset=asset,
                side=Side.LONG if amount > 0 else Side.SHORT,
                size=abs(amount),
                entry_price=self._contract_to_asset_price(asset, entry) if entry else 0,
                mark_price=self._contract_to_asset_price(asset, mark) if mark else 0,
                leverage=float(p.get("leverage", 1)),
                unrealized_pnl=float(p.get("unrealized_pnl", "0")),
            ))
        return positions

    async def place_order(self, order: Order) -> Optional[str]:
        ticker = ASSET_TO_TICKER.get(order.asset)
        if not ticker:
            return None
        side = "ask" if order.side == Side.SHORT else "bid"
        import uuid
        cloid = order.cloid or str(uuid.uuid4())
        body = {
            "ticker": ticker,
            "type": "market" if order.order_type == OrderType.MARKET else "limit",
            "side": side,
            "count": f"{order.quantity:.2f}",
            "client_order_id": cloid,
            "time_in_force": "immediate_or_cancel" if order.order_type == OrderType.MARKET else "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": order.reduce_only,
        }
        if order.order_type == OrderType.LIMIT and order.price:
            body["price"] = f"{order.price:.6f}"
        else:
            body["price"] = "0.0001"
        data = await self._request("POST", "/margin/orders", body=body, auth=True)
        return data.get("order_id")

    async def cancel_order(self, cloid: str) -> bool:
        data = await self._request("DELETE", f"/margin/orders/{cloid}", auth=True)
        return bool(data)

    async def connect_ws(self):
        logger.info("Kalshi WebSocket not yet implemented")

    async def close(self):
        await self._http.aclose()
