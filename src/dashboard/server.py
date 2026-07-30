"""Dashboard — lightweight HTTP server for monitoring.
Uses only stdlib (http.server + sqlite3). No extra dependencies.

Endpoints:
  /               — HTML dashboard (CONS)
  /spot           — HTML dashboard (SMA200 perp)
  /api/status     — Bot health, equity, risk metrics
  /api/trades     — Recent closed trades
  /api/positions  — Open positions
  /api/equity     — Equity history for chart
  /api/signals    — Recent signal log
  /api/reflection — Weekly reflection report
  /api/governor   — RiskGovernor state
  /api/sma200/kill     — SMA200 kill state
  /api/sma200/regime   — Per-asset regime scores
"""

import json, os, sqlite3, sys, time, urllib.request, math
from functools import lru_cache
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

_repo = Path(__file__).resolve().parent.parent.parent
ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
          "AAVE", "LTC", "NEAR", "SUI", "BNB", "XLM", "HBAR", "BCH", "ZEC", "PEPE", "SHIB"]
_MARKET_CACHE = {"ts": 0.0, "data": []}

def _fmt(n, decimals=2):
    try:
        return round(float(n), decimals)
    except:
        return 0.0

def _read_state(conn, key, default=None):
    try:
        r = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if r:
            return json.loads(r[0])
    except:
        pass
    return default

def _connect_db(path):
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _get_equity(conn):
    """Read equity from equity_snapshots (most accurate)."""
    r = conn.execute("SELECT equity, peak_equity FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if r:
        return float(r["equity"]), float(r["peak_equity"])
    # Fallback: paper_equity state key
    try:
        pe = _read_state(conn, "paper_equity", 5000)
        return float(pe) if pe else 5000.0, float(pe) if pe else 5000.0
    except:
        return 5000.0, 5000.0

def _get_bot_stats(conn):
    """Full stats snapshot from a bot DB."""
    stats = {"equity": 5000, "peak_equity": 5000, "total_trades": 0, "win_rate": 0,
             "profit_factor": 0, "sharpe": 0, "sortino": 0, "gross_exposure": 0,
             "effective_leverage": 0, "daily_pnl_pct": 0, "drawdown_duration_hours": 0,
             "allow_entry": True, "positions": 0, "sma200_killed": False, "sma200_kill_pnl": 0}
    try:
        eq, peak = _get_equity(conn)
        stats["equity"] = _fmt(eq)
        stats["peak_equity"] = _fmt(peak)

        # Daily PnL
        day = conn.execute("SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 1 OFFSET 1440").fetchone()
        if day and day["equity"]:
            stats["daily_pnl_pct"] = _fmt((eq - float(day["equity"])) / float(day["equity"]) * 100)

        # Positions
        pos = _read_state(conn, "positions", [])
        stats["positions"] = len(pos)
        gross = sum(abs(float(p.get("entry_price", 0)) * abs(float(p.get("size", 0)))) for p in pos)
        stats["gross_exposure"] = _fmt(gross)
        stats["effective_leverage"] = _fmt(gross / eq) if eq else 0

        # Trades
        trades = conn.execute("SELECT pnl_dollars, pnl_pct FROM trades").fetchall()
        stats["total_trades"] = len(trades)
        if trades:
            wins = [t["pnl_dollars"] for t in trades if t["pnl_dollars"] > 0]
            losses = [t["pnl_dollars"] for t in trades if t["pnl_dollars"] < 0]
            stats["win_rate"] = _fmt(len(wins) / len(trades), 4) if trades else 0
            total_loss = abs(sum(losses)) if losses else 1
            stats["profit_factor"] = _fmt(sum(wins) / total_loss, 2) if total_loss > 0 else 0

            # Sharpe/Sortino on last 100
            last_100 = list(trades)[-100:]
            if len(last_100) >= 5:
                returns = [float(t["pnl_pct"] or 0) for t in last_100]
                m = sum(returns) / len(returns)
                s = math.sqrt(sum((r - m) ** 2 for r in returns) / len(returns))
                stats["sharpe"] = _fmt((m / s) * math.sqrt(365), 2) if s > 0 else 0
                neg = [r for r in returns if r < 0]
                if neg:
                    dd = math.sqrt(sum(r * r for r in neg) / len(neg))
                    stats["sortino"] = _fmt((m / dd) * math.sqrt(365), 2) if dd > 0 else 0
                else:
                    stats["sortino"] = stats["sharpe"]

        # SMA200 kill state
        kill = _read_state(conn, "sma200_killed", {})
        stats["sma200_killed"] = kill.get("value", False)
        stats["sma200_kill_pnl"] = kill.get("pnl_28d", 0)

        # Drawdown duration
        snaps = conn.execute("SELECT equity, timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 100").fetchall()
        if snaps and peak > 0:
            for i, s in enumerate(snaps):
                if float(s["equity"]) >= peak:
                    stats["drawdown_duration_hours"] = _fmt(i * (1.0 / 60.0), 1)
                    break

        # Altfins / snapshot
        sp = Path(str(_repo) + "/data/external_snapshot.json")
        if sp.exists():
            snap = json.loads(sp.read_text())
            stats["altfins_permits"] = snap.get("altfins_permits", {})
            stats["altfins_signal_count"] = snap.get("altfins_signal_count", 0)
    except Exception as e:
        stats["error"] = str(e)
    return stats

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Trading Bot</title>
<style>
  :root{--bg:#030914;--panel:#07111f;--card:#0b1524;--card2:#0e1a2b;--border:#18304c;--muted:#8495ad;--text:#e8f1ff;--soft:#aebbd0;--cyan:#10e6ff;--blue:#1d9bff;--purple:#7d3cff;--green:#39f07a;--red:#ff5d63;--amber:#ffb11a;--shadow:rgba(0,0,0,.45)}
  *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 88% -5%,rgba(125,60,255,.30),transparent 22%),radial-gradient(circle at 7% 4%,rgba(16,230,255,.18),transparent 24%),linear-gradient(135deg,#020712,#071321 55%,#020710);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,sans-serif;min-height:100vh}
  .app{display:grid;grid-template-columns:246px 1fr;min-height:100vh}
  .side{position:sticky;top:0;height:100vh;padding:24px 18px;border-right:1px solid rgba(36,62,96,.55);background:linear-gradient(180deg,rgba(3,10,20,.96),rgba(2,8,15,.90))}
  .brand{display:flex;align-items:center;gap:12px;margin-bottom:28px}.brand h1{margin:0;font-size:17px;letter-spacing:3px}.brand span{font-size:9px;color:var(--muted);letter-spacing:1px}
  .nav{display:grid;gap:4px}.nav a{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:10px;color:var(--soft);text-decoration:none;font-size:13px;font-weight:500}.nav a.active{background:rgba(29,155,255,.12);color:var(--blue)}.nav .ico{width:22px;text-align:center}
  .botbox{border:1px solid var(--border);border-radius:12px;padding:14px;margin-top:22px}.botbox .row{display:flex;justify-content:space-between;font-size:12px;padding:4px 0;border-bottom:1px solid rgba(36,62,96,.3)}.botbox .row:last-child{border:0}
  .main{padding:24px 26px}.tab-bar{display:flex;gap:0;margin:0 0 16px;background:var(--card);border-radius:8px;overflow:hidden;border:1px solid var(--border)}
  .tab-bar .tab{padding:10px 20px;cursor:pointer;border:none;background:transparent;color:var(--muted);font-weight:600;font-size:12px;flex:1;text-align:center;text-decoration:none}
  .tab-bar .tab.active{background:rgba(29,155,255,.15);color:var(--blue)}
  .top{display:flex;gap:18px;align-items:center;margin-bottom:16px}
  .top .title h2{margin:0;font-size:22px}.top .title p{margin:0;color:var(--muted);font-size:12px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
  .card.kpi{padding:14px}.kpi .label{font-size:11px;color:var(--muted);margin-bottom:4px}.kpi .value{font-size:22px;font-weight:700}.kpi .sub{font-size:11px;color:var(--muted)}
  .gain{color:var(--green)}.loss{color:var(--red)}.neutral{color:var(--muted)}
  .badge{display:inline-flex;align-items:center;border-radius:999px;padding:4px 10px;font-size:11px;font-weight:700}
  .badge.active{background:rgba(57,240,122,.15);color:var(--green)}.badge.warn{background:rgba(255,177,26,.15);color:var(--amber)}.badge.danger{background:rgba(255,93,99,.15);color:var(--red)}
  .grid{display:grid;grid-template-columns:1.65fr 1.1fr;gap:14px;margin-bottom:16px}
  .bottom{display:grid;grid-template-columns:1.2fr 0.7fr 0.7fr 1fr;gap:14px;margin-top:14px}
  table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:8px;border-bottom:1px solid rgba(38,64,96,.55)}th{color:var(--muted);font-size:11px}
  .mini-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .heat{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin-top:12px}
  .heat div{padding:13px 8px;text-align:center;border-radius:7px;background:linear-gradient(180deg,rgba(31,115,65,.75),rgba(16,76,48,.78))}
  .heat div.red{background:linear-gradient(180deg,rgba(111,35,43,.85),rgba(64,20,28,.88))}
  .bar{height:8px;background:var(--border);border-radius:999px;overflow:hidden;margin:4px 0}.bar span{display:block;height:100%;background:var(--green);border-radius:999px}.bar span.warn{background:var(--amber)}.bar span.danger{background:var(--red)}
  .status-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(36,62,96,.3);font-size:13px}
  @media(max-width:1300px){.grid,.bottom{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand"><div><h1>HERMES</h1><span>TRADING BOT</span></div></div>
    <nav class="nav">
      <a class="active" href="#overview"><span class="ico">O</span>Overview</a>
      <a href="#positions"><span class="ico">P</span>Positions</a>
      <a href="#controls"><span class="ico">C</span>Controls</a>
      <a href="#journal"><span class="ico">J</span>Journal</a>
    </nav>
    <div class="botbox">
      <div class="row"><span>Status</span><b><span id="bot-status" style="color:var(--green)">Active</span></b></div>
      <div class="row"><span>Mode</span><b id="mode-label">Paper</b></div>
      <div class="row"><span>SMA200 Kill</span><b id="kill-status">—</b></div>
      <div class="row"><span>Regime Gate</span><b id="regime-status">—</b></div>
      <div class="row"><span>Refresh</span><b>15 sec</b></div>
    </div>
  </aside>
  <main class="main">
    <div class="top">
      <div class="title"><h2 id="page-title">Conservative Bot</h2><p><span id="today"></span> UTC</p></div>
      <div style="flex:1"></div>
      <div class="badge active"><span id="bot-name">CONS A</span></div>
    </div>

    <section id="ab-banner" style="background:linear-gradient(135deg,#1a0f2e,#0d1f3a);border:2px solid #ffb11a;border-radius:12px;padding:16px 24px;margin-bottom:16px;display:flex;align-items:center;gap:18px">
      <div style="flex:1;display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div style="background:rgba(16,230,255,.08);border:1px solid rgba(16,230,255,.3);border-radius:8px;padding:12px">
          <div style="font-size:10px;color:#10e6ff;font-weight:700;margin-bottom:4px">CONS A</div>
          <div style="font-size:22px;font-weight:700" id="ab-cons-equity">—</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px" id="ab-cons-meta">—</div>
          <div style="font-size:10px;color:#10e6ff;margin-top:2px" id="ab-cons-params">Exit: 120-168h</div>
        </div>
        <div style="background:rgba(255,177,26,.08);border:1px solid rgba(255,177,26,.4);border-radius:8px;padding:12px">
          <div style="font-size:10px;color:#ffb11a;font-weight:700;margin-bottom:4px">AGGR B</div>
          <div style="font-size:22px;font-weight:700" id="ab-aggr-equity">—</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px" id="ab-aggr-meta">—</div>
          <div style="font-size:10px;color:#ffb11a;margin-top:2px" id="ab-aggr-params">Exit: 48-96h</div>
        </div>
      </div>
    </section>

    <section class="kpis" id="kpis">
      <div class="card kpi"><div class="label">Equity</div><div class="value" id="equity">—</div><div class="sub" id="equity-sub">—</div></div>
      <div class="card kpi"><div class="label">Daily PnL</div><div class="value" id="daily-pnl">—</div><div class="sub" id="daily-pnl-sub">—</div></div>
      <div class="card kpi"><div class="label">Win Rate</div><div class="value" id="win-rate">—</div><div class="sub">all trades</div></div>
      <div class="card kpi"><div class="label">Profit Factor</div><div class="value" id="profit-factor">—</div><div class="sub">ratio</div></div>
      <div class="card kpi"><div class="label">Sharpe (30d)</div><div class="value" id="sharpe">—</div><div class="sub">annualized</div></div>
      <div class="card kpi"><div class="label">Sortino</div><div class="value" id="sortino">—</div><div class="sub">downside</div></div>
      <div class="card kpi"><div class="label">Positions</div><div class="value" id="positions-count">—</div><div class="sub">open</div></div>
      <div class="card kpi"><div class="label">Leverage</div><div class="value" id="leverage">—</div><div class="sub">effective</div></div>
    </section>

    <section id="control-status" class="card" style="margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <h3 style="margin:0;font-size:15px">Gate &amp; Kill Status</h3>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px">
        <div style="background:var(--card2);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted)">SMA200 Kill</div>
          <div style="font-size:16px;font-weight:700" id="kill-val">--</div>
          <div style="font-size:10px;color:var(--muted)" id="kill-pnl">--</div>
        </div>
        <div style="background:var(--card2);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted)">Regime Gate</div>
          <div style="font-size:16px;font-weight:700" id="regime-val">--</div>
          <div style="font-size:10px;color:var(--muted)" id="regime-detail">--</div>
        </div>
        <div style="background:var(--card2);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted)">Max Positions</div>
          <div style="font-size:16px;font-weight:700"><span id="max-pos">3</span>/<span id="cur-pos">0</span></div>
          <div style="font-size:10px;color:var(--muted)">capacity</div>
        </div>
        <div style="background:var(--card2);border-radius:8px;padding:12px">
          <div style="font-size:11px;color:var(--muted)">Drawdown</div>
          <div style="font-size:16px;font-weight:700" id="dd-val">--</div>
          <div style="font-size:10px;color:var(--muted)" id="dd-duration">--</div>
        </div>
      </div>
    </section>

    <section class="card" id="positions" style="margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <h3 style="margin:0;font-size:15px">Open Positions (<span id="position-count">0</span>)</h3>
      </div>
      <div style="overflow-x:auto">
        <table><thead><tr><th>Asset</th><th>Side</th><th>Entry</th><th>Current</th><th>Unrealized</th><th>Stop</th><th>Age</th></tr></thead>
        <tbody id="positions-body"></tbody></table>
      </div>
    </section>

    <section class="card" id="trades" style="margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <h3 style="margin:0;font-size:15px">Recent Trades (SMA200)</h3>
      </div>
      <div style="overflow-x:auto">
        <table><thead><tr><th>Asset</th><th>Side</th><th>Entry</th><th>Exit</th><th>R</th><th>PnL</th><th>Exit Reason</th></tr></thead>
        <tbody id="trades-body"></tbody></table>
      </div>
    </section>

    <section class="card" id="markets" style="margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <h3 style="margin:0;font-size:15px">Market Heatmap (24h)</h3>
      </div>
      <div class="heat" id="heatmap"></div>
    </section>
  </main>
</div>
<script>
const BOT = "__BOT__";
const fmtUSD = n => '$' + Number(n||0).toLocaleString(undefined, {maximumFractionDigits:2});
const pct = n => (Number(n||0) >= 0 ? '+' : '') + Number(n||0).toFixed(2) + '%';
const cls = n => Number(n||0) >= 0 ? 'gain' : 'loss';
const now = () => { document.getElementById('today').textContent = new Date().toISOString().slice(0,19).replace('T',' ') };
setInterval(now, 1000); now();

let consStats = {}, aggrStats = {};
let _abLoaded = false;

function api(e) {
  return '/api/' + e;
}

async function load(){
  try {
    if (BOT === 'aggr') {
      let [st, tr, po] = await Promise.all([
      ]);
      renderStatus(st, tr, po, 'aggr');
    } else if (BOT === 'spot') {
      let [st, tr, po] = await Promise.all([
        fetch('/api/sma200/status').then(r => r.json()),
        fetch('/api/sma200/trades').then(r => r.json()),
        fetch('/api/sma200/positions').then(r => r.json()),
      ]);
      renderStatus(st, tr, po, 'spot');
    } else {
      let [st, tr, po, ma, co] = await Promise.all([
        fetch('/api/status').then(r => r.json()),
        fetch('/api/trades').then(r => r.json()),
        fetch('/api/positions').then(r => r.json()),
        fetch('/api/markets').then(r => r.json()),
        fetch('/api/compare').then(r => r.json()),
      ]);
      consStats = st;
      renderStatus(st, tr, po, 'cons');
      renderMarkets(ma);
      renderCompare(co);
    }
  } catch(e) { console.error(e); }
}

function renderCompare(co) {
  if (!co || co.error) return;
  aggrStats = co;
  // Re-render the A/B banner
  document.getElementById('ab-cons-equity').textContent = fmtUSD(consStats.equity || 5000);
  document.getElementById('ab-aggr-equity').textContent = fmtUSD(co.equity || 5000);
  document.getElementById('ab-cons-meta').textContent = (consStats.total_trades || 0) + 't WR ' + pct((consStats.win_rate||0)*100);
  document.getElementById('ab-aggr-meta').textContent = (co.total_trades || 0) + 't WR ' + pct((co.win_rate||0)*100);
  _abLoaded = true;
}

function renderStatus(st, trades, positions, mode) {
  const isSpot = mode === 'spot';
  document.getElementById('page-title').textContent = mode === 'aggr' ? 'Aggressive Bot (B)' : mode === 'spot' ? 'SMA200 Perp' : 'Conservative Bot (A)';
  document.getElementById('bot-name').textContent = mode === 'aggr' ? 'AGGR B' : mode === 'spot' ? 'SMA200' : 'CONS A';
  document.getElementById('mode-label').textContent = mode === 'aggr' ? 'AGGRESSIVE' : mode === 'spot' ? 'SMA200' : 'CONSERVATIVE';

  if (isSpot) {
    document.getElementById('markets').style.display = 'none';
    document.getElementById('ab-banner').style.display = 'none';
    document.getElementById('control-status').style.display = 'none';
  } else {
    document.getElementById('markets').style.display = 'block';
    if (mode === 'cons') {
      document.getElementById('ab-banner').style.display = 'flex';
    } else {
      document.getElementById('ab-banner').style.display = 'none';
    }
  }

  // KPI row
  const eq = st.equity || 5000;
  document.getElementById('equity').textContent = fmtUSD(eq);
  document.getElementById('daily-pnl').textContent = fmtUSD(st.daily_pnl_pct ? (eq * st.daily_pnl_pct / 100) : 0);
  document.getElementById('equity-sub').textContent = pct(st.daily_pnl_pct || 0) + ' today';
  document.getElementById('daily-pnl-sub').textContent = pct(st.daily_pnl_pct || 0);
  document.getElementById('win-rate').textContent = st.win_rate ? (st.win_rate * 100).toFixed(1) + '%' : '0.0%';
  document.getElementById('profit-factor').textContent = st.profit_factor ? st.profit_factor.toFixed(2) : '—';
  document.getElementById('sharpe').textContent = st.sharpe ? st.sharpe.toFixed(2) : '—';
  document.getElementById('sortino').textContent = st.sortino ? st.sortino.toFixed(2) : '—';
  document.getElementById('positions-count').textContent = st.positions || 0;
  document.getElementById('leverage').textContent = (st.effective_leverage||0).toFixed(2) + 'x';

  // Kill + regime
  if (!isSpot) {
    const killed = st.sma200_killed;
    document.getElementById('kill-val').textContent = killed ? 'KILLED' : 'Active';
    document.getElementById('kill-val').style.color = killed ? 'var(--red)' : 'var(--green)';
    document.getElementById('kill-pnl').textContent = '28d PnL: ' + fmtUSD(st.sma200_kill_pnl || 0);
    document.getElementById('kill-status').textContent = killed ? 'KILLED' : 'Active';
    document.getElementById('kill-status').style.color = killed ? 'var(--red)' : 'var(--green)';
    // Regime info from marks or dynamic
    document.getElementById('regime-val').textContent = '—';
    document.getElementById('regime-status').textContent = '—';
    document.getElementById('cur-pos').textContent = st.positions || 0;
    const dd = st.peak_equity > 0 ? ((st.peak_equity - eq) / st.peak_equity * 100) : 0;
    document.getElementById('dd-val').textContent = dd.toFixed(2) + '%';
    document.getElementById('dd-duration').textContent = (st.drawdown_duration_hours || 0) + 'h since peak';
  }

  // Positions
  const tbody = document.getElementById('positions-body');
  document.getElementById('position-count').textContent = positions.length;
  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted)">No open positions</td></tr>';
  } else {
    tbody.innerHTML = positions.map(p => {
      const ep = Number(p.entry_price || 0);
      const upnl = p.unrealized_pnl || 0;
      const cur = ep + upnl / Math.max(p.size || 1, 0.001);
      const entryTime = p.entry_time?.[0]?.slice(0,16) || '—';
      return `<tr><td>${p.asset}</td><td class="${p.side === 'long' ? 'gain' : 'loss'}">${p.side}</td><td>${fmtUSD(ep)}</td><td>${fmtUSD(cur)}</td><td class="${cls(upnl)}">${fmtUSD(upnl)}</td><td>${fmtUSD(p.stop_loss || 0)}</td><td>${entryTime}</td></tr>`;
    }).join('');
  }

  // Trades (SMA200 only if not isSpot, otherwise all)
  const tbody2 = document.getElementById('trades-body');
  const smaTrades = mode === 'spot' ? trades : (trades || []).filter(t => (t.strategy||'') === 'sma200_perp');
  if (!smaTrades.length) {
    tbody2.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted)">No SMA200 trades yet</td></tr>';
  } else {
    tbody2.innerHTML = smaTrades.slice(0, 20).map(t => {
      const r = t.r_multiple || t.r || 0;
      return `<tr><td>${t.asset}</td><td class="${(t.side||'short').toLowerCase() === 'long' ? 'gain' : 'loss'}">${t.side || 'SHORT'}</td><td>${fmtUSD(t.entry_price)}</td><td>${fmtUSD(t.exit_price)}</td><td class="${cls(r)}">${r.toFixed(2)}R</td><td class="${cls(t.pnl_dollars)}">${fmtUSD(t.pnl_dollars)}</td><td>${t.exit_reason || '—'}</td></tr>`;
    }).join('');
  }
}

function renderMarkets(markets) {
  const heat = document.getElementById('heatmap');
  const data = markets.length ? markets : ASSETS.map(a => ({asset: a, change_24h: 0, price: 0}));
  heat.innerHTML = data.slice(0, 20).map(m => {
    const ch = Number(m.change_24h || 0);
    return `<div class="${ch < 0 ? 'red' : ''}">${m.asset}<br><span class="${ch < 0 ? 'loss' : 'gain'}">${pct(ch)}</span></div>`;
  }).join('');
}

document.querySelectorAll('.tab-bar a').forEach(a => {
  const p = a.getAttribute('href');
  if (BOT === 'cons' && (p === '/' || p === '/cons')) a.classList.add('active');
  if (BOT === 'spot' && p === '/spot') a.classList.add('active');
});

load();
setInterval(load, 15000);
</script>
</body>
</html>"""


def _aggregate_by(trades, key_fn):
    buckets = {}
    for t in trades:
        k = key_fn(t)
        if not k: continue
        buckets.setdefault(k, []).append(t)
    results = []
    for k, ts in sorted(buckets.items(), key=lambda x: -len(x[1])):
        wins = [t for t in ts if (t.get("pnl_pct") or 0) > 0]
        losses = [t for t in ts if (t.get("pnl_pct") or 0) <= 0]
        pnl = sum(t.get("pnl_dollars", 0) or 0 for t in ts)
        win_pnl = sum(t.get("pnl_dollars", 0) or 0 for t in wins)
        loss_pnl = abs(sum(t.get("pnl_dollars", 0) or 0 for t in losses))
        results.append({
            "key": k, "trades": len(ts), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(ts) if ts else 0,
            "profit_factor": win_pnl / loss_pnl if loss_pnl > 0 else 0,
            "pnl_dollars": round(pnl, 2),
        })
    return results


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, db_path=None, compare_db_path=None, **kwargs):
        self.db_path = db_path or _repo / "data" / "hermes.db"
        self.compare_db_path = compare_db_path
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path in ("/", "/cons"):
            self._send_html(HTML.replace("__BOT__", "cons"))
            self._send_html(HTML.replace("__BOT__", "aggr"))
        elif self.path == "/spot":
            self._send_html(HTML.replace("__BOT__", "spot"))
        elif self.path.startswith("/api/"):
            self._handle_api()
        else:
            self.send_error(404)

    def _send_html(self, html: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html.encode())

    def _send_json(self, data: Any):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _handle_api(self):
        api = self.path.split("?")[0]
        handler = {
            "/api/status": self._api_status,
            "/api/trades": self._api_trades,
            "/api/positions": self._api_positions,
            "/api/equity": self._api_equity,
            "/api/markets": self._api_markets,
            "/api/signals": self._api_signals,
            "/api/reflection": self._api_reflection,
            "/api/compare": self._api_compare,
            "/api/sma200/status": self._api_sma200_status,
            "/api/sma200/trades": self._api_sma200_trades,
            "/api/sma200/positions": self._api_sma200_positions,
            "/api/sma200/equity": self._api_sma200_equity,
        }.get(api)
        if handler:
            try:
                handler()
            except Exception as e:
                self._send_json({"error": str(e)})
        else:
            self.send_error(404)

    def _connect(self):
        return _connect_db(self.db_path)

    def _api_status(self):
        conn = self._connect()
        try:
            self._send_json(_get_bot_stats(conn))
        finally:
            conn.close()

    def _api_trades(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
            self._send_json([dict(r) for r in rows])
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_positions(self):
        conn = self._connect()
        try:
            state = _read_state(conn, "positions", [])
            # Enrich with current timeframe info
            positions = []
            for p in state:
                p["age_min"] = 0
                if p.get("entry_time"):
                    try:
                        et = datetime.fromisoformat(p["entry_time"])
                        p["age_min"] = round((datetime.now(timezone.utc) - et).total_seconds() / 60)
                    except:
                        pass
                positions.append(p)
            self._send_json(positions)
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_equity(self):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT equity, peak_equity, timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 500"
            ).fetchall()
            self._send_json([{"equity": r["equity"], "peak": r["peak_equity"], "ts": r["timestamp"]} for r in rows][::-1])
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_markets(self):
        now = time.time()
        if now - _MARKET_CACHE["ts"] < 55 and _MARKET_CACHE["data"]:
            self._send_json(_MARKET_CACHE["data"])
            return
        try:
            sp = _repo / "data" / "external_snapshot.json"
            snap = {}
            if sp.exists():
                snap = json.loads(sp.read_text())
            funding = snap.get("funding", {})
            prices = snap.get("prices", {})
            changes = snap.get("change_24h", {})
            markets = []
            for asset in ASSETS:
                if asset not in funding:
                    continue
                markets.append({
                    "asset": asset, "price": prices.get(asset, 0),
                    "change_24h": changes.get(asset, 0),
                    "funding_rate": funding.get(asset, 0),
                })
            _MARKET_CACHE["data"] = markets
            _MARKET_CACHE["ts"] = now
            self._send_json(markets)
        except Exception:
            self._send_json(_MARKET_CACHE["data"] or [])

    def _api_signals(self):
        conn = self._connect()
        try:
            state = _read_state(conn, "daily_signals", [])
            self._send_json(state)
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_reflection(self):
        conn = self._connect()
        try:
            state = _read_state(conn, "weekly_reflection", {})
            self._send_json(state)
        except Exception:
            self._send_json({})
        finally:
            conn.close()

    def _api_compare(self):
        if not self.compare_db_path:
            self._send_json({"error": "no compare_db"})
            return
        conn = _connect_db(self.compare_db_path)
        try:
            self._send_json(_get_bot_stats(conn))
        finally:
            conn.close()

    # ── SMA200 perp endpoints (reads from CONS hermes.db) ──────────────
    def _api_sma200_status(self):
        conn = self._connect()
        try:
            stats = _get_bot_stats(conn)
            # Override: only SMA200 perp trades
            sma_trades = conn.execute(
                "SELECT pnl_dollars, r_multiple, side FROM trades WHERE strategy='sma200_perp'"
            ).fetchall()
            s_trades = [dict(r) for r in sma_trades]
            stats["total_trades"] = len(s_trades)
            if s_trades:
                wins = [t for t in s_trades if t.get("pnl_dollars", 0) > 0]
                stats["win_rate"] = len(wins) / len(s_trades) if s_trades else 0
                stats["avg_r"] = sum(abs(t.get("r_multiple", 0) or 0) for t in s_trades) / len(s_trades)
            self._send_json(stats)
        finally:
            conn.close()

    def _api_sma200_trades(self):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE strategy='sma200_perp' ORDER BY id DESC LIMIT 50"
            ).fetchall()
            self._send_json([dict(r) for r in rows])
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_sma200_positions(self):
        conn = self._connect()
        try:
            state = _read_state(conn, "positions", [])
            # Only show SMA200 perp positions
            sma_pos = []
            for p in state:
                strat = p.get("strategy", "")
                if "sma200" in strat:
                    sma_pos.append(p)
            self._send_json(sma_pos)
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_sma200_equity(self):
        self._api_equity()

    # ── AGGR endpoints ────────────────────────────────────────────────
        if not self.compare_db_path or not Path(str(self.compare_db_path)).exists():
            return None
        return _connect_db(self.compare_db_path)

        if conn is None:
            self._send_json({"error": "no_aggr_db", "equity": 5000})
            return
        try:
            self._send_json(_get_bot_stats(conn))
        finally:
            conn.close()

        try:
            if conn is None:
                self._send_json([])
                return
            state = _read_state(conn, "positions", [])
            self._send_json(state)
            conn.close()
        except Exception:
            self._send_json([])

        try:
            if conn is None:
                self._send_json([])
                return
            rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
            conn.close()
            self._send_json([dict(r) for r in rows])
        except Exception:
            self._send_json([])

        try:
            if conn is None:
                self._send_json([])
                return
            rows = conn.execute(
                "SELECT equity, peak_equity, timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 500"
            ).fetchall()
            conn.close()
            self._send_json([{"equity": r["equity"], "peak": r["peak_equity"], "ts": r["timestamp"]} for r in rows][::-1])
        except Exception:
            self._send_json([])

    def log_message(self, fmt, *args):
        pass


class ThreadingServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(db_path=Path("data/hermes.db"), port=8081, compare_db_path=None):
    class Handler(DashboardHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, db_path=db_path, compare_db_path=compare_db_path, **kwargs)
    server = ThreadingServer(("0.0.0.0", port), Handler)
    print(f"Dashboard: http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Hermes Dashboard")
    parser.add_argument("db_path", nargs="?", default="data/hermes.db")
    parser.add_argument("port", nargs="?", type=int, default=8081)
    parser.add_argument("--second-db", type=str, default=None)
    args = parser.parse_args()
    compare = Path(args.second_db) if args.second_db else None
    serve(Path(args.db_path), args.port, compare)
