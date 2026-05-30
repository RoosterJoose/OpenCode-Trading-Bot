# AltfINS API Signal Analysis — Hermes v2 Verification

## Current State
Bot fetches: RSI14, RSI9, ADX, SMA50, SMA200, Short/Med/Long Term Trend from screener
Bot uses: AltfINS signals feed as ensemble source with decay-weighted accuracy tracking

## Proposed Signal Key Subscription (~25 keys)

### Tier 1 — MUST INTEGRATE (directional, trend-sensitive, actionable)

**Trend + Momentum (trendSensitive=true):**
- `UP_DOWN_TREND` — multi-timeframe trend direction
- `SIGNALS_SUMMARY_STRONG_UP_DOWN_TREND` — strong across ST/MT/LT
- `UP_DOWN_TREND_AND_FRESH_MOMENTUM_INFLECTION` — trend + MACD inflection
- `MOMENTUM_UP_DOWN_TREND` — MACD crossover in trend context
- `FRESH_MOMENTUM_MACD_SIGNAL_LINE_CROSSOVER` — MACD signal line cross
- `EARLY_MOMENTUM_MACD_HISTOGRAM_INFLECTION` — early momentum shift
- `MOMENTUM_RSI_CONFIRMATION` — MACD cross + RSI > 50/<50

**Oversold/Overbought in Context (trendSensitive=true):**
- `SIGNALS_SUMMARY_OVERSOLD_OVERBOUGHT_UP_DOWN` — RSI in trend context
- `SIGNALS_SUMMARY_OVERSOLD_OVERBOUGHT_MOMENTUM` — oversold + momentum
- `SIGNALS_SUMMARY_VERY_OVERSOLD_OVERBOUGHT` — extreme RSI (<25 or >75)
- `PULLBACK_UP_DOWN_TREND` — buy dip / sell bounce

**Support/Resistance (trendSensitive=true):**
- `SUPPORT_RESISTANCE_BREAKOUT` — breakout signal
- `SUPPORT_RESISTANCE_APPROACHING_OVERSOLD` — S/R + RSI confluence
- `SUPPORT_RESISTANCE_APPROACHING` — approaching key level

**Crossover (trendSensitive=true):**
- `SIGNALS_SUMMARY_SMA_50_200` — golden/death cross
- `SIGNALS_SUMMARY_EMA_12_26` — MACD line cross
- `SIGNALS_SUMMARY_PRICE_EMA_9_12` — fast price vs EMA
- `SIGNALS_SUMMARY_PRICE_SMA_5_10` — short-term price vs SMA

**Volatility (trendSensitive=false but regime-relevant):**
- `SIGNALS_SUMMARY_TR_ATR_2x` — moderate volatility increase
- `SIGNALS_SUMMARY_TR_ATR_3x` — significant volatility increase
- `SIGNALS_SUMMARY_BOLLBAND_PRICE_UPPER_LOWER` — Bollinger touch

**Divergence (high value reversal signal):**
- `SIGNALS_SUMMARY_RSI_DIVERGENCE` — RSI divergence

**Reversal Candlestick (high-conviction specific):**
- `SIGNALS_SUMMARY_HAMMER`
- `SIGNALS_SUMMARY_ENGULFING`

### Tier 2 — SKIP FOR NOW (can add later)

**Harmonic patterns (Gartley, Butterfly, ABCD)**
→ Need manual chart confirmation, unreliable in automated perp trading

**Fundamental signals (TVL, Mcap/Revenue, Protocol Revenue)**
→ We trade perps, don't invest on fundamentals

**Top Gainers/Top Losers**
→ Lagging, backward-looking

**Weak candlestick (Doji, Spinning Top, Harami, Tweezer, Kicker, etc.)**
→ Single-candle noise in crypto markets

**ATH proximity signals (Within 5% of ATH, Recent ATH)**
→ Non-directional, don't tell us to go long or short

**Pattern-based (Rising/Falling Wedge, Rectangle, Pennant, Flag, Triangle)**
→ AI pattern detection generates false signals on crypto

**Multi-candle patterns (Three White Soldiers, Three Black Crows, Three Inside, Three Outside, Morning/Evening Doji Star, Abandoned Baby, Three Line Strike)**
→ Too many candles = slow signal, already moved before we get it

**Channel patterns (Channel Up, Channel Down)**
→ Already detected by our EMA trend following

**Local High/Low patterns**
→ Redundant with our own swing detection

**Trading Range signals**
→ Already detected by ADX < 20

**MA Ribbon**
→ Redundant with crossover signals

## Proposed Expanded Screener Fields

Currently​: RSI14, RSI9, ADX, SMA50, SMA200, ST_TREND, MT_TREND, LT_TREND

Add:
- `ATR` — cross-validate our local ATR
- `TR_VS_ATR` — current range vs ATR (volatility regime)
- `OBV_TREND` — volume confirmation divergence
- `VOLUME_RELATIVE` — RVOL spike detection
- `MACD` — MACD line value
- `MACD_SIGNAL_LINE` — signal line for cross detection
- `MACD_HISTOGRAM` — bar rising/falling for early momentum
- `STOCH` — stochastic oscillator
- `STOCH_RSI` — stochastic RSI combo
- `WILLIAMS` — Williams %R
- `BOLLINGER_BAND_UPPER` — upper band distance
- `BOLLINGER_BAND_LOWER` — lower band distance
- `ATH_PERCENT_DOWN` — % from ATH (risk metric)
- `SHORT_TERM_TREND_CHANGE` — trend shift detection
- `MEDIUM_TERM_TREND_CHANGE` — trend shift detection

## Historical Analytics for Walk-Forward

Pull historical data for: RSI14, MACD_HISTOGRAM, ADX, ATH_PERCENT_DOWN
→ Feed into our walk-forward optimizer alongside Hyperliquid candle data

## Verification Questions for NotebookLM

1. Are the Tier 1 signal key selections reasonable for a perp futures bot (5m-1h timeframe)?
2. Are there any signals in Tier 2 or 3 that should be moved to Tier 1?
3. Is any Tier 1 signal likely to generate excessive false positives on crypto?
4. Should I add/remove from the expanded screener field list?
5. Is the RSI_DIVERGENCE signal worth the API cost vs computing locally?
6. Should any signal have specific accuracy decay weighting adjustments?
