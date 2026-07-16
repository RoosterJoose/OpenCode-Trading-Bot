# Hermes Trading Bot

Multi-bot paper trading system implementing cross-sectional momentum, 4-hour trend, and 5-minute fade strategies across Coinbase CDE perpetuals and SMA200 spot. Three independent bots run simultaneously: Conservative (standard params), Aggressive (relaxed entry gates), and SMA200 Spot (daily macro trend).

## Architecture

### Bot Services

Three systemd services run on an Oracle Cloud VPS (Ubuntu):

| Service | Type | Description |
|---|---|---|
| `hermes-bot` | oneshot → long-running | Conservative perp bot (0.70 min confidence) |
| `hermes-bot-aggressive` | oneshot → long-running | Aggressive perp bot (0.55 min confidence) |
| `hermes-dashboard` | long-running | Web UI on port 8081 |
| `hermes-sma200.service` + `.timer` | daily 02:00 UTC | SMA200 spot strategy |

### Directory Layout

```
/opt/hermes-trading-bot/           # Conservative bot
├── src/
│   ├── core/
│   │   ├── loop.py               # Main trading loop
│   │   ├── perp_risk.py           # Risk manager (leverage, stops, drawdown)
│   │   ├── ic_allocator.py        # Information Coefficient budget allocation
│   │   ├── event_kill.py          # ForexFactory macro-event kill switch
│   │   └── types.py               # Shared type definitions
│   ├── strategies/
│   │   ├── xs_momentum.py         # Cross-sectional momentum (primary)
│   │   ├── trend_4h.py            # 4-hour Donchian breakout
│   │   └── fade_5m.py             # 5-minute volatility fade
│   ├── adapters/
│   │   ├── base.py                # Exchange adapter abstraction
│   │   ├── coinbase_advanced.py   # Coinbase CDE perp adapter
│   │   └── kalshi.py              # Kalshi perp adapter (dormant)
│   ├── store/
│   │   └── sqlite.py              # SQLite persistence
│   └── dashboard/
│       └── server.py              # Web dashboard (3-page tabbed UI)
├── scripts/
│   ├── deep_audit.py              # AST-level nesting verification
│   ├── verify_method_calls.py     # Static call verification
│   ├── daily_reflection.py        # 00:05 UTC daily analysis
│   ├── weekly_reflection.py       # Sunday 15:00 UTC weekly
│   ├── daily_recs.py              # 00:15 UTC recommendations
│   ├── sharpe_tracker.py          # Rolling Sharpe / Sortino tracker
│   ├── strategy_budget.py         # Historical budget seeding
│   ├── backfill_candles.py        # Paginated candle backfill
│   ├── historical_backtest.py     # Multi-strategy backtest
│   └── deploy.sh                  # Full deployment pipeline
├── data/
│   ├── hermes.db                  # CONS bot SQLite DB
│   ├── sma200_state.db            # SMA200 spot DB
│   └── external_snapshot.json     # Exchange-agnostic data export
├── .env                           # API keys (chmod 600)
└── deploy.sh                      # One-command deploy

/opt/hermes-trading-bot-aggressive/  # Aggressive bot (same structure)
└── data_aggressive/
    └── hermes.db                   # AGGR bot SQLite DB
```

### Data Flow

```
Coinbase CDE REST API
  ├── candle fetches (all 23 assets, 3 timeframes)
  │   ├── 1h (300 bars) → XS momentum evaluation
  │   ├── 4h (300 bars) → Trend4h evaluation  
  │   └── 5m (100 bars) → Fade5m evaluation
  ├── funding rates → OI velocity calculation
  ├── bid/ask spreads → spread gate
  └── order placement (paper:local; live:JWT POST)

Every 60-second cycle:
  1. Fetch candles (semaphore-limited to 5 concurrent fetches)
  2. Compute 7-day returns for XS ranking
  3. Evaluate each strategy per asset
  4. Apply risk gates (leverage, drawdown, concentration, OI, spread)
  5. Execute entry/exit if confidence ≥ threshold
  6. Save state to SQLite
  7. Check invariants & self-heal conditions
```

## Strategies

### 1. Cross-Sectional Momentum (xs_momentum.py)
**Primary sleeve.** Ranks all 23 assets by 7-day total return, enters long on top 3 and short on bottom 3.

- **Entry**: `confidence = 0.50 + 0.15*rank_factor + 0.10*magnitude_factor` where `magnitude_factor = min(|ret_7d| / 0.02, 2.0)`
- **Exit**: 3% stop loss, 5% profit target, or portfolio sweep
- **Long gate**: requires `ret_7d > -0.03` (top asset not a catastrophic loser)
- **Short gate**: requires `ret_7d < -0.01` (clear underperformance)
- **Confidence clamp**: [0.50, 0.90]
- **Cooldown**: 60 cycles after stop loss
- **Blocked assets** (backtest-proven negative edge): ZEC, AAVE, ADA

### 2. 4-Hour Trend (trend_4h.py)
Macro trend following with Donchian breakout confirmation on 4-hour candles.

- **Bias filter**: EMA50 on 4h data (long only above, short only below)
- **Entry**: 20-bar Donchian channel breakout with OI velocity confirmation
- **Exit**: Chandelier trail (4.0x ATR majors, 5.0x ATR alts) + 30-day time exit
- **Sizing**: 60% budget allocation

### 3. 5-Minute Fade (fade_5m.py)
Mean reversion on liquidation wicks. Enters opposite direction of 3σ+ deviations from 20-period SMA.

- **Entry**: Z-score ≥ 3.0σ (majors) or 3.5σ (alts) from 20-SMA, fade the direction
- **Order type**: LIMIT (passive, opposite side of wick)
- **Exit**: 1.0x ATR stop, 2.0x ATR target, 2-hour time exit
- **Note**: Requires 5m candle history > 100 bars to evaluate

### 4. SMA200 Spot (sma200_runner.py)
Daily macro trend on Coinbase spot (no leverage).

- **Entry**: Daily close > 200-period SMA
- **Exit**: Daily close < 200-period SMA or 30-day time exit
- **Risk**: 1% per trade, 3 max concurrent, 1.0x leverage (spot)
- **Run**: Oneshot via systemd timer at 02:00 UTC daily
- **Targets**: BTC, ETH, SOL
- **DB**: Separate `sma200_state.db`

### Strategy Removal History

| Strategy | Removed | Reason |
|---|---|---|
| MeanReversion (mr.py) | Phase 1 | 0.00 avg R across 496 trades |
| DonchianBreakout (donchian.py) | Phase 1 | -$66 on 12 trades, 8% WR |
| TrendFollow (trend.py, EMA cross) | Phase 2 | Replaced by trend_4h |
| DriftMomentum | Phase 3 | Eaten by xs_momentum |

## Risk Framework

All risk gates are hardcoded in `perp_risk.py`:

```
GLOBAL:
  1% risk per trade              (risk_dollars = equity × 0.01)
  3.0x max portfolio leverage    (sum notional / equity ≤ 3.0)
  4% daily drawdown halt         (stops all entries for the day)
  3 max concurrent positions     (portfolio-wide)
  Global loss streak ≥ 5         (blocks all entries, auto-clears after 120 cycles w/o entry)

PER-POSITION:
  Stop distance: ATR × multiplier (majors 2.0x, alts 3.0x; clamped [0.3%, 8.0%])
  Position size: risk_dollars / stop_distance
  BTC correlation penalty: size × (1 − |ρ₍30₎|)
  OI velocity gate: blocks if OI > 15% / 48h
  Spread gate: blocks if bid/ask > threshold × ATR
  Concentration halt: any single position > 50% equity
  Max age: 12 hours (any position)
  Stale exit: 60 min with PnL ≤ $0
  Peak decay: exit if PnL decays >50% from peak (requires peak ≥ $3)
```

### Portfolio Sweep (loop.py:815-850)

Every cycle, the sweep checks each position:
1. **Knife guard time exit**: 60-min forced close for longs in bear market
2. **Max age**: 12 hours absolute limit
3. **Peak decay**: profitable position dropped >50% from peak (minimum $3 peak)
4. **Stale**: 60+ minutes with PnL ≤ $0

Sweep-exempt: positions that hit TP1 and have Chandelier trailing (tp1_scaled flag).

### Circuit Breakers (self-heal)

Four self-healing mechanisms automatically recover from failure modes:

1. **Entry stall → restart**: If no ENTRY_DIAG for 20+ consecutive cycles, bot exits with code 42 (systemd auto-restarts)
2. **Block reason dominance**: If >80% of entry blocks are the same reason for 60+ cycles, auto-clear the blocking state
3. **Loss streak stale**: If global_loss_streak ≥ 5 with no new entries for 120+ cycles, reset to 0
4. **Memory leak prevention**: `_block_reasons` dict auto-clears every 60 cycles

## Dashboard

Three independent pages served from `src/dashboard/server.py`:

| Path | Bot | API Base |
|---|---|---|
| `/` or `/cons` | Conservative | `/api/status`, `/api/trades`, ... |
| `/aggressive` | Aggressive | `/api/aggressive/status`, ... |
| `/spot` | SMA200 Spot | `/api/sma200/status`, ... |

Architecture: **No JS state sharing**. Each page is a standalone HTML file with bot-specific API prefixes injected server-side via `__BOT__` placeholder replacement. Tab bar uses `<a>` links (full page reload).

### API Endpoints

| Endpoint | Return |
|---|---|
| `GET /api/status` | CONS equity, trades, WR, PF, Sharpe, exposure |
| `GET /api/trades` | Last 50 CONS trades |
| `GET /api/positions` | Current CONS positions |
| `GET /api/equity` | CONS equity history (last 500 snaps) |
| `GET /api/markets` | Market data (funding, OI, prices) |
| `GET /api/signals` | Last 50 detected signals |
| `GET /api/compare` | AGGR equity, trades, WR, PF |
| `GET /api/aggressive/status` | AGGR status (same fields as /api/status) |
| `GET /api/aggressive/{trades,positions,equity}` | AGGR data |
| `GET /api/sma200/status` | SPOT equity, trades, WR |
| `GET /api/sma200/{trades,positions}` | SPOT data |
| `GET /api/daily-reflection` | Daily reflection report |
| `GET /api/health` | Daily health check |
| `GET /api/learnings` | Cumulative learnings |
| `GET /api/readiness` | Paper-to-live readiness check |
| `GET /api/ab-reflection` | A/B comparison report |

## State Keys (SQLite)

Persistent state stored in each bot's `state` table:

| Key | Value |
|---|---|
| `paper_equity` | Current equity balance |
| `paper_peak_equity` | All-time peak equity |
| `positions` | JSON array of current positions |
| `strategy_budget` | IC allocator strategy weights |
| `total_trades` | Running trade count |
| `daily_signals` | Last cycle's signals |
| `weekly_reflection` | Weekly analysis report |
| `daily_reflection` | Daily analysis report |
| `ab_comparison` | A/B comparison snapshot |
| `paused_strategies` | Per-strategy pause flags |
| `daily_health` | Health check results |
| `dynamic_thresholds` | Closed-loop learner adjustments |

## Infrastructure

### Deployment

```
curl -sL https://github.com/RoosterJoose/OpenCode-Trading-Bot/raw/master/deploy.sh | sudo bash
```

Deploy pipeline (from `deploy.sh`):
1. `verify_method_calls.py` — AST method-call verification on all .py files
2. Git commit + push CONS
3. Git pull AGGR
4. Clear `__pycache__`
5. Compile-check both bots
6. `deep_audit.py` (36 checks, aborts on failure)
7. Restart both bots
8. Post-deploy invariant sweep

### Environment Variables

File: `/opt/hermes-trading-bot/.env` (chmod 600, owner hermes)

```
HERMES_COINBASE__API_KEY_ID=<cdp_org_key>
HERMES_COINBASE__PRIVATE_KEY=<ec_private_key_with_newlines>
HERMES_TELEGRAM_BOT_TOKEN=<bot_token>
HERMES_TELEGRAM_CHAT_ID=<chat_id>
ALTFINS_API_KEY=<key1>
ALTFINS_API_KEY_2=<key2>
```

### Systemd Files

- `/etc/systemd/system/hermes-bot.service` — CONS bot (long-running, auto-restart)
- `/etc/systemd/system/hermes-bot-aggressive.service` — AGGR bot
- `/etc/systemd/system/hermes-dashboard.service` — Web UI (port 8081)
- `/etc/systemd/system/hermes-sma200.service` — SMA200 oneshot
- `/etc/systemd/system/hermes-sma200.timer` — Daily 02:00 UTC

### Timer Schedule

| Timer | Time | Purpose |
|---|---|---|
| `hermes-bot` | continuous | Every 60s cycle |
| `hermes-sma200.timer` | 02:00 UTC daily | SMA200 evaluation |
| `hermes-daily-reflection` | 00:05 UTC | Daily missed-move analysis |
| `hermes-weekly-reflection` | 15:00 UTC Sunday | Weekly parameter review |
| `hermes-sma200.timer` | 02:00 UTC daily | SMA200 evaluation |
| daily recs | 00:15 UTC | Strategy recommendations |

## Known Constraints

### Exchange Limitations
- Coinbase CDE max 300 bars per candle request (regardless of interval)
- Historical backfill requires repeated reverse-chronological pagination
- CDE volume field is in contract units, not dollars (volume gate disabled)
- Coinbase Advanced Trade API rate limits: ~6 requests/second across all endpoints

### Strategy Limitations
- XS momentum on 23 assets is near the minimum viable universe (rank-based edge is weak)
- All intraday strategies (XS, Trend4h, Fade5m) show avg R ≈ 0 on 1h crypto data
- No intraday strategy has achieved Sharpe > 0.6 in live paper trading
- Fade5m requires > 100 5-minute candle history to compute meaningful z-scores
- Kalshi adapter is implemented but dormant (no edge demonstrated vs Coinbase)

### What Works (verified edge)
- BTC SMA200 daily: Sharpe 1.12 on 4.4 years, PF 2.26, 41% WR
- ETH SMA200 daily: Sharpe 1.07, PF 2.17, 48.4% WR
- SOL SMA200 daily: Sharpe 0.78, PF 1.78, 34.3% WR
- Portfolio SMA200 (equal weight): Sharpe 1.29, PF 2.00
- These strategies have NOT been adapted to the SMA200 runner (pending price recovery above SMA200)

## API Key Rotation

### Coinbase API Keys
JWT-based auth with `kid` header required for CDP API v3 org keys. The key includes:
```
{
  "iss": "cdp",
  "sub": <api_key_id>,
  "nbf": <now>,
  "exp": <now + 120>,
  "uri": "POST api.coinbase.com/api/v3/brokerage/orders"
}
```
Header: `{"kid": <api_key_id>, "nonce": <16_byte_hex>}`

Private key must have `\n` sequences converted to actual newlines before JWT encoding (handled in `_coerce()` in main.py).

### Altfins API Keys
Two API keys alternate on quota exhaustion (HTTP 429 detection). Each key has 466 monthly permits. At the configured schedule:
- Screener: every 192 cycles (~3h12m)
- Permit check: every 360 cycles (6h)
- Total: ~465 calls/bot/month → clean end-of-month exhaustion

## Trade Intent Queue

Supports Freqtrade integration via `trade_intents` SQLite table:

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| asset | TEXT | Asset symbol |
| direction | TEXT | "long" or "short" |
| confidence | REAL | 0.0-1.0 |
| entry_price | REAL | Suggested entry |
| stop_price | REAL | Suggested stop |
| reason | TEXT | Strategy name |
| idempotency_key | TEXT UNIQUE | Dedup key |
| expires_at | INTEGER | Unix timestamp |
| status | TEXT | pending/accepted/rejected |
| created_at | INTEGER | Unix timestamp |

Gates: ipso money confidence ≥ 0.70, stop distance ≤ compute_leverage(max), projected portfolio leverage ≤ 3.0x, funding/extreme gate pass, strategy not paused.

## Git Workflow

Both CONS and AGGR point to the same GitHub repo (`github.com:RoosterJoose/OpenCode-Trading-Bot.git`). After any code change:
1. Commit CONS (`git add -A && git commit && git push`)
2. Reset/pull AGGR (`git reset --hard origin/master` or `git pull --rebase`)
3. Restart both bots

Changes made via deploy scripts (`deploy.sh`) automatically handle the dual-deploy.
