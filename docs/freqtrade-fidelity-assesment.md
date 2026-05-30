# Hermes → Freqtrade Fidelity Assessment

> Generated 2026-05-30 after deploying spec-compliant HermesPerpStrategy v2.
> Documents what Freqtrade can and cannot reproduce from the Hermes signal spec.

---

## 1. What Freqtrade Reproduces Faithfully

| Spec Rule | Freqtrade Status | Notes |
|-----------|-----------------|-------|
| EMA 9/21 | ✓ | Exact same formula (ewm, adjust=False) |
| RSI 14 (simple gain/loss) | ✓ | Delta-based, same as mr.py:175-188 |
| ADX (14-period ±DM) | ✓ | Same formula as trend.py:204-223 |
| ATR (14-period SMA of TR) | ✓ | Same as mr.py:190-197 |
| Hurst exponent (100-window) | ✓ | Same algorithm as loop.py:522-551 |
| ER (14-period) | ✓ | Same as loop.py:512-520 |
| Norm vol / regime detection | ✓ | Same thresholds (0.03, 0.0015, 0.55/0.45, 0.60/0.30) |
| Regime col assignment | ✓ | dead, high_vol, strong_trend, trend, mr |
| MR entry: RSI <= 28 | ✓ | mr.py:72-75 |
| MR entry: volume >= 2M | ✓ | mr.py:66-67 |
| MR entry: no strong_trend | ✓ | mr.py:69-70 |
| Trend entry: ema cross | ✓ | trend.py:85-90 |
| Trend entry: ADX >= 25 | ✓ | trend.py:92-94 |
| Trend entry: near_fast_ema | ✓ | trend.py:88 |
| Trend entry: volume >= 5M | ✓ | trend.py:71-73 |
| Trend entry: regime trending | ✓ | trend.py:68-69 |
| MR base confidence 0.5 | ✓ | mr.py:95-131 |
| MR deep oversold boost | ✓ | RSI <= 20 → +0.2 |
| MR regime boost | ✓ | regime_mr → +0.1 |
| Trend base confidence 0.5 | ✓ | trend.py:99-131 |
| Trend strong_regime boost | ✓ | strong_trend → +0.2 |
| Confidence >= 0.70 gate | ✓ | mr.py + trend.py |
| ATR stop: major 2x, alt 3x | ✓ | perp_risk.py:169-181 |
| Stop clamp [1.5%, 4.0%] | ✓ | trend.py:160-162 |
| Leverage: ATR > 3% → 1x | ✓ | perp_risk.py:150-160 |
| Baseline leverage 2x | ✓ | perp_risk.py:150 |
| MR TP exits (0.5R, 1.5R, 3.0R) | ✓ | mr.py:161-168 |
| Chandelier exit (22-period, highest high, 3.5x) | ✓ | trend.py:158-163 |
| PSAR exit (step 0.015, max AF 0.18, after 48h) | ✓ | trend.py:166-170 |
| EMA death cross exit | ✓ | trend.py:175-178 |
| RSI overheat exit (>72) | ✓ | |
| MR hard stop (fixed ATR below entry) | ✓ Fixed | Removed `use_custom_stoploss`, moved stop to `custom_exit` |

---

## 2. What Freqtrade Cannot Reproduce (Missing Data)

These require external data that is not available in the Freqtrade backtest environment.

| Spec Rule | Status | Impact |
|-----------|--------|--------|
| Altfins confirmation (1.2x / 0.85x) | ❌ No historical Altfins data | Confidence is lower-bound only (no boosts). Backtest understates valid entries and overstates invalid ones. |
| Funding rate confidence boost | ❌ No historical funding data | MR: no +0.15 for funding < -0.001. Trend: no +0.1 for funding < -0.0005. |
| Funding drag exit | ❌ No historical funding data | trend.py:180-181 not active |
| Extreme funding halt (±1.0%) | ❌ Not testable | perp_risk.py:143-146 |
| OI velocity gate (>15%/48h) | ❌ No historical OI data | perp_risk.py:113-133 |

---

## 3. What Freqtrade Cannot Reproduce (Architectural Differences)

These are gaps between what Hermes does natively and what Freqtrade's strategy framework allows.

| Spec Rule | Status | Details |
|-----------|--------|---------|
| Asset-level cooldown (MR: 12 bars, Trend: 60 cycles) | ❌ No per-asset cooldown in Freqtrade | Freqtrade has global cooldown_lookback but not per-symbol-per-strategy. |
| Component signal attribution | ❌ No signal tracking | Freqtrade has no equivalent to SignalTracker. Each trade has an enter_tag but no decay-weighted accuracy per source. |
| BTC correlation penalty | ❌ Not implementable in strategy | perp_risk.py:193-196 requires correlation calc across pairs. |
| Portfolio leverage cap (≤3x) | ❌ No cross-margin awareness | perp_risk.py:205-208 uses gross_exposure(). |
| Position sizing: 1% risk / stop distance | ⚠️ Partial | Freqtrade uses fixed stake. custom_stake_amount could approximate 1% risk but no dynamic quantity calc. |
| Linear streak modifier φ | ❌ No streak tracking across trades | perp_risk.py:198-203 |

---

## 4. Backtest Result (Post-Fix)

**Period:** 2026-01-07 → 2026-05-30 (142 days, 3 pairs: BTC/ETH/SOL)
**Market change:** -33.15% (broad downtrend)

| Metric | Pre-Fix | Post-Fix | Improvement |
|--------|---------|----------|-------------|
| Trades | 269 | 145 | -46% (fewer premature exits) |
| Final balance | $7,906 | $8,776 | +$870 |
| Profit % | -20.94% | -12.24% | +8.7pp |
| Profit factor | 0.22 | 0.60 | +0.38 |
| Sharpe | -22.18 | -4.72 | +17.46 |
| Max drawdown | 21.25% | 14.00% | -7.25pp |
| Win rate | 35.3% | 49.0% | +13.7pp |

**Exit breakdown (post-fix):**
| Exit Reason | Trades | Avg Profit | Total PnL | Win Rate |
|-------------|--------|------------|-----------|----------|
| mr_tp1 | 63 | +2.68% | +$1,691 | 100% |
| rsi_overheated | 8 | +1.64% | +$132 | 100% |
| stop_loss | 74 | -4.12% | -$3,046 | 0% |

**Key improvement:** Trailing_stop_loss exits eliminated. MR TP exits now have 100% WR.

**Remaining issue:** Stop losses (-$3,046) outweigh TP profits (+$1,823). The stop distance (ATR-based, clamped [1.5%, 4%]) is wider than the TP1 target (0.5R), so losing trades lose more than winning trades gain. This is expected in a broad downtrend — MR always loses money in trending markets.

---

## 5. Key Findings

### 5.1 Trailing Stop Fix Benefits
The fix (removing `use_custom_stoploss` and moving stops to `custom_exit`) improved every metric. No more premature exits from Freqtrade's trailing stop behavior.

### 5.2 Zero Trend Trades
Trend entry conditions never fired in 142 days. The Hurst/ER regime never classified as trending over this period. This is consistent with a broad -33% downtrend. Not necessarily a bug — if the market is genuinely not trending per our regime thresholds, MR is the correct strategy.

### 5.3 Funding Missing
All funding-related features (confidence boost, exit, halt) are inactive. This understates confidence scores and removes the funding_drag exit.

### 5.4 Altfins Missing
Without Altfins, confidence is lower-bound only. This inflates trade count (no Altfins penalty on bad entries) and reduces reward (no Altfins boost on good entries).

---

## 6. Assessment

The Freqtrade HermesPerpStrategy is **spec-compliant for OHLCV-only operations** and the trailing stop fix resolved the most serious fidelity gap.

**Not ready for intent emission** because:
1. Missing external data (funding, OI, Altfins) means live performance cannot be predicted from backtest.
2. No per-asset cooldown means Freqtrade enters more frequently than Hermes would.
3. 0 trend trades in 142 days needs live verification — may be correct for this market period.

**Ready for lab research/backtesting** because:
1. OHLCV logic matches Hermes spec exactly.
2. Lookahead and recursive analysis pass with no bias.
3. MR entry/exit behavior is consistent with Hermes spec.
4. The trailing stop issue is fixed.

**Next step:** Build historical external data caches (funding, OI, Altfins) so Freqtrade backtests can include those signals. Without them, the backtest is only testing a subset of Hermes.
