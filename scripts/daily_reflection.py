#!/usr/bin/env python3
"""
Daily end-of-day market reflection.

Fetches 24h of 1h candles for all tracked assets, detects significant moves,
compares against Hermes signals, and generates learning insights for missed moves.

Results written to SQLite state keys:
  - daily_reflection: full daily report
  - missed_moves: detailed missed-move analyses
"""

import json
import math
import sqlite3
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Tracked assets (must match Hermes config) ──────────────────────

ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "LTC", "NEAR", "ATOM", "UNI", "ARB", "OP", "APT", "SUI", "AAVE", "INJ",
]

# ── Hyperliquid API helper ──────────────────────────────────────────

HL_URL = "https://api.hyperliquid.xyz/info"


def fetch_candles(coin: str, hours: int = 24) -> list[dict]:
    now_ms = int(time.time() * 1000)
    interval_ms = 3_600_000
    start_ms = now_ms - (hours * interval_ms)
    body = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": now_ms},
    }
    req = urllib.request.Request(HL_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return [{
        "ts": c["t"], "open": float(c["o"]), "high": float(c["h"]),
        "low": float(c["l"]), "close": float(c["c"]), "volume": float(c["v"]),
    } for c in data] if isinstance(data, list) else []


# ── Technical indicators ─────────────────────────────────────────────

def ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1]
    k = 2.0 / (period + 1)
    result = sum(closes[:period]) / period
    for p in closes[period:]:
        result = (p - result) * k + result
    return result


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def adx(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period * 2 + 5:
        return 0.0
    tr_vals, plus_dm, minus_dm = [], [], []
    for i in range(-period * 2 + 1, 0):
        h, l, ph, pl = candles[i]["high"], candles[i]["low"], candles[i - 1]["high"], candles[i - 1]["low"]
        tr_vals.append(max(h - l, abs(h - pl), abs(l - ph)))
        up = h - ph
        down = pl - l
        plus_dm.append(max(up, 0) if up > down else 0)
        minus_dm.append(max(down, 0) if down > up else 0)
    atr_p = sum(tr_vals[-period:]) / period
    if atr_p <= 0:
        return 0.0
    pdi = (sum(plus_dm[-period:]) / period) / atr_p * 100
    ndi = (sum(minus_dm[-period:]) / period) / atr_p * 100
    dx = abs(pdi - ndi) / (pdi + ndi) * 100 if (pdi + ndi) > 0 else 0
    return dx


# ── Missed move analysis ─────────────────────────────────────────────

def analyze_missed_move(
    asset: str,
    candles: list[dict],
    price_before: float,
    price_after: float,
    daily_change_pct: float,
    had_signal: bool,
) -> dict:
    closes = [c["close"] for c in candles]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi_val = rsi(closes)
    adx_val = adx(candles)
    last = candles[-1] if candles else {}
    volume_24h = sum(c["volume"] for c in candles) * last.get("close", 1)

    bull = ema9 > ema21
    bear = ema9 < ema21
    near_ema = abs(closes[-1] - ema9) / ema9 * 100 if ema9 else 999

    # Determine what setup existed before the move
    # Look at candles before the last 3 (ignore the move candles themselves)
    pre_closes = [c["close"] for c in candles[:-3]]
    pre_ema9 = ema(pre_closes, 9) if len(pre_closes) >= 9 else None
    pre_ema21 = ema(pre_closes, 21) if len(pre_closes) >= 21 else None
    pre_rsi = rsi(pre_closes)
    pre_adx = adx(candles[:-3])
    pre_bull = pre_ema9 > pre_ema21 if pre_ema9 and pre_ema21 else None
    pre_near_ema = abs(pre_closes[-1] - pre_ema9) / pre_ema9 * 100 if pre_ema9 and pre_closes else 999

    would_enter = False
    blocked_reason = ""

    # Simulate strategy logic pre-move
    if daily_change_pct < -1.0:  # Bearish move
        # Trend short check
        if pre_bull is False and pre_adx and pre_adx > 25 and pre_near_ema <= 4:
            would_enter = True
            blocked_reason = "trend_short"
        elif pre_rsi and pre_rsi > 72 and pre_adx and pre_adx < 30:
            # MR short check
            would_enter = True
            blocked_reason = "mr_short"
        else:
            reasons = []
            if pre_bull is not False:
                reasons.append("no_bear_cross")
            if pre_adx and pre_adx <= 25:
                reasons.append(f"adx_{pre_adx:.0f}_too_low")
            if pre_near_ema > 4:
                reasons.append(f"extended_{pre_near_ema:.1f}%")
            if pre_rsi and pre_rsi < 28:
                reasons.append(f"oversold_rsi_{pre_rsi:.0f}")
            blocked_reason = "; ".join(reasons) if reasons else "multiple_gates"
    else:  # Bullish move
        # Trend long check
        if pre_bull is True and pre_adx and pre_adx > 25 and pre_near_ema <= 4:
            would_enter = True
            blocked_reason = "trend_long"
        elif pre_rsi and pre_rsi < 28 and pre_adx and pre_adx < 30:
            # MR long check
            would_enter = True
            blocked_reason = "mr_long"
        else:
            reasons = []
            if pre_bull is not True:
                reasons.append("no_bull_cross")
            if pre_adx and pre_adx <= 25:
                reasons.append(f"adx_{pre_adx:.0f}_too_low")
            if pre_near_ema > 4:
                reasons.append(f"extended_{pre_near_ema:.1f}%")
            if pre_rsi and pre_rsi > 72:
                reasons.append(f"overbought_rsi_{pre_rsi:.0f}")
            blocked_reason = "; ".join(reasons) if reasons else "multiple_gates"

    return {
        "asset": asset,
        "daily_change_pct": round(daily_change_pct, 2),
        "price_start": round(candles[0]["close"], 2) if candles else 0,
        "price_end": round(candles[-1]["close"], 2) if candles else 0,
        "volume_24h_usd": round(volume_24h, 0),
        "range_pct": round((max(c["high"] for c in candles) - min(c["low"] for c in candles)) / closes[0] * 100, 2) if candles else 0,
        "had_signal": had_signal,
        "pre_move_ema9": round(pre_ema9, 2) if pre_ema9 else None,
        "pre_move_ema21": round(pre_ema21, 2) if pre_ema21 else None,
        "pre_move_bull": pre_bull,
        "pre_move_near_ema_pct": round(pre_near_ema, 2),
        "pre_move_rsi": round(pre_rsi, 1) if pre_rsi else None,
        "pre_move_adx": round(pre_adx, 1) if pre_adx else None,
        "would_enter": would_enter,
        "entry_reason": blocked_reason if would_enter else None,
        "missed_reason": None if would_enter else blocked_reason,
    }


# ── Main ─────────────────────────────────────────────────────────────

def main(db_path: str):
    db = Path(db_path)
    conn = sqlite3.connect(str(db))
    report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = []
    missed_moves = []
    total_significant = 0
    total_missed = 0
    total_entered = 0

    # Load existing signal state
    signal_state = {}
    try:
        rows = conn.execute("SELECT key, value FROM state WHERE key LIKE 'last_signal_%'").fetchall()
        for k, v in rows:
            signal_state[k.replace("last_signal_", "")] = json.loads(v)
    except Exception:
        pass

    for asset in ASSETS:
        candles = fetch_candles(asset, 24)
        if not candles or len(candles) < 5:
            continue

        closes = [c["close"] for c in candles]
        start_price = closes[0]
        end_price = closes[-1]
        change_pct = (end_price - start_price) / start_price * 100

        high = max(c["high"] for c in candles)
        low = min(c["low"] for c in candles)
        range_pct = (high - low) / start_price * 100

        volume_24h = sum(c["volume"] for c in candles) * end_price

        had_signal = asset in signal_state
        current_rsi = rsi(closes)
        current_adx_val = adx(candles)

        is_significant = abs(change_pct) >= 1.5 or range_pct >= 3.0
        if is_significant:
            total_significant += 1
            if not had_signal:
                analysis = analyze_missed_move(asset, candles, start_price, end_price, change_pct, False)
                missed_moves.append(analysis)
                total_missed += 1
                if analysis.get("would_enter"):
                    total_entered += 1

        results.append({
            "asset": asset,
            "change_24h_pct": round(change_pct, 2),
            "range_24h_pct": round(range_pct, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(end_price, 2),
            "volume_24h_usd": round(volume_24h, 0),
            "rsi": round(current_rsi, 1),
            "adx": round(current_adx_val, 1),
            "had_signal": had_signal,
            "significant": is_significant,
        })

    # Generate learning summary
    learning = []
    if missed_moves:
        would_have_entered = [m for m in missed_moves if m.get("would_enter")]
        strategy_could_not = [m for m in missed_moves if not m.get("would_enter")]

        if would_have_entered:
            learning.append({
                "type": "missed_by_config",
                "count": len(would_have_entered),
                "assets": [m["asset"] for m in would_have_entered],
                "reasons": list(set(m["entry_reason"] for m in would_have_entered if m.get("entry_reason"))),
                "action": "Review strategy config or confidence thresholds — setup was present but missed.",
            })

        if strategy_could_not:
            missing_patterns = defaultdict(list)
            for m in strategy_could_not:
                reason = m.get("missed_reason", "unknown")
                missing_patterns[reason].append(m["asset"])
            for reason, assets in sorted(missing_patterns.items()):
                learning.append({
                    "type": "strategy_limitation",
                    "count": len(assets),
                    "assets": assets,
                    "reason": reason,
                    "action": "Strategy does not capture this pattern. Adjust parameters or add new entry type.",
                })

        # Weekly trend direction summary
        bull_count = sum(1 for r in results if r["change_24h_pct"] > 0)
        bear_count = sum(1 for r in results if r["change_24h_pct"] < 0)
        strong_bull = sum(1 for r in results if r["change_24h_pct"] > 3)
        strong_bear = sum(1 for r in results if r["change_24h_pct"] < -3)
        high_adx = sum(1 for r in results if r["adx"] > 30)

        learning.append({
            "type": "market_summary",
            "date": report_date,
            "bull_count": bull_count,
            "bear_count": bear_count,
            "strong_bull": strong_bull,
            "strong_bear": strong_bear,
            "high_adx_assets": high_adx,
            "total_significant_moves": total_significant,
            "missed_moves": total_missed,
            "potentially_catchable": total_entered,
            "action": "Review strategy alignment with current market regime." if high_adx > 10 else None,
        })

    # Compute dominant bias
    bias = "mixed"
    if len(results) >= 5:
        avg_change = sum(r["change_24h_pct"] for r in results) / len(results)
        bull_ratio = sum(1 for r in results if r["change_24h_pct"] > 0) / len(results)
        if avg_change > 1.0 and bull_ratio > 0.6:
            bias = "bullish_dominant"
        elif avg_change < -1.0 and bull_ratio < 0.4:
            bias = "bearish_dominant"
        elif abs(avg_change) < 0.5:
            bias = "sideways"

    daily_report = {
        "date": report_date,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_assets": len(results),
        "significant_moves": total_significant,
        "missed_moves": total_missed,
        "potentially_catchable": total_entered,
        "bias": bias,
        "learning": learning,
        "assets": results,
    }

    # Write to SQLite
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                 ("daily_reflection", json.dumps(daily_report, default=str)))
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                 ("missed_moves", json.dumps(missed_moves, default=str)))

    # Also store a time-series entry for dashboard history
    try:
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                     (f"daily_reflection_{report_date}", json.dumps(daily_report, default=str)))
    except Exception:
        pass

    # ── Insert lessons into append-only table (NotebookLM design) ────
    try:
        # Clear old lessons for today before inserting fresh
        conn.execute("DELETE FROM lessons WHERE date = ?", (report_date,))
        for l in learning:
            if l["type"] == "market_summary":
                continue
            cat = l["type"]
            detail = l.get("reason", l.get("action", ""))
            cnt = l.get("count", 1)
            action = l.get("action", "")
            assets = l.get("assets", [])
            for asset in assets if assets else ["PORTFOLIO_WIDE"]:
                conn.execute(
                    "INSERT INTO lessons (date, asset, pattern_category, pattern_detail, frequency_count, action) VALUES (?, ?, ?, ?, ?, ?)",
                    (report_date, asset, cat, detail, cnt, action),
                )
        conn.commit()
    except Exception as e:
        print(f"Lesson insert error: {e}", file=sys.stderr)

    # ── Cumulative learnings aggregation ─────────────────────────────
    try:
        # Load all historical daily reflections
        rows = conn.execute("SELECT key, value FROM state WHERE key LIKE 'daily_reflection_%' AND key != ?",
                            (f"daily_reflection_{report_date}",)).fetchall()
        histories = []
        for k, v in rows:
            try:
                histories.append(json.loads(v))
            except Exception:
                pass

        all_learnings = []
        seen_dates = set()
        for h in histories + [daily_report]:
            d = h.get("date")
            if d and d not in seen_dates:
                seen_dates.add(d)
                all_learnings.append(h)

        # Count missed-move reasons across all history
        reason_counter: dict[str, int] = defaultdict(int)
        asset_move_counter: dict[str, int] = defaultdict(int)
        asset_bull_counter: dict[str, int] = defaultdict(int)
        asset_bear_counter: dict[str, int] = defaultdict(int)
        bias_history = []

        for rep in all_learnings:
            bias_history.append({"date": rep.get("date"), "bias": rep.get("bias")})
            for a in rep.get("assets", []):
                asset_move_counter[a["asset"]] += 1
                if a["change_24h_pct"] > 0:
                    asset_bull_counter[a["asset"]] += 1
                elif a["change_24h_pct"] < 0:
                    asset_bear_counter[a["asset"]] += 1

        # Count missed-move reasons
        for rep in all_learnings:
            for l in rep.get("learning", []):
                if l["type"] != "market_summary" and l.get("reason"):
                    reason_counter[l["reason"]] += l.get("count", 1)

        # Find top consistent losers (most bear days)
        top_bear = sorted(asset_bear_counter.items(), key=lambda x: -x[1])[:5]
        top_bull = sorted(asset_bull_counter.items(), key=lambda x: -x[1])[:5]

        cumulative = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "total_days": len(all_learnings),
            "bias_history": bias_history,
            "persistent_missed_reasons": [
                {"reason": r, "count": c}
                for r, c in sorted(reason_counter.items(), key=lambda x: -x[1])[:10]
            ],
            "most_frequently_bearish": [{"asset": a, "days": c} for a, c in top_bear],
            "most_frequently_bullish": [{"asset": a, "days": c} for a, c in top_bull],
            "lessons": [],
        }

        # Generate persistent lessons from patterns
        if reason_counter:
            top_reason = max(reason_counter.items(), key=lambda x: x[1])
            if top_reason[1] >= 2:
                cumulative["lessons"].append(
                    f"{top_reason[0]}: blocked {top_reason[1]}x across {len(all_learnings)} days. "
                    "Consistent pattern — consider whether this gate is too restrictive."
                )

        if bias_history:
            biased_days = [b for b in bias_history if b["bias"] in ("bullish_dominant", "bearish_dominant")]
            mixed_days = [b for b in bias_history if b["bias"] == "mixed"]
            if len(biased_days) > len(mixed_days):
                dominant_bias = max(set(b["bias"] for b in biased_days), key=lambda x: sum(1 for b in biased_days if b["bias"] == x))
                cumulative["lessons"].append(
                    f"Market bias dominant direction: {dominant_bias} on {len(biased_days)}/{len(bias_history)} days. "
                    "Consider weighting strategy allocation toward dominant regime."
                )
            elif len(mixed_days) > len(biased_days) and len(bias_history) >= 3:
                cumulative["lessons"].append(
                    f"Market has been mixed {len(mixed_days)}/{len(bias_history)} days. "
                    "No clear directional bias — mean reversion may outperform trends."
                )

        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                     ("cumulative_learnings", json.dumps(cumulative, default=str)))

        # Individual asset learning records
        asset_lessons = {}
        for rep in all_learnings:
            for a in rep.get("assets", []):
                asset_name = a["asset"]
                if asset_name not in asset_lessons:
                    asset_lessons[asset_name] = []
                asset_lessons[asset_name].append({
                    "date": rep.get("date"),
                    "change_pct": a["change_24h_pct"],
                    "rsi": a["rsi"],
                    "had_signal": a["had_signal"],
                })

        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
                     ("asset_learnings", json.dumps(asset_lessons, default=str)))
    except Exception as e:
        print(f"Cumulative learning error: {e}", file=sys.stderr)

    conn.commit()
    conn.close()

    print(f"Daily reflection {report_date}: {len(results)} assets, {total_significant} significant, "
          f"{total_missed} missed, {total_entered} catchable | bias={bias}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: daily_reflection.py <db_path>")
        sys.exit(1)
    main(sys.argv[1])
