# Hermes Signal Specification v1.0

> Canonical reference for porting Hermes logic into external frameworks (e.g. Freqtrade).
> Every rule below is traceable to a specific line in the Hermes v2 source.

---

## 1. Regime Detection

**Source:** `src/core/loop.py` lines 480–510 (`_infer_regime`)

**Inputs:** last 100+ closes, last 14 candles for ATR.

**Algorithm:**
1. Compute `norm_vol = ATR_14 / last_close`.
   - `ATR_14` = average true range over last 14 candles (`max(h-l, |h-pc|, |l-pc|)`).
2. If `norm_vol > 0.03` → `HIGH_VOL` (block entries).
3. If `norm_vol < 0.0015` → `DEAD_MARKET` (skip trading).
4. Compute Hurst exponent `H` over all closes (lags 2..min(n/2, 100)).
5. Compute Efficiency Ratio `ER` over last 14 closes:
   - `direction = abs(closes[-1] - closes[-15])`
   - `volatility = sum(abs(closes[i] - closes[i-1]) for i in range(-14, 0))`
   - `ER = direction / volatility` (or 0.5 if volatility == 0).
6. Joint classification:
   - `H > 0.55 and ER > 0.60` → `STRONGLY_TRENDING`
   - `H > 0.55` → `TRENDING`
   - `H < 0.45 and ER < 0.30` → `MEAN_REVERTING`
   - else → `RANDOM_WALK`

**Line refs:** loop.py:488–510

---

## 2. Mean Reversion Strategy (`mr`)

**Source:** `src/strategies/mr.py`

### 2.1 Entry (`should_enter`)

**Preconditions (all must pass):**
1. Asset not on cooldown (`cooldown_bars` default = 12).  
   Line: mr.py:57–59
2. No open position for this asset.  
   Line: mr.py:60–61
3. At least 50 candles.  
   Line: mr.py:62–63
4. Volume gate: `last.volume * last.close >= 2_000_000` USD.  
   Line: mr.py:66–67
5. Regime not in (`STRONGLY_TRENDING`, `HIGH_VOL`).  
   Line: mr.py:69–70
6. RSI oversold: `RSI_14 <= 28.0`.  
   Line: mr.py:72–75
   - RSI computed as simple average gain/loss over 14 periods (not Wilder smoothing).  
   Line: mr.py:175–188

**Stop distance:**
- ATR = simple average TR over 14 periods.  
  Line: mr.py:190–197
- `stop_pct = (ATR / entry_price) * mult`
  - `mult = 2.0` for BTC/ETH, else `3.0`.  
  Line: mr.py:82–84
- Clamp: `max(1.5%, min(stop_pct, 4.0%))`.  
  Line: mr.py:87–88
- `stop_price = entry_price * (1 - stop_pct)`.

**Confidence scoring:**
- Base = 0.5
- If `RSI <= 20` → +0.2
- Altfins confirmation (see §5) → `min(confidence * 1.2, 0.95)`
- No Altfins → `confidence *= 0.85`
- If `funding_rate < -0.001` → +0.15
- If regime is `MEAN_REVERTING` or `STRONGLY_MR` → +0.1
- Cap at 1.0  
  Lines: mr.py:95–131

**Entry returned as:**
```python
(Side.LONG, confidence, {
    "entry_price": entry_price,
    "stop_loss": stop_price,
    "risk_r": risk_r,
    "rsi": rsi,
    "atr_pct": stop_pct * 100,
    "sources": [...],
    "component_sources": [...],
    "tp1": entry_price + risk_r * 0.5 * entry_price,
    "tp2": entry_price + risk_r * 1.5 * entry_price,
    "tp3": entry_price + risk_r * 3.0 * entry_price,
})
```

### 2.2 Exit (`should_exit`)

1. Hard stop: `current_price <= position.stop_loss` → `"stop_loss"`.  
   Line: mr.py:154–155
2. R-multiple profit targets:
   - `r_mult = (current_price - entry) / max(entry - stop_loss, 0.001)`
   - If `r_mult >= 3.0` → `"tp3"`
   - If `r_mult >= 1.5` → `"tp2"`
   - If `r_mult >= 0.5` → `"tp1"`  
   Lines: mr.py:161–168
3. Funding spike: `funding_rate > 0.005` → `"funding_spike"`.  
   Line: mr.py:170–171

**Note:** MR exits only close MR positions (ownership enforced in loop.py:275–277).

---

## 3. Trend Following Strategy (`trend`)

**Source:** `src/strategies/trend.py`

### 3.1 Entry (`should_enter`)

**Preconditions:**
1. Not on cooldown (`cooldown_cycles` default = 60).  
   Line: trend.py:60–62
2. No open position.  
   Line: trend.py:63–64
3. `len(candles) >= slow_period + adx_period + 5` (21 + 14 + 5 = 40).  
   Line: trend.py:65–66
4. Regime in (`TRENDING`, `STRONGLY_TRENDING`).  
   Line: trend.py:68–69
5. Volume gate: `last.volume * last.close >= 5_000_000` USD.  
   Line: trend.py:71–73
6. EMA condition:
   - `ema_fast_9` and `ema_slow_21` computed from current candles.
   - Previous candle’s EMAs also computed.
   - `cross_above = prev_fast <= prev_slow and ema_fast > ema_slow`
   - `continuation = ema_fast > ema_slow and close > ema_fast`
   - `near_fast_ema = (close - ema_fast) / ema_fast <= 0.012`
   - Entry if `cross_above` OR (`continuation and near_fast_ema`).  
   Lines: trend.py:75–90
7. ADX >= 25.0  
   Line: trend.py:92–94
   - ADX computed as `abs(pdi - ndi) / (pdi + ndi) * 100` over 14 periods.  
   Lines: trend.py:204–223

**Confidence scoring:**
- Base = 0.5
- Sources start with `[ema_cross | trend_continuation, adx_confirmed]`
- If regime == `STRONGLY_TRENDING` → +0.2
- Altfins confirmation (trend keywords) → `min(confidence * 1.2, 0.95)`
- No Altfins → `confidence *= 0.85`
- If `funding_rate < -0.0005` → +0.1
- Cap at 1.0  
  Lines: trend.py:99–131

**Entry returned as:**
```python
(Side.LONG, confidence, {
    "entry_price": entry_price,
    "fast_ema": ema_fast,
    "slow_ema": ema_slow,
    "adx": adx,
    "atr": atr,
    "sources": [...],
    "component_sources": [...],
})
```

### 3.2 Exit (`should_exit`)

1. Hard stop: `current_price <= position.stop_loss` → `"stop_loss"`.  
   Line: trend.py:151–152
2. Chandelier exit:
   - `atr_dist = atr * 3.5`
   - `min_dist = 1.5% of price`, `max_dist = 4.0% of price`
   - `stop_dist = max(min_dist, min(max_dist, atr_dist))`
   - `chandelier = max(high for last 22 candles) - stop_dist`
   - If `current_price <= chandelier` → `"chandelier"`  
   Lines: trend.py:154–173
3. PSAR (after 48h):
   - If position age > 48 hours, compute PSAR (step 0.015, max AF 0.18).
   - If `current_price <= psar` → `"psar"`  
   Lines: trend.py:166–170
4. EMA death cross: `ema_fast < ema_slow` → `"ema_death_cross"`.  
   Lines: trend.py:175–178
5. Funding drag: `funding_rate > 0.003` → `"funding_drag"`.  
   Lines: trend.py:180–181

**Note:** Trend exits only close trend positions (ownership enforced in loop.py:275–277).

---

## 4. Risk Gates

**Source:** `src/core/perp_risk.py`

### 4.1 Entry Authorization (`allow_entry`)

Returns `(bool, reason)`.  
Lines: perp_risk.py:93–109

1. Drawdown halt: `current_drawdown >= 12.0%` → reject.
2. Position cap: `active_positions >= 3` → reject.
3. Daily loss halt: `daily_loss <= -4.0%` → reject.
4. Leverage halt: `current_leverage >= 3.0x` → reject.

### 4.2 OI Velocity Gate

- Record OI every cycle, keep 48h history.
- `oi_velocity = (latest_oi - earliest_oi) / earliest_oi * 100`
- Reject if `> 15.0%`.  
  Lines: perp_risk.py:113–133

### 4.3 Funding Gate

- Reject if `abs(funding_rate) >= 1.0%` (extreme funding).  
  Lines: perp_risk.py:143–146
- Funding score for longs: `abs(rate) / 0.001` (clamped 0..1).  
  Lines: perp_risk.py:137–141

### 4.4 Leverage Scaling

- Default base leverage = 2.0x.
- If `ATR_pct > 3.0%` → force 1.0x.  
  Lines: perp_risk.py:150–160

### 4.5 Stop Distance

- Majors (BTC, ETH): `2.0 * ATR_pct`
- Alts: `3.0 * ATR_pct`
- Clamp: `[1.5%, 4.0%]`  
  Lines: perp_risk.py:169–181

### 4.6 Position Sizing

Formula:
```
risk_dollars = equity * (1.0 / 100)
max_notional = risk_dollars / (stop_distance_pct / 100)
quantity = max_notional / entry_price
```

Then apply:
1. BTC correlation penalty: `qty *= max(0.1, 1.0 - abs(corr_30))`  
   Lines: perp_risk.py:193–196
2. Linear streak modifier over last 10 trades:
   ```
   phi = max(0.5, min(1.5, 1.0 + (wins - losses) / 10.0))
   qty *= phi
   ```
   Lines: perp_risk.py:198–203
3. Portfolio capacity clamp:
   ```
   remaining = (3.0 * equity) - gross_exposure
   max_qty = remaining / price
   quantity = min(quantity, max_qty)
   ```
   Lines: perp_risk.py:205–208

**Hard confidence threshold:** `confidence >= 0.70` required before any trade opens.  
Line: loop.py:313–314

---

## 5. Altfins Integration

**Source:** `src/adapters/altfins.py`

### 5.1 Screener Data

Fetched every 5 minutes (5 cycles).  
Line: loop.py:167–185

**24 verified indicator fields:**
`RSI14`, `RSI9`, `RSI25`, `ADX`, `SMA50`, `SMA200`, `SHORT_TERM_TREND`, `MEDIUM_TERM_TREND`, `LONG_TERM_TREND`, `ATR`, `TR_VS_ATR`, `OBV_TREND`, `VOLUME_RELATIVE`, `MACD`, `MACD_SIGNAL_LINE`, `MACD_HISTOGRAM`, `STOCH`, `STOCH_RSI`, `WILLIAMS`, `BOLLINGER_BAND_UPPER`, `BOLLINGER_BAND_LOWER`, `ATH_PERCENT_DOWN`, `SHORT_TERM_TREND_CHANGE`, `MEDIUM_TERM_TREND_CHANGE`.

### 5.2 Signal Keys (Tier 1)

Allowed keys:
```
UP_DOWN_TREND
SIGNALS_SUMMARY_STRONG_UP_DOWN_TREND
UP_DOWN_TREND_AND_FRESH_MOMENTUM_INFLECTION
MOMENTUM_UP_DOWN_TREND
FRESH_MOMENTUM_MACD_SIGNAL_LINE_CROSSOVER
EARLY_MOMENTUM_MACD_HISTOGRAM_INFLECTION
MOMENTUM_RSI_CONFIRMATION
SIGNALS_SUMMARY_OVERSOLD_OVERBOUGHT_UP_DOWN
SIGNALS_SUMMARY_OVERSOLD_OVERBOUGHT_MOMENTUM
SIGNALS_SUMMARY_VERY_OVERSOLD_OVERBOUGHT
PULLBACK_UP_DOWN_TREND
SUPPORT_RESISTANCE_BREAKOUT
SUPPORT_RESISTANCE_APPROACHING_OVERSOLD
SIGNALS_SUMMARY_SMA_50_200
SIGNALS_SUMMARY_EMA_12_26
SIGNALS_SUMMARY_TR_ATR_2x
SIGNALS_SUMMARY_TR_ATR_3x
SIGNALS_SUMMARY_BOLLBAND_PRICE_UPPER_LOWER
SIGNALS_SUMMARY_RSI_DIVERGENCE
SIGNALS_SUMMARY_TRADING_RANGE_V2
SIGNALS_SUMMARY_CHANNEL_UP
SIGNALS_SUMMARY_CHANNEL_DOWN
```

Lines: altfins.py:77–100

### 5.3 Ensemble Scoring

- Each signal contributes `confidence * 0.2` to long/short score.
- Composite ensemble signal created if `signal_count >= 3` and `abs(net_score) >= 0.3`.  
  Lines: altfins.py:476–500

### 5.4 Altfins Role in Strategies

- **MR:** looks for `oversold`, `pullback`, `bollinger_touch_lower` keywords.  
  Line: mr.py:110
- **Trend:** looks for `momentum`, `breakout`, `uptrend`, `cross`, `trend`, `channel_up`.  
  Line: trend.py:114
- If matched, confidence *= 1.2 (max 0.95); else *= 0.85.

---

## 6. Signal Tracking / Weighting

**Source:** `src/core/reflect.py`

### 6.1 Decay-Weighted Accuracy

- Exponential decay: `lambda = 0.92` (half-life ~10 trades).  
  Line: reflect.py:29
- `smoothed = 0.92 * prev + 0.08 * outcome`  
  Line: reflect.py:48
- Weight = `max(0.0, min(1.0, smoothed_accuracy))`.  
  Line: reflect.py:61–67

### 6.2 Retirement / Reactivation

- Retired if accuracy < 48% and `n >= 10`.  
  Line: reflect.py:91–92
- Reactivated if accuracy >= 55% and `n >= 50`.  
  Line: reflect.py:88–89

### 6.3 Weekly Reflector

- Runs Sundays 12:00–14:00 UTC if >= 30 trades.  
  Lines: reflect.py:139–171
- Uses Mann-Whitney U test (non-parametric) for parameter bucket comparison.  
  Lines: reflect.py:216–244
- Generates `ParameterSuggestion` objects for human review.

---

## 7. External Intent Contract

**Source:** `src/core/intents.py` (already built)

Intents received from Freqtrade must include:
- `idempotency_key`: unique per signal
- `created_at`: ISO timestamp
- `signal_candle_close`: candle that generated the signal
- `expires_at`: TTL (configurable)
- `source`: e.g. `"freqtrade"`
- `strategy`: e.g. `"HermesPerpStrategy"`
- `asset`, `side`, `confidence`
- `entry_tag`: human-readable entry reason
- `intended_entry_price`, `requested_stop_price`, `requested_leverage`
- `components`: list of local/altfins signal sources used

Hermes validates:
- Not expired
- Not duplicate
- `confidence >= 0.70`
- Risk gates (drawdown, positions, leverage, OI, funding)
- Stop distance in `[1.5%, 4.0%]`
- Leverage clamped by ATR rule
- Portfolio capacity

**Line refs:** loop.py:396–478

---

## 8. Freqtrade Mapping Notes

To make Freqtrade reproduce Hermes decisions faithfully:

1. **Indicators:** Compute exactly the same EMA, RSI, ADX, ATR, Hurst, ER as Hermes.
2. **Regime:** Use the same thresholds (`0.55/0.45` Hurst, `0.60/0.30` ER, `0.03` high-vol, `0.0015` dead-market).
3. **Entry tags:** Use `"mr"` and `"trend"` so Hermes exit ownership works.
4. **Volume:** Compute USD volume as `volume * close`.
5. **Confidence:** Replicate the exact scoring from mr.py:95–131 and trend.py:99–131.
6. **Stops:** Use Hermes `custom_stoploss` logic (2x/3x ATR, clamped).
7. **Exits:** Implement chandelier + PSAR exactly as in trend.py:154–181.
8. **Leverage:** Use Hermes `leverage()` callback (base 2x, 1x if ATR > 3%).
9. **Funding/OI:** Cache historical data and `.shift(1)` before use in backtests.
10. **Altfins:** Do NOT call Altfins inside `populate_indicators`. Read from external cache only.

---

*Generated from Hermes v2 commit 9f42a4c.*
