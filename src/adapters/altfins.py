"""
Altfins API v2 adapter — expanded screener + filtered signal keys with ensemble integration.

Each Altfins data point feeds into our signal ensemble:
  - Screener indicators → numeric signals (RSI, ATR, MACD, Stoch, etc.)
  - Signal keys → filtered to high-value directional keys only
  - Decay-weighted accuracy tracking, retired if <48%, quadratic edge amplification

NotebookLM-verified Tier 1 signal selection.
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

SYMBOL_MAP = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "BNB": "BNB",
    "XRP": "XRP", "DOGE": "DOGE", "ADA": "ADA", "AVAX": "AVAX",
    "LINK": "LINK", "DOT": "DOT",
}

TREND_MAP = {
    "STRONG_UP": 1.0,
    "UP": 0.6,
    "NEUTRAL": 0.0,
    "DOWN": -0.6,
    "STRONG_DOWN": -1.0,
}

TREND_CHANGE_MAP = {
    "UPGRADE": 0.8,
    "DOWNGRADE": -0.8,
    "NO_CHANGE": 0.0,
}

# Expanded screener fields — NotebookLM verified
VALUE_TYPES = [
    "RSI14",
    "RSI9",
    "RSI25",
    "ADX",
    "SMA50",
    "SMA200",
    "SHORT_TERM_TREND",
    "MEDIUM_TERM_TREND",
    "LONG_TERM_TREND",
    "ATR",
    "TR_VS_ATR",
    "OBV_TREND",
    "VOLUME_RELATIVE",
    "MACD",
    "MACD_SIGNAL_LINE",
    "MACD_HISTOGRAM",
    "STOCH",
    "STOCH_RSI",
    "WILLIAMS",
    "BOLLINGER_BAND_UPPER",
    "BOLLINGER_BAND_LOWER",
    "ATH_PERCENT_DOWN",
    "SHORT_TERM_TREND_CHANGE",
    "MEDIUM_TERM_TREND_CHANGE",
]

# NotebookLM-verified Tier 1 signal keys — only these pass through
ALLOWED_SIGNAL_KEYS = {
    "UP_DOWN_TREND",
    "SIGNALS_SUMMARY_STRONG_UP_DOWN_TREND",
    "UP_DOWN_TREND_AND_FRESH_MOMENTUM_INFLECTION",
    "MOMENTUM_UP_DOWN_TREND",
    "FRESH_MOMENTUM_MACD_SIGNAL_LINE_CROSSOVER",
    "EARLY_MOMENTUM_MACD_HISTOGRAM_INFLECTION",
    "MOMENTUM_RSI_CONFIRMATION",
    "SIGNALS_SUMMARY_OVERSOLD_OVERBOUGHT_UP_DOWN",
    "SIGNALS_SUMMARY_OVERSOLD_OVERBOUGHT_MOMENTUM",
    "SIGNALS_SUMMARY_VERY_OVERSOLD_OVERBOUGHT",
    "PULLBACK_UP_DOWN_TREND",
    "SUPPORT_RESISTANCE_BREAKOUT",
    "SUPPORT_RESISTANCE_APPROACHING_OVERSOLD",
    "SIGNALS_SUMMARY_SMA_50_200",
    "SIGNALS_SUMMARY_EMA_12_26",
    "SIGNALS_SUMMARY_TR_ATR_2x",
    "SIGNALS_SUMMARY_TR_ATR_3x",
    "SIGNALS_SUMMARY_BOLLBAND_PRICE_UPPER_LOWER",
    "SIGNALS_SUMMARY_RSI_DIVERGENCE",
    "SIGNALS_SUMMARY_TRADING_RANGE_V2",
    "SIGNALS_SUMMARY_CHANNEL_UP",
    "SIGNALS_SUMMARY_CHANNEL_DOWN",
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
        """Fetch expanded indicators for our asset universe."""
        alt_symbols = [s for s in symbols if s in SYMBOL_MAP]
        if not alt_symbols:
            return {}

        body = {
            "symbols": alt_symbols,
            "timeInterval": "HOURLY",
            "displayType": VALUE_TYPES,
        }

        try:
            payload = {**body, "page": 0, "size": min(len(alt_symbols), 50)}
            data = await self._post("/screener-data/search-requests", payload)
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
            raw = item.get("additionalData", item)
            parsed = {}
            for field in VALUE_TYPES:
                val = raw.get(field)
                if val is not None:
                    try:
                        parsed[field] = float(val)
                    except (ValueError, TypeError):
                        parsed[field] = val
            results[local] = parsed

        self._cached_indicators = results
        return results

    async def fetch_signals(
        self, symbols: list[str], lookback_days: int = 1
    ) -> list[Signal]:
        """Fetch triggered signals, filtered to NotebookLM-verified Tier 1 keys."""
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

            signal_key = item.get("signalKey", "")
            if signal_key not in ALLOWED_SIGNAL_KEYS:
                continue

            direction = item.get("direction", "BULLISH")
            signal_name = item.get("signalName", signal_key)
            ts = item.get("timestamp", now.isoformat())

            try:
                parsed_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                parsed_ts = now

            side = Side.LONG if direction == "BULLISH" else Side.SHORT

            # Base confidence 0.6, boosted for trend-sensitive + trendUp combo
            base_conf = 0.6
            trend_sensitive = item.get("trendSensitive", False)
            if trend_sensitive and direction == "BULLISH":
                indicator = self._cached_indicators.get(local, {})
                st = indicator.get("SHORT_TERM_TREND")
                if isinstance(st, str):
                    trend_val = TREND_MAP.get(st, 0.0)
                    if trend_val > 0:
                        base_conf = min(0.85, base_conf + 0.15)

            signals.append(Signal(
                source=f"altfins:{signal_key}",
                asset=local,
                direction=side,
                confidence=base_conf,
                timestamp=parsed_ts,
                bucket="altfins_signal",
                metadata={
                    "signal_name": signal_name,
                    "signal_key": signal_key,
                    "trend_sensitive": trend_sensitive,
                },
            ))

        self._cached_signals = signals
        return signals

    async def fetch_indicators_as_signals(
        self, symbols: list[str]
    ) -> list[Signal]:
        """Convert expanded screener indicators into weighted signals."""
        indicators = await self.fetch_screener(symbols)
        signals = []

        for asset, data in indicators.items():
            now_utc = datetime.now(timezone.utc)

            # === RSI ===
            rsi14 = data.get("RSI14")
            if rsi14 is not None:
                if rsi14 <= 28:
                    conf = max(0.5, 1.0 - (rsi14 / 56))
                    signals.append(Signal(
                        source="altfins:rsi14_oversold",
                        asset=asset, direction=Side.LONG, confidence=min(conf, 1.0),
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"rsi14": rsi14, "type": "oversold"},
                    ))
                elif rsi14 >= 72:
                    conf = max(0.5, (rsi14 - 50) / 50)
                    signals.append(Signal(
                        source="altfins:rsi14_overbought",
                        asset=asset, direction=Side.SHORT, confidence=min(conf, 1.0),
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"rsi14": rsi14, "type": "overbought"},
                    ))

            # RSI 9 — faster oversold/overbought
            rsi9 = data.get("RSI9")
            if rsi9 is not None and rsi9 <= 25:
                signals.append(Signal(
                    source="altfins:rsi9_oversold",
                    asset=asset, direction=Side.LONG, confidence=0.55,
                    timestamp=now_utc, bucket="altfins_indicator",
                    metadata={"rsi9": rsi9},
                ))
            elif rsi9 is not None and rsi9 >= 75:
                signals.append(Signal(
                    source="altfins:rsi9_overbought",
                    asset=asset, direction=Side.SHORT, confidence=0.55,
                    timestamp=now_utc, bucket="altfins_indicator",
                    metadata={"rsi9": rsi9},
                ))

            # === STOCH ===
            stoch = data.get("STOCH")
            if stoch is not None:
                if stoch <= 20:
                    signals.append(Signal(
                        source="altfins:stoch_oversold",
                        asset=asset, direction=Side.LONG, confidence=0.5,
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"stoch": stoch, "type": "oversold"},
                    ))
                elif stoch >= 80:
                    signals.append(Signal(
                        source="altfins:stoch_overbought",
                        asset=asset, direction=Side.SHORT, confidence=0.5,
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"stoch": stoch, "type": "overbought"},
                    ))

            # === WILLIAMS %R ===
            williams = data.get("WILLIAMS")
            if williams is not None:
                if williams <= -80:
                    signals.append(Signal(
                        source="altfins:williams_oversold",
                        asset=asset, direction=Side.LONG, confidence=0.5,
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"williams": williams},
                    ))
                elif williams >= -20:
                    signals.append(Signal(
                        source="altfins:williams_overbought",
                        asset=asset, direction=Side.SHORT, confidence=0.5,
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"williams": williams},
                    ))

            # === TREND ===
            for trend_type, key in [
                ("SHORT_TERM_TREND", "short"),
                ("MEDIUM_TERM_TREND", "medium"),
                ("LONG_TERM_TREND", "long"),
            ]:
                raw = data.get(trend_type)
                if isinstance(raw, str):
                    val = TREND_MAP.get(raw, 0.0)
                    if abs(val) >= 0.6:
                        side = Side.LONG if val > 0 else Side.SHORT
                        signals.append(Signal(
                            source=f"altfins:{key}_trend",
                            asset=asset, direction=side, confidence=abs(val),
                            timestamp=now_utc, bucket="altfins_trend",
                            metadata={"trend": raw, "period": trend_type},
                        ))

            # === TREND CHANGES — early shift detection ===
            for change_type, key in [
                ("SHORT_TERM_TREND_CHANGE", "short_trend_change"),
                ("MEDIUM_TERM_TREND_CHANGE", "medium_trend_change"),
            ]:
                raw = data.get(change_type)
                if isinstance(raw, str):
                    val = TREND_CHANGE_MAP.get(raw, 0.0)
                    if abs(val) >= 0.8:
                        side = Side.LONG if val > 0 else Side.SHORT
                        signals.append(Signal(
                            source=f"altfins:{key}",
                            asset=asset, direction=side, confidence=0.65,
                            timestamp=now_utc, bucket="altfins_trend",
                            metadata={"change": raw, "period": change_type},
                        ))

            # === MACD MOMENTUM ===
            macd_hist = data.get("MACD_HISTOGRAM")
            macd_line = data.get("MACD")
            macd_signal = data.get("MACD_SIGNAL_LINE")
            if macd_hist is not None and macd_line is not None and macd_signal is not None:
                # Histogram rising = bullish momentum
                prev_hist = self._cached_indicators.get(asset, {}).get("MACD_HISTOGRAM", macd_hist)
                if isinstance(prev_hist, (int, float)) and isinstance(macd_hist, (int, float)):
                    if macd_hist > prev_hist and macd_hist > 0:
                        signals.append(Signal(
                            source="altfins:macd_momentum_bullish",
                            asset=asset, direction=Side.LONG, confidence=0.6,
                            timestamp=now_utc, bucket="altfins_indicator",
                            metadata={"macd_hist": macd_hist, "macd_line": macd_line},
                        ))
                # MACD line cross above signal = bullish cross
                if macd_line > macd_signal and data.get("_prev_macd", macd_line) <= data.get("_prev_signal", macd_signal):
                    signals.append(Signal(
                        source="altfins:macd_cross_bullish",
                        asset=asset, direction=Side.LONG, confidence=0.65,
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"macd": macd_line, "signal": macd_signal},
                    ))
                data["_prev_macd"] = macd_line
                data["_prev_signal"] = macd_signal

            # === BOLLINGER BAND ===
            bb_upper = data.get("BOLLINGER_BAND_UPPER")
            bb_lower = data.get("BOLLINGER_BAND_LOWER")
            price = data.get("LAST_PRICE") or 0
            if bb_upper is not None and price > 0 and price >= bb_upper:
                signals.append(Signal(
                    source="altfins:bollinger_touch_upper",
                    asset=asset, direction=Side.SHORT, confidence=0.5,
                    timestamp=now_utc, bucket="altfins_indicator",
                    metadata={"price": price, "bb_upper": bb_upper},
                ))
            if bb_lower is not None and price > 0 and price <= bb_lower:
                signals.append(Signal(
                    source="altfins:bollinger_touch_lower",
                    asset=asset, direction=Side.LONG, confidence=0.5,
                    timestamp=now_utc, bucket="altfins_indicator",
                    metadata={"price": price, "bb_lower": bb_lower},
                ))

            # === ATR VOLATILITY REGIME ===
            tr_vs_atr = data.get("TR_VS_ATR")
            if tr_vs_atr is not None and tr_vs_atr > 0:
                if tr_vs_atr >= 3.0:
                    signals.append(Signal(
                        source="altfins:volatility_high",
                        asset=asset, direction=Side.SHORT, confidence=0.5,
                        timestamp=now_utc, bucket="altfins_volatility",
                        metadata={"tr_vs_atr": tr_vs_atr},
                    ))

            # === VOLUME CONFIRMATION ===
            rvol = data.get("VOLUME_RELATIVE")
            obv = data.get("OBV_TREND")
            if rvol is not None and rvol >= 1.5 and isinstance(obv, str):
                if "UP" in obv:
                    signals.append(Signal(
                        source="altfins:volume_breakout_bullish",
                        asset=asset, direction=Side.LONG, confidence=0.55,
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"rvol": rvol, "obv": obv},
                    ))
                elif "DOWN" in obv:
                    signals.append(Signal(
                        source="altfins:volume_breakout_bearish",
                        asset=asset, direction=Side.SHORT, confidence=0.55,
                        timestamp=now_utc, bucket="altfins_indicator",
                        metadata={"rvol": rvol, "obv": obv},
                    ))

            # === ATH DISTANCE (risk metric) ===
            ath_down = data.get("ATH_PERCENT_DOWN")
            if ath_down is not None and ath_down <= 5:
                signals.append(Signal(
                    source="altfins:near_ath",
                    asset=asset, direction=Side.SHORT, confidence=0.4,
                    timestamp=now_utc, bucket="altfins_risk",
                    metadata={"ath_down_pct": ath_down},
                ))

        return signals

    async def get_all_signals(self, symbols: list[str]) -> list[Signal]:
        """Combined: screener indicators + filtered signals feed + ensemble score.

        Fetches screener data ONCE, avoids duplicate API calls.
        """
        # Fetch screener first so indicators are cached for fetch_signals
        indicator_sigs = await self.fetch_indicators_as_signals(symbols)
        altfins_sigs = await self.fetch_signals(symbols)

        # Compute ensemble score from already-cached data
        alt_symbols = [s for s in symbols if s in SYMBOL_MAP]
        all_sigs = indicator_sigs + altfins_sigs
        now_utc = datetime.now(timezone.utc)

        scores: dict[str, dict[str, float]] = {}
        for sig in all_sigs:
            asset = sig.asset
            if asset not in scores:
                scores[asset] = {"long_score": 0.0, "short_score": 0.0, "signal_count": 0, "net_score": 0.0}
            bucket = sig.direction.value
            scores[asset]["signal_count"] += 1
            increment = sig.confidence * 0.2
            scores[asset][f"{bucket}_score"] = min(1.0, scores[asset][f"{bucket}_score"] + increment)
            scores[asset]["net_score"] = scores[asset]["long_score"] - scores[asset]["short_score"]

        # Add composite ensemble signal
        for asset, score in scores.items():
            if score["signal_count"] >= 3 and abs(score["net_score"]) >= 0.3:
                net = score["net_score"]
                direction = Side.LONG if net > 0 else Side.SHORT
                altfins_sigs.append(Signal(
                    source="altfins:ensemble",
                    asset=asset,
                    direction=direction,
                    confidence=min(abs(net), 1.0),
                    timestamp=now_utc,
                    bucket="altfins_ensemble",
                    metadata={"signal_count": score["signal_count"], "net_score": round(net, 3)},
                ))

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
