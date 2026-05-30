"""
Hyperliquid adapter — WebSocket + REST for perp data and execution.

API: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
Info endpoint (read): POST https://api.hyperliquid.xyz/info
Exchange endpoint (write): POST https://api.hyperliquid.xyz/exchange
WebSocket: wss://api.hyperliquid.xyz/ws
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

import httpx

from src.core.types import (
    Candle,
    MarketSnapshot,
    Order,
    OrderType,
    PerpCandle,
    PerpConfig,
    PerpPosition,
    Side,
)

logger = logging.getLogger("hermes.hyperliquid")

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_EXCHANGE = "https://api.hyperliquid.xyz/exchange"
HL_WS = "wss://api.hyperliquid.xyz/ws"


class HyperliquidAdapter:
    def __init__(
        self,
        wallet_address: str = "",
        private_key: str = "",
        testnet: bool = False,
        ws_reconnect_delay: float = 5.0,
    ):
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.testnet = testnet
        self._http = httpx.AsyncClient(timeout=30.0)
        self._running = False

        self._latest_mids: dict[str, float] = {}
        self._latest_snapshots: dict[str, dict] = {}
        self._latest_funding: dict[str, float] = {}
        self._latest_oi: dict[str, float] = {}
        self._perp_configs: dict[str, PerpConfig] = {}
        self._position_cache: dict[str, PerpPosition] = {}
        self._candle_cache: dict[str, list[PerpCandle]] = defaultdict(list)
        self._callbacks: list[Callable] = []

        self._ws: Optional[Any] = None
        self._ws_reconnect_delay = ws_reconnect_delay

    # ── Public data (REST) ──────────────────────────────────────────────

    async def _info(self, payload: dict) -> Any:
        resp = await self._http.post(HL_INFO, json=payload)
        resp.raise_for_status()
        return resp.json()

    async def fetch_all_mids(self) -> dict[str, float]:
        data = await self._info({"type": "allMids"})
        self._latest_mids = {k: float(v) for k, v in (data.get("mids", data) if isinstance(data, dict) else data).items()}
        return self._latest_mids

    async def fetch_asset_contexts(self) -> dict[str, dict]:
        data = await self._info({"type": "metaAndAssetCtxs"})
        if not isinstance(data, list) or len(data) != 2:
            return {}
        meta, ctxs = data
        result = {}
        for coin, ctx in zip(meta.get("universe", []), ctxs):
            name = coin.get("name", "")
            if name:
                result[name] = ctx
        return result

    async def fetch_candles(
        self, asset: str, interval: str = "1h", limit: int = 200
    ) -> list[PerpCandle]:
        now_ms = int(time.time() * 1000)
        # Approximate candle durations in ms for startTime calculation
        interval_ms = {
            "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
            "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
            "4h": 14_400_000, "8h": 28_800_000, "12h": 57_600_000,
            "1d": 864_000_00, "3d": 259_200_000, "1w": 604_800_000, "1M": 2_592_000_000,
        }.get(interval, 3_600_000)
        start_ms = now_ms - (limit * interval_ms)

        data = await self._info({
            "type": "candleSnapshot",
            "req": {
                "coin": asset,
                "interval": interval,
                "startTime": start_ms,
                "endTime": now_ms,
            },
        })
        candles = []
        for raw in data if isinstance(data, list) else data.get("candles", []):
            candles.append(PerpCandle(
                timestamp=int(raw["t"]),
                open=float(raw["o"]),
                high=float(raw["h"]),
                low=float(raw["l"]),
                close=float(raw["c"]),
                volume=float(raw["v"]),
            ))
        if candles:
            self._candle_cache[asset] = candles
        return candles

    async def fetch_funding(self, asset: Optional[str] = None) -> dict[str, float]:
        ctxs = await self.fetch_asset_contexts()
        result = {coin: float(ctx.get("funding") or 0.0) for coin, ctx in ctxs.items()}
        if asset:
            self._latest_funding[asset] = result.get(asset, 0.0)
            return {asset: self._latest_funding[asset]}
        self._latest_funding.update(result)
        return result

    async def fetch_open_interest(self, asset: Optional[str] = None) -> dict[str, float]:
        ctxs = await self.fetch_asset_contexts()
        result = {coin: float(ctx.get("openInterest") or 0.0) for coin, ctx in ctxs.items()}
        if asset:
            self._latest_oi[asset] = result.get(asset, 0.0)
            return {asset: self._latest_oi[asset]}
        self._latest_oi.update(result)
        return result

    async def fetch_metadata(self) -> dict[str, PerpConfig]:
        data = await self._info({"type": "meta"})
        configs = {}
        for asset_data in data.get("universe", data if isinstance(data, list) else []):
            name = asset_data.get("name", asset_data.get("coin", ""))
            configs[name] = PerpConfig(
                asset=name,
                max_leverage=float(asset_data.get("maxLeverage", 3)),
                step_size=float(asset_data.get("szDecimals", asset_data.get("stepSize", 0.001))),
                min_size=float(asset_data.get("minSize", 0.001)),
            )
        self._perp_configs = configs
        return configs

    async def fetch_snapshot(self, asset: str) -> MarketSnapshot:
        await asyncio.gather(
            self.fetch_all_mids(),
            self.fetch_funding(asset),
            self.fetch_open_interest(asset),
            return_exceptions=True,
        )
        price = self._latest_mids.get(asset, 0.0)
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

    # ── Account data (requires auth) ───────────────────────────────────

    async def fetch_positions(self) -> list[PerpPosition]:
        if not self.wallet_address:
            return list(self._position_cache.values())
        data = await self._info({
            "type": "clearinghouseState",
            "user": self.wallet_address,
        })
        positions = []
        for pos in data.get("assetPositions", []):
            p = pos.get("position", pos)
            coin = p.get("coin", "")
            sz = float(p.get("szi", 0))
            if sz == 0:
                continue
            entry_px = float(p.get("entryPx", 0))
            liq_px = float(p.get("liquidationPx", 0))
            upnl = float(p.get("unrealizedPnl", 0))
            rpnl = float(p.get("realizedPnl", 0))
            leverage = float(p.get("leverage", {}).get("value", 1))
            side = Side.LONG if sz > 0 else Side.SHORT
            positions.append(PerpPosition(
                asset=coin,
                side=side,
                entry_price=entry_px,
                size=abs(sz),
                leverage=leverage,
                liquidation_price=liq_px,
                unrealized_pnl=upnl,
                realized_pnl=rpnl,
            ))
        for p in positions:
            self._position_cache[p.asset] = p
        for key in list(self._position_cache.keys()):
            if not any(p.asset == key for p in positions):
                del self._position_cache[key]
        return positions

    async def fetch_account_summary(self) -> dict:
        if not self.wallet_address:
            return {"equity": 0.0, "margin": 0.0}
        data = await self._info({
            "type": "clearinghouseState",
            "user": self.wallet_address,
        })
        return {
            "equity": float(data.get("marginSummary", {}).get("accountValue", 0)),
            "margin": float(data.get("marginSummary", {}).get("totalMarginUsed", 0)),
            "free_collateral": float(data.get("withdrawable", 0)),
        }

    async def adjust_leverage(self, asset: str, leverage: float):
        if not self.wallet_address:
            return
        payload = {
            "type": "updateLeverage",
            "asset": asset,
            "leverage": leverage,
        }
        sig = self._sign(payload)
        await self._http.post(HL_EXCHANGE, json=sig)

    async def place_order(self, order: Order) -> Optional[str]:
        if not self.wallet_address:
            return None
        if order.order_type == OrderType.MARKET:
            payload = {
                "action": {
                    "type": "order",
                    "orders": [{
                        "a": order.asset,
                        "b": order.side == Side.LONG,
                        "p": str(order.price) if order.price else "",
                        "s": str(order.quantity),
                        "r": order.reduce_only,
                        "t": {"limit": {"tif": "Ioc"}},
                    }],
                    "grouping": "na",
                    "brokerCode": 1,
                },
                "nonce": int(time.time() * 1000),
            }
            sig = self._sign(payload)
            resp = await self._http.post(HL_EXCHANGE, json=sig)
            data = resp.json()
            return str(data)
        return None

    async def cancel_order(self, cloid: str) -> bool:
        if not self.wallet_address:
            return False
        payload = {
            "type": "cancel",
            "asset": "",
            "cloid": cloid,
        }
        sig = self._sign(payload)
        await self._http.post(HL_EXCHANGE, json=sig)
        return True

    def _sign(self, payload: dict) -> dict:
        if not self.private_key:
            return payload
        msg = json.dumps(payload, separators=(",", ":"))
        sig = hmac.new(
            self.private_key.encode(),
            msg.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {"payload": payload, "signature": sig, "wallet": self.wallet_address}

    # ── WebSocket ──────────────────────────────────────────────────────

    async def connect_ws(self):
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed, WS data unavailable")
            return
        self._running = True
        while self._running:
            try:
                async with websockets.connect(HL_WS) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "subscribe", "channel": "allMids"}))
                    await ws.send(json.dumps({"type": "subscribe", "channel": "funding"}))
                    logger.info("HL WebSocket connected")
                    async for msg in ws:
                        data = json.loads(msg)
                        await self._handle_ws_message(data)
            except Exception as e:
                logger.warning("WS disconnected: %s — reconnecting in %.0fs", e, self._ws_reconnect_delay)
                self._ws = None
                await asyncio.sleep(self._ws_reconnect_delay)

    async def _handle_ws_message(self, data: dict):
        channel = data.get("channel", "")
        if channel == "allMids":
            self._latest_mids.update({k: float(v) for k, v in data.get("data", {}).items()})
        elif channel == "funding":
            for entry in data.get("data", []):
                coin = entry.get("coin", "")
                rate = float(entry.get("fundingRate", 0))
                if coin:
                    self._latest_funding[coin] = rate
        for cb in self._callbacks:
            try:
                await cb(data)
            except Exception:
                pass

    def on_message(self, callback: Callable):
        self._callbacks.append(callback)

    async def fetch_price(self, asset: str) -> float:
        mids = self._latest_mids or await self.fetch_all_mids()
        return mids.get(asset, 0.0)

    async def close(self):
        self._running = False
        await self._http.aclose()
