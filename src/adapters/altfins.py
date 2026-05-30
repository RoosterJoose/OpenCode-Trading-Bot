"""
Altfins API v2 adapter — screener data + signals feed as weighted signal sources.

Each Altfins data point feeds into our signal ensemble just like local TA:
decay-weighted accuracy tracking, retired if <48%, quadratic edge amplification.

Endpoints used:
  Screener: POST /api/v2/public/screener-data/search-requests — RSI, SMA, trends, etc.
  Signals:  POST /api/v2/public/signals-feed/search-requests   — 130+ signal types
  TA:       GET  /api/v2/public/technical-analysis/data         — expert trade setups
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from src.core.types import Side, Signal

logger = logging.getLogger("hermes.altfins")

BASE = "https://altfins.com"
API = f"{BASE}/api/v2/public"

TIMEOUT = httpx.Timeout(30.0)

# Map Altfins symbols → our asset names
SYMBOL_MAP = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "BNB": "BNB",
    "XRP": "XRP", "DOGE": "DOGE", "ADA": "ADA", "AVAX": "AVAX",
    "LINK": "LINK", "DOT": "DOT",
}

# Screener value types we care about for signals
VALUE_TYPES = [
    "RSI14",
    "RSI9",
    "ADX",
    "SMA50",
    "SMA200",
    "SHORT_TERM_TREND",
    "MEDIUM_TERM_TREND",
    "LONG_TERM_TREND",
]

TREND_MAP = {
    "STRONG_UP": 1.0,
    "UP": 0.6,
    "NEUTRAL": 0.0,
    "DOWN": -0.6,
    "STRONG_DOWN": -1.0,
}


class AltfinsAdapter:
    def __init__(self, api_key: str, check_interval: int = 300):
        self.api_key = api_key
        self.check_interval = check_interval
        self._http = httpx.AsyncClient(timeout=TIMEOUT)
        self._last_fetch: Optional[datetime] = None
        self._cached_signals: list[Signal] = []
        self._cached_indicators: dict[str, dict] = {}

    async def _post(self, path: str, body: dict) -> Any:
        resp = await self._http.post(
            f"{API}{path}",
            json=body,
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        resp = await self._http.get(
            f"{API}{path}",
            params=params,
            headers={"X-API-KEY": self.api_key},
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_screener(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch key indicators for our asset universe from Altfins screener."""
        alt_symbols = [s for s in symbols if s in SYMBOL_MAP]
        if not alt_symbols:
            return {}

        body = {
            "symbols": alt_symbols,
            "timeInterval": "HOURLY",
            "displayType": VALUE_TYPES,
        }

        try:
            data = await self._post("/screener-data/search-requests", {
                **body,
                "page": 0,
                "size": min(len(alt_symbols), 50),
            })
        except Exception as e:
            logger.debug("Altfins screener error: %s", e)
            return self._cached_indicators

        results = {}
        if isinstance(data, dict):
            items = data.get("content") or data.get("data") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        for item in items:
            symbol = item.get("symbol", "")
            local = SYMBOL_MAP.get(symbol)
            if not local:
                continue
            results[local] = item.get("additionalData", item)

        self._cached_indicators = results
        return results

    async def fetch_signals(
        self, symbols: list[str], lookback_days: int = 1
    ) -> list[Signal]:
        """Fetch triggered signals from Altfins signals feed."""
        alt_symbols = [s for s in symbols if s in SYMBOL_MAP]
        if not alt_symbols:
            return []

        now = datetime.now(timezone.utc)
        from_date = now

        body = {
            "symbols": alt_symbols,
            "fromDate": from_date.isoformat(),
            "toDate": now.isoformat(),
        }

        try:
            payload = {**body, "page": 0, "size": 50}
            data = await self._post("/signals-feed/search-requests", payload)
        except Exception as e:
            logger.debug("Altfins signals error: %s", e)
            return self._cached_signals

        if isinstance(data, dict):
            items = data.get("content") or data.get("data") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        signals = []
        for item in items:
            symbol = item.get("symbol", "")
            local = SYMBOL_MAP.get(symbol)
            if not local:
                continue

            direction = item.get("direction", "BULLISH")
            signal_key = item.get("signalKey", "unknown")
            signal_name = item.get("signalName", signal_key)

            side = Side.LONG if direction == "BULLISH" else Side.SHORT
            ts = item.get("timestamp", now.isoformat())

            try:
                parsed_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                parsed_ts = now

            signals.append(Signal(
                source=f"altfins:{signal_key}",
                asset=local,
                direction=side,
                confidence=0.6,
                timestamp=parsed_ts,
                bucket="altfins_signal",
                metadata={"signal_name": signal_name, "signal_key": signal_key},
            ))

        self._cached_signals = signals
        return signals

    async def fetch_indicators_as_signals(
        self, symbols: list[str]
    ) -> list[Signal]:
        """Convert screener indicators into weighted signals."""
        indicators = await self.fetch_screener(symbols)
        signals = []

        for asset, data in indicators.items():
            # RSI oversold / overbought signals
            rsi14 = data.get("RSI14")
            if rsi14 is not None:
                rsi14 = float(rsi14)
                if rsi14 <= 28:
                    confidence = max(0.5, 1.0 - (rsi14 / 56))
                    signals.append(Signal(
                        source=f"altfins:rsi14_oversold",
                        asset=asset,
                        direction=Side.LONG,
                        confidence=min(confidence, 1.0),
                        timestamp=datetime.now(timezone.utc),
                        bucket="altfins_indicator",
                        metadata={"rsi14": rsi14, "type": "oversold"},
                    ))
                elif rsi14 >= 72:
                    confidence = max(0.5, (rsi14 - 50) / 50)
                    signals.append(Signal(
                        source=f"altfins:rsi14_overbought",
                        asset=asset,
                        direction=Side.SHORT,
                        confidence=min(confidence, 1.0),
                        timestamp=datetime.now(timezone.utc),
                        bucket="altfins_indicator",
                        metadata={"rsi14": rsi14, "type": "overbought"},
                    ))

            # Trend signals
            for trend_type, key in [
                ("SHORT_TERM_TREND", "short"),
                ("MEDIUM_TERM_TREND", "medium"),
                ("LONG_TERM_TREND", "long"),
            ]:
                raw = data.get(trend_type)
                if raw and isinstance(raw, str):
                    val = TREND_MAP.get(raw, 0.0)
                    if abs(val) >= 0.6:
                        side = Side.LONG if val > 0 else Side.SHORT
                        signals.append(Signal(
                            source=f"altfins:{key}_trend",
                            asset=asset,
                            direction=side,
                            confidence=abs(val),
                            timestamp=datetime.now(timezone.utc),
                            bucket="altfins_trend",
                            metadata={"trend": raw, "period": trend_type},
                        ))

        return signals

    async def get_all_signals(self, symbols: list[str]) -> list[Signal]:
        """Combined: fetches screener indicators + signals feed."""
        altfins_sigs = await self.fetch_signals(symbols)
        indicator_sigs = await self.fetch_indicators_as_signals(symbols)
        return altfins_sigs + indicator_sigs

    async def check_permit_usage(self) -> dict:
        """Check remaining API permit count."""
        try:
            info = await self._get("/available-permits")
            return {"available": info} if isinstance(info, (int, float)) else info
        except Exception as e:
            return {"error": str(e)}

    async def close(self):
        await self._http.aclose()
