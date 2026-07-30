# Hermes Trading Bot -- Complete Handoff V2
Generated 2026-07-27 01:30 UTC

### CONS: equity=$4876 peak=$5043 positions=3
### AGGR: equity=$4696 peak=$5059 positions=3

## Strategy Performance (Full History)

### CONS
  HermesPerpStrategy    1t  WR=100.0%  PnL=$   +9.71  avgR=+0.00
  donchian          12t  WR=  8.3%  PnL=$  -66.32  avgR=-0.03
  fade_5m            5t  WR= 60.0%  PnL=$   -9.21  avgR=-0.01
  manual             3t  WR= 66.7%  PnL=$   +3.93  avgR=+0.00
  mr               500t  WR= 44.8%  PnL=$ +240.75  avgR=-0.01
  sma200_perp       31t  WR= 58.1%  PnL=$ -100.11  avgR=-0.08
  trend            197t  WR= 51.8%  PnL=$+1151.24  avgR=-0.01
  trend_4h           1t  WR=  0.0%  PnL=$   -2.51  avgR=-0.03
  xs_momentum      176t  WR= 50.6%  PnL=$ +325.42  avgR=+0.03
### AGGR
  donchian          15t  WR= 13.3%  PnL=$ -511.04  avgR=-0.05
  drift_momentum     4t  WR= 25.0%  PnL=$ +358.88  avgR=-0.04
  fade_5m            6t  WR= 50.0%  PnL=$   -9.77  avgR=-0.01
  manual             1t  WR=  0.0%  PnL=$ -427.41  avgR=+0.00
  mr               332t  WR= 44.0%  PnL=$ +501.70  avgR=-0.01
  sma200_perp       28t  WR= 42.9%  PnL=$ -275.46  avgR=-0.25
  trend              4t  WR= 25.0%  PnL=$ -106.42  avgR=-0.03
  trend_4h           1t  WR=  0.0%  PnL=$   -2.16  avgR=-0.03
  xs_momentum      183t  WR= 47.5%  PnL=$+1820.34  avgR=-0.17

## Post-Reset Performance (Jul 11+)

### CONS
  xs_momentum     115t  PnL=$  +10.42  avgR=+0.06  WR=50.4%
  HermesPerpStrategy   1t  PnL=$   +9.71  avgR=+0.00  WR=100.0%
  trend_4h          1t  PnL=$   -2.51  avgR=-0.03  WR=0.0%
  fade_5m           5t  PnL=$   -9.21  avgR=-0.01  WR=60.0%
  mr               15t  PnL=$  -21.91  avgR=-0.00  WR=33.3%
  trend             5t  PnL=$  -69.70  avgR=-0.01  WR=60.0%
  sma200_perp      31t  PnL=$ -100.11  avgR=-0.08  WR=58.1%
### AGGR
  mr               17t  PnL=$  +93.67  avgR=+0.01  WR=29.4%
  trend_4h          1t  PnL=$   -2.16  avgR=-0.03  WR=0.0%
  fade_5m           6t  PnL=$   -9.77  avgR=-0.01  WR=50.0%
  xs_momentum     128t  PnL=$  -78.96  avgR=-0.25  WR=43.0%
  trend             3t  PnL=$  -96.52  avgR=-0.02  WR=33.3%
  sma200_perp      28t  PnL=$ -275.46  avgR=-0.25  WR=42.9%

## Bug Catalog

1. R-multiple SHORT inverted (Jul 15) -- 323 trades wrong sign
2. AGGR DB path to CONS (Jul 14) -- ic_allocator + closed_loop both wrong
3. equity_snapshots vs paper_equity (persistent)
4. daily_start_equity=$10k hardcoded (Jul 12) -- false -49.8% halt
5. Stale WR halt persisted across restarts (Jul 10, 4 sessions)
6. AGGR paper_equity=$11,273 (Jul 16) -- 2.25x sizing all night
7. paused_strategies missing from AGGR DB (Jul 21, 5th time) -- -$125 rogue longs
8. Peak decay killing SMA200 positions (Jul 21) -- $253/day churn
9. XS hour-gate shared counter (Jul 13) -- only 2/23 got XS
10. XS confidence formula multiplicative (Jul 15) -- no longs passed
11. Stale pycache 48h (Jul 18-19)
12. git checkout wiped uncommitted loop.py (Jul 10, 11, 14 -- 3x)
13. Dashboard Promise.all race condition (Jul 14)
14. Two-bot divergence (6+ occurrences)
15. Kalshi dead code (never connected, silent failure since May)

## Open Positions

CONS: $4672  3 positions
  DOT    short uP=$+20.88
  SHIB   short uP=$-233.21
  LTC    short uP=$+8.65
AGGR: $4799  3 positions
  DOT    short uP=$+60.55
  SUI    short uP=$+40.71
  HBAR   short uP=$+1.78

## Dead Code
- _hyperliquid_deprecated.py
- kalshi.py
- trend.py
- momentum.py
- sma200_runner.py
- reflect.py
- ic_allocator.py
- intents.py
- scripts/closed_loop.py
- scripts/submit_intent.py
- scripts/daily_recs.py
