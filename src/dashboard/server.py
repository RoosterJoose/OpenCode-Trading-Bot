"""
Dashboard — lightweight HTTP server for monitoring.
Uses only stdlib (http.server + sqlite3). No extra dependencies.

Endpoints:
  /               — HTML dashboard
  /api/status     — Bot health, equity, risk metrics
  /api/trades     — Recent closed trades
  /api/positions  — Open positions
  /api/equity     — Equity history for chart
  /api/signals    — Recent signal log
  /api/reflection — Weekly reflection report
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
from functools import lru_cache
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Any

# Resolve project root
_repo = Path(__file__).resolve().parent.parent.parent
ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE",
          "ADA", "AVAX", "LINK", "DOT", "AAVE",
          "LTC", "NEAR", "SUI", "BNB", "XLM",
          "HBAR", "BCH", "ZEC", "PEPE", "SHIB",
          "HYPE", "ONDO", "ENA"]
_MARKET_CACHE = {"ts": 0.0, "data": []}

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Trading Bot</title>
<style>
  :root {
    --bg:#030914; --panel:#07111f; --card:#0b1524; --card2:#0e1a2b;
    --border:#18304c; --muted:#8495ad; --text:#e8f1ff; --soft:#aebbd0;
    --cyan:#10e6ff; --blue:#1d9bff; --purple:#7d3cff; --green:#39f07a;
    --red:#ff5d63; --amber:#ffb11a; --shadow:rgba(0,0,0,.45);
  }
  *{box-sizing:border-box} html{scroll-behavior:smooth} body{margin:0;background:radial-gradient(circle at 88% -5%,rgba(125,60,255,.30),transparent 22%),radial-gradient(circle at 7% 4%,rgba(16,230,255,.18),transparent 24%),linear-gradient(135deg,#020712,#071321 55%,#020710);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;overflow-x:hidden}
  .app{display:grid;grid-template-columns:246px 1fr;min-height:100vh}.side{position:sticky;top:0;height:100vh;padding:24px 18px;border-right:1px solid rgba(36,62,96,.55);background:linear-gradient(180deg,rgba(3,10,20,.96),rgba(2,8,15,.90));box-shadow:12px 0 34px rgba(0,0,0,.24)}
  .brand{display:flex;align-items:center;gap:14px;margin:2px 0 32px}.logo{width:44px;height:44px;border-radius:14px;background:conic-gradient(from 220deg,var(--cyan),var(--purple),#15335c,var(--cyan));clip-path:polygon(50% 0,100% 88%,0 88%);filter:drop-shadow(0 0 18px rgba(16,230,255,.45))}.brand h1{font-size:18px;letter-spacing:4px;margin:0}.brand span{display:block;color:var(--muted);font-size:11px;letter-spacing:2px;margin-top:3px}
  .nav{display:flex;flex-direction:column;gap:10px}.nav a{display:flex;align-items:center;gap:14px;color:#c6d1e4;text-decoration:none;padding:15px 16px;border-radius:12px;border:1px solid transparent;font-weight:650}.nav a.active,.nav a:hover{background:linear-gradient(100deg,rgba(16,160,255,.75),rgba(125,60,255,.85));color:#fff;box-shadow:0 10px 28px rgba(35,112,255,.25)}.ico{width:23px;text-align:center;color:#b5c4db}.botbox{position:absolute;left:18px;right:18px;bottom:22px;background:linear-gradient(180deg,rgba(13,29,48,.88),rgba(9,20,34,.78));border:1px solid var(--border);border-radius:14px;padding:15px}.botbox .row{display:flex;justify-content:space-between;color:var(--soft);font-size:12px;margin:10px 0}.dot{display:inline-block;width:9px;height:9px;background:var(--green);border-radius:50%;box-shadow:0 0 16px var(--green)}
  .main{padding:24px 24px 26px}.top{display:grid;grid-template-columns:1fr minmax(320px,520px) auto;gap:18px;align-items:center;margin-bottom:22px}.title h2{font-size:27px;margin:0 0 6px}.title p{margin:0;color:var(--muted);font-size:13px}.search{height:54px;border:1px solid var(--border);background:rgba(8,18,31,.82);border-radius:13px;display:flex;align-items:center;gap:12px;padding:0 18px;box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}.search input{width:100%;background:transparent;border:0;outline:0;color:var(--text);font-size:14px}.search kbd{color:#99a9bf;border:1px solid #263f63;border-radius:7px;padding:3px 8px}.pill{border:1px solid var(--border);border-radius:13px;background:rgba(9,24,39,.84);padding:13px 17px;font-weight:800;color:#76ff9b;display:flex;gap:10px;align-items:center;white-space:nowrap}.profile{display:flex;align-items:center;gap:12px;color:#d8e3f3}.avatar{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#3764ff,#ff9a3c);display:grid;place-items:center;font-weight:900}.pro{display:inline-block;background:#5d36d8;color:#d7cbff;border-radius:7px;padding:2px 8px;font-size:11px;margin-top:3px}
  .kpis{display:grid;grid-template-columns:repeat(11,minmax(135px,1fr));gap:13px;margin-bottom:14px}.card{background:linear-gradient(180deg,rgba(14,28,48,.86),rgba(7,17,30,.88));border:1px solid rgba(36,62,96,.75);border-radius:13px;box-shadow:0 18px 38px var(--shadow),inset 0 1px 0 rgba(255,255,255,.04)}.kpi{padding:15px 17px;min-height:112px;position:relative;overflow:hidden}.label{color:#b0bed3;font-size:13px;margin-bottom:8px}.value{font-size:25px;font-weight:850;letter-spacing:-.5px}.gain{color:var(--green)}.loss{color:var(--red)}.sub{font-size:13px;color:var(--muted);margin-top:8px}.spark{position:absolute;right:13px;bottom:12px;width:92px;height:38px;opacity:.95}.bar{height:7px;background:#15263b;border-radius:999px;overflow:hidden;margin-top:18px}.bar span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--blue),var(--purple))}.donut{width:64px;height:64px;border-radius:50%;background:conic-gradient(var(--blue) 0 242deg,var(--purple) 242deg 318deg,#162842 318deg);display:grid;place-items:center}.donut:after{content:"";width:44px;height:44px;border-radius:50%;background:#081424}
  .grid{display:grid;grid-template-columns:1.65fr 1.1fr;gap:14px}    .section{padding:16px}.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.head h3{margin:0;font-size:16px}.tools{display:flex;gap:8px}.tools button,.viewbtn{border:1px solid #214064;background:#0a1728;color:#cfe1f5;border-radius:9px;padding:8px 11px;cursor:pointer}.tools button.active{background:linear-gradient(135deg,#0a70c9,#0c3155);border-color:#2e8eea}.chart{height:258px;width:100%}.mini-grid{display:grid;grid-template-columns:1.05fr 1fr 1fr 1fr;gap:8px}.intel-card{padding:14px;border-radius:10px;background:#0a1728;border:1px solid #1d3656}.heat{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin-top:12px}.heat div{padding:13px 8px;text-align:center;border-radius:7px;background:linear-gradient(180deg,rgba(31,115,65,.75),rgba(16,76,48,.78));font-weight:800}.heat div.red{background:linear-gradient(180deg,rgba(111,35,43,.85),rgba(64,20,28,.88))}.strategies{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.strategy{padding:14px;border:1px solid #1d3656;border-radius:10px;background:#091525}.strategy .tag{float:right;font-size:11px;padding:4px 9px;border-radius:999px;background:rgba(57,240,122,.12);color:var(--green)}
 48:   .main{padding:24px 24px 26px}.top{display:grid;grid-template-columns:1fr minmax(320px,520px) auto;gap:18px;align-items:center;margin-bottom:22px}.title h2{font-size:27px;margin:0 0 6px}.title p{margin:0;color:var(--muted);font-size:13px}.search{height:54px;border:1px solid var(--border);background:rgba(8,18,31,.82);border-radius:13px;display:flex;align-items:center;gap:12px;padding:0 18px;box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}.search input{width:100%;background:transparent;border:0;outline:0;color:var(--text);font-size:14px}.search kbd{color:#99a9bf;border:1px solid #263f63;border-radius:7px;padding:3px 8px}.pill{border:1px solid var(--border);border-radius:13px;background:rgba(9,24,39,.84);padding:13px 17px;font-weight:800;color:#76ff9b;display:flex;gap:10px;align-items:center;white-space:nowrap}.profile{display:flex;align-items:center;gap:12px;color:#d8e3f3}.avatar{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#3764ff,#ff9a3c);display:grid;place-items:center;font-weight:900}.pro{display:inline-block;background:#5d36d8;color:#d7cbff;border-radius:7px;padding:2px 8px;font-size:11px;margin-top:3px}
  table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:9px 8px;border-bottom:1px solid rgba(38,64,96,.55)}th{color:#8fa2bc;font-size:11px;font-weight:700}.badge{display:inline-flex;align-items:center;border-radius:999px;padding:4px 10px;font-size:11px;font-weight:800}.positive{color:var(--green)}.negative{color:var(--red)}.neutral{color:#a7b6ca}.badge.positive{background:rgba(57,240,122,.12)}.badge.negative{background:rgba(255,93,99,.12)}.badge.neutral{background:rgba(122,150,185,.12)}.confidence{height:6px;width:55px;background:#172842;border-radius:999px;overflow:hidden}.confidence span{display:block;height:100%;background:linear-gradient(90deg,#36e776,#7dff9f)}
  .bottom{display:grid;grid-template-columns:1.45fr .62fr .75fr 1fr;gap:14px;margin-top:14px}.riskline{margin:15px 0}.ring{width:86px;height:86px;border-radius:50%;background:conic-gradient(var(--amber) 0 226deg,#172842 226deg);display:grid;place-items:center;color:var(--amber);font-weight:900}.ai{background:linear-gradient(135deg,rgba(19,38,92,.9),rgba(46,21,99,.88))}.ask{margin-top:12px;border:0;border-radius:10px;padding:13px 15px;width:100%;color:#fff;background:linear-gradient(90deg,#2559ff,#7d3cff);text-align:left}.timeline{display:grid;gap:12px}.event{display:grid;grid-template-columns:56px 14px 1fr;gap:10px;align-items:start}.event .pin{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 15px var(--green);margin-top:6px}.event small{color:#7f91aa}.event b{display:block;font-size:13px}.empty{color:#8192a9;padding:24px;text-align:center}.mobile-only{display:none}
  @media(max-width:1300px){.kpis{grid-template-columns:repeat(3,1fr)}.grid,.bottom{grid-template-columns:1fr}.strategies{grid-template-columns:repeat(2,1fr)}}@media(max-width:860px){.app{grid-template-columns:1fr}.side{display:none}.main{padding:16px}.top{grid-template-columns:1fr}.kpis{grid-template-columns:1fr 1fr}.mini-grid,.heat,.strategies{grid-template-columns:1fr 1fr}.mobile-only{display:block}}@media(max-width:560px){.kpis,.mini-grid,.heat,.strategies{grid-template-columns:1fr}.value{font-size:21px}}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand"><div class="logo"></div><div><h1>HERMES</h1><span>TRADING BOT</span></div></div>
    <nav class="nav">
      <a class="active" href="#overview"><span class="ico">⌂</span>Overview</a><a href="#markets"><span class="ico">◎</span>Markets</a><a href="#strategies"><span class="ico">⌘</span>Strategies</a><a href="#positions"><span class="ico">▣</span>Positions</a><a href="#risk"><span class="ico">◇</span>Risk</a><a href="#journal"><span class="ico">☷</span>Journal</a><a href="#analytics"><span class="ico">▥</span>Analytics</a>
    </nav>
    <div class="botbox"><div class="row"><span>Bot Status</span><b><i class="dot"></i> Running</b></div><div class="row"><span>Mode</span><b id="side-mode">Paper</b></div><div class="row"><span>Altfins Permits</span><b id="altfins-permits">—</b></div><div class="row"><span>Server</span><b>Oracle US-East</b></div><div class="row"><span>Refresh</span><b>15 sec</b></div></div>
  </aside>
  <main class="main" id="overview">
    <div class="top">
      <div class="title"><h2>Dashboard</h2><p><span id="today"></span> · <span id="clock"></span> UTC</p></div>
      <label class="search">⌕ <input id="search" placeholder="Search markets, pairs, strategies..." autocomplete="off"><kbd>⌘ K</kbd></label>
      <div style="display:flex;gap:14px;align-items:center"><div class="pill"><i class="dot"></i> Bot Active</div><div class="profile"><div class="avatar">H</div><div><b>Hermes</b><br><span class="pro">Paper</span></div></div></div>
    </div>
    <section id="ab-banner" style="background:linear-gradient(135deg,#1a0f2e,#0d1f3a,#1a0f2e);border:2px solid #ffb11a;border-radius:12px;padding:18px 24px;margin:18px 0;display:flex;align-items:center;gap:18px;box-shadow:0 0 32px rgba(255,177,26,.25)">
      <div style="flex-shrink:0;font-size:36px">&#9876;</div>
      <div style="flex:1">
        <div style="font-size:11px;letter-spacing:2px;color:#ffb11a;font-weight:700;margin-bottom:4px">A/B TEST LIVE</div>
        <div style="font-size:18px;font-weight:700;color:#fff;margin-bottom:8px">Conservative vs Aggressive Bot</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
          <div id="ab-conservative" style="background:rgba(16,230,255,.08);border:1px solid rgba(16,230,255,.3);border-radius:8px;padding:12px">
            <div style="font-size:10px;letter-spacing:1.5px;color:#10e6ff;font-weight:700;margin-bottom:6px">CONSERVATIVE</div>
            <div style="font-size:22px;font-weight:700;color:#fff" id="ab-conservative-equity">$11,273</div>
            <div style="font-size:11px;color:#aebbd0;margin-top:4px" id="ab-conservative-meta">201 trades &middot; 49% WR &middot; 0.70 conf</div>
          </div>
          <div id="ab-aggressive" style="background:rgba(255,177,26,.08);border:1px solid rgba(255,177,26,.4);border-radius:8px;padding:12px">
            <div style="font-size:10px;letter-spacing:1.5px;color:#ffb11a;font-weight:700;margin-bottom:6px">AGGRESSIVE</div>
            <div style="font-size:22px;font-weight:700;color:#fff" id="ab-aggressive-equity">$11,277</div>
            <div style="font-size:11px;color:#aebbd0;margin-top:4px" id="ab-aggressive-meta">0 trades &middot; 0% WR &middot; 0.55 conf</div>
          </div>
        </div>
      </div>
      <div style="flex-shrink:0;text-align:right;font-size:10px;color:#aebbd0;max-width:120px">
        <div>Both starting at</div>
        <div style="font-size:18px;color:#fff;font-weight:700">$11,273</div>
        <div style="margin-top:6px">Winner at 30 trades</div>
      </div>
    </section>
    <section id="ab-detail" style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px">
      <div style="background:linear-gradient(180deg,rgba(14,28,48,.86),rgba(7,17,30,.88));border:1px solid rgba(36,62,96,.75);border-radius:13px;padding:14px">
        <div style="font-size:11px;letter-spacing:1.5px;color:#10e6ff;font-weight:700;margin-bottom:10px">PER-ASSET BREAKDOWN</div>
        <div id="ab-per-asset" style="max-height:200px;overflow:auto;font-size:12px"></div>
      </div>
      <div style="background:linear-gradient(180deg,rgba(14,28,48,.86),rgba(7,17,30,.88));border:1px solid rgba(36,62,96,.75);border-radius:13px;padding:14px">
        <div style="font-size:11px;letter-spacing:1.5px;color:#ffb11a;font-weight:700;margin-bottom:10px">PER-REGIME BREAKDOWN</div>
        <div id="ab-per-regime" style="max-height:200px;overflow:auto;font-size:12px"></div>
      </div>
    </section>
    <section class="kpis">
      <div class="card kpi"><div class="label">Total Equity</div><div class="value" id="equity">—</div><div class="sub gain" id="equity-sub">—</div><canvas class="spark" id="spark-equity"></canvas></div>
      <div class="card kpi"><div class="label">Daily PnL</div><div class="value" id="daily-pnl">—</div><div class="sub" id="daily-pnl-sub">—</div><canvas class="spark" id="spark-daily"></canvas></div>
      <div class="card kpi"><div class="label">Weekly PnL</div><div class="value" id="weekly-pnl">—</div><div class="sub" id="weekly-pnl-sub">—</div><canvas class="spark" id="spark-weekly"></canvas></div>
      <div class="card kpi"><div class="label">Win Rate</div><div style="display:flex;justify-content:space-between"><div><div class="value" id="win-rate">—</div><div class="sub gain" id="win-rate-sub">live</div></div><div class="donut"></div></div></div>
      <div class="card kpi"><div class="label">Profit Factor</div><div class="value" id="profit-factor">—</div><div class="sub gain" id="profit-sub">—</div><canvas class="spark" id="spark-profit"></canvas></div>
      <div class="card kpi"><div class="label">Current Exposure</div><div class="value" id="exposure-pct">—</div><div class="bar"><span id="exposure-bar" style="width:0%"></span></div></div>
      <div class="card kpi"><div class="label">Active Positions</div><div class="value" id="active-positions">—</div><div class="sub" id="positions-sub">Across tracked pairs</div><canvas class="spark" id="spark-pos"></canvas></div>
      <div class="card kpi"><div class="label">Sortino (30d)</div><div class="value" id="sortino">—</div><div class="sub" id="sortino-sub">downside-adjusted</div></div>
      <div class="card kpi"><div class="label">Rolling 20 WR</div><div class="value" id="rolling-wr">—</div><div class="sub" id="rolling-sub">latest 20 trades</div></div>
      <div class="card kpi"><div class="label">Expectancy (R)</div><div class="value" id="expectancy">—</div><div class="sub" id="expectancy-sub">avg R per trade</div></div>
      <div class="card kpi"><div class="label">DD Duration</div><div class="value" id="dd-duration">—</div><div class="sub" id="dd-duration-sub">hours since peak</div></div>
    </section>
    <section class="grid">
      <div class="card section"><div class="head"><div><h3>Equity Curve <span class="neutral">ⓘ</span></h3><div><b id="curve-equity">—</b> <span class="gain" id="curve-change">—</span></div></div><div class="tools"><button data-range="1D">1D</button><button data-range="1W">1W</button><button class="active" data-range="1M">1M</button><button data-range="3M">3M</button></div></div><canvas class="chart" id="equity-chart"></canvas></div>
      <div class="card section" id="markets"><div class="head"><h3>Market Intelligence <span class="neutral">ⓘ</span></h3></div><div class="mini-grid"><div class="intel-card"><div class="label">Market Regime</div><h3 class="gain" id="market-regime">Watching</h3><div class="sub" id="source-sub">Coinbase + Altfins + Kalshi</div></div><div class="intel-card"><div class="label">Volatility</div><h3 id="volatility">Normal</h3><div class="sub" id="vol-sub">ATR gates active</div></div><div class="intel-card"><div class="label">Funding Sentiment</div><h3 class="gain" id="funding">Enabled</h3><div class="sub">long confidence source</div></div><div class="intel-card"><div class="label">Altfins Signals</div><h3 class="gain" id="altfins-count">0</h3><div class="sub" id="altfins-sub">last hour</div></div></div><div class="sub" style="margin-top:13px">Market Heatmap (24H)</div><div class="heat" id="heatmap"></div></div>
    </section>
    <section class="card section" id="strategies" style="margin-top:14px"><div class="head"><h3>Strategy Performance <span class="neutral">ⓘ</span></h3></div><div class="strategies" id="strategy-cards"></div></section>
    <section class="bottom">
      <div class="card section" id="positions"><div class="head"><h3>Open Positions (<span id="position-count">0</span>) <span class="neutral">ⓘ</span></h3><button class="viewbtn" id="position-filter">View All</button></div><div style="overflow:auto"><table><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>Current</th><th>Unrealized PnL</th><th>Stop Loss</th><th>Leverage</th><th>Size</th></tr></thead><tbody id="positions-body"></tbody></table></div></div>
      <div class="card section" id="risk"><div class="head"><h3>Risk Controls <span class="neutral">ⓘ</span></h3></div><div style="display:flex;gap:18px;align-items:center"><div><div class="riskline"><div class="label">Max Daily Drawdown</div><b class="gain" id="drawdown">—</b><div class="bar"><span id="dd-bar" style="width:0%"></span></div></div><div class="riskline"><div class="label">Leverage Usage</div><b id="leverage">—</b><div class="bar"><span id="lev-bar" style="width:0%"></span></div></div></div><div class="ring" id="risk-ring">0%</div></div><div class="riskline"><div class="label">Exposure Limit</div><b id="limit-text">—</b><div class="bar"><span id="limit-bar" style="width:0%"></span></div></div><div class="badge positive">Kill Switch Enabled</div></div>
      <div class="card section ai"><div class="head"><h3>AI Reflection <span class="neutral">ⓘ</span></h3></div><div id="reflection">Loading...</div><button class="ask">Ask Hermes ↗</button></div>
      <div class="card section"><div class="head"><h3>Daily Health <span class="neutral">ⓘ</span></h3></div><div id="daily-health">Loading...</div></div>
      <div class="card section"><div class="head"><h3>Daily Market Analysis <span class="neutral">ⓘ</span></h3></div><div id="daily-reflection">Loading...</div></div>
      <div class="card section"><div class="head"><h3>Accumulated Learnings <span class="neutral">ⓘ</span></h3></div><div id="learnings">Loading...</div></div>
      <div class="card section" id="journal"><div class="head"><h3>Recent Activity <span class="neutral">ⓘ</span></h3><button class="viewbtn" id="activity-filter">View All</button></div><div class="timeline" id="activity"></div></div>
    </section>
    <section class="card section" id="analytics" style="margin-top:14px"><div class="head"><h3>Watchlist / Opportunity Scanner <span class="neutral">ⓘ</span></h3><button class="viewbtn">View All</button></div><div style="overflow:auto"><table><thead><tr><th>#</th><th>Pair</th><th>Price</th><th>RSI (14)</th><th>Trend</th><th>Vol. Spike</th><th>Confidence</th><th>Signal</th></tr></thead><tbody id="watchlist"></tbody></table></div></section>
  </main>
</div>
<script>
let perAsset=[],perRegime=[];const ASSETS=['BTC','ETH','SOL','BNB','XRP','DOGE','ADA','AVAX','LINK','DOT','AAVE','LTC','NEAR','SUI','XLM','HBAR','BCH','ZEC','PEPE','SHIB','HYPE','ONDO','ENA'];let state={status:{},trades:[],positions:[],equity:[],signals:[],reflection:{},markets:[],range:'1M',query:'',learnings:{},health:{}};
const fmtUSD=n=>'$'+Number(n||0).toLocaleString(undefined,{maximumFractionDigits:2});const pct=n=>(Number(n||0)>=0?'+':'')+Number(n||0).toFixed(2)+'%';const cls=n=>Number(n||0)>=0?'positive':'negative';
function now(){const d=new Date();document.getElementById('today').textContent=d.toLocaleDateString(undefined,{weekday:'long',month:'long',day:'numeric',year:'numeric'});document.getElementById('clock').textContent=d.toISOString().slice(11,19)}setInterval(now,1000);now();
function drawLine(id, pts, color='#1d9bff', fill=true){const c=document.getElementById(id);if(!c)return;const r=c.getBoundingClientRect();c.width=Math.max(40,r.width*devicePixelRatio);c.height=Math.max(28,r.height*devicePixelRatio);const x=c.getContext('2d');x.scale(devicePixelRatio,devicePixelRatio);const w=r.width,h=r.height;x.clearRect(0,0,w,h);if(!pts||pts.length<2){pts=[0,1,0.6,1.4,1.2,1.8]}const min=Math.min(...pts),max=Math.max(...pts),rng=max-min||1;x.beginPath();pts.forEach((p,i)=>{const xx=i/(pts.length-1)*w;const yy=h-8-((p-min)/rng)*(h-16);i?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.strokeStyle=color;x.lineWidth=2;x.stroke();if(fill){x.lineTo(w,h);x.lineTo(0,h);x.closePath();const g=x.createLinearGradient(0,0,0,h);g.addColorStop(0,color+'55');g.addColorStop(1,color+'00');x.fillStyle=g;x.fill()}}
function equitySlice(){const map={ '1D':24,'1W':7*24,'1M':31*24,'3M':93*24};return state.equity.slice(-Math.min(state.equity.length,map[state.range]||state.equity.length)).map(p=>Number(p.equity||0))}
function renderKpis(){const s=state.status,p=state.positions,eq=Number(s.equity||10000),peak=Number(s.peak_equity||eq),dd=peak?((peak-eq)/peak*100):0,exp=Number(s.gross_exposure||0),lev=Number(s.effective_leverage||0),expPct=Math.min(100,exp/(eq*3||1)*100);document.getElementById('equity').textContent=fmtUSD(eq);document.getElementById('curve-equity').textContent=fmtUSD(eq);document.getElementById('equity-sub').textContent=pct(s.daily_pnl_pct||0)+' today';document.getElementById('curve-change').textContent=pct(((eq-10000)/10000)*100)+' all time';document.getElementById('daily-pnl').textContent=fmtUSD((eq-10000));document.getElementById('daily-pnl-sub').textContent=pct(s.daily_pnl_pct||0);document.getElementById('weekly-pnl').textContent=fmtUSD((eq-10000));document.getElementById('weekly-pnl-sub').textContent=pct(((eq-10000)/10000)*100);document.getElementById('win-rate').textContent=s.win_rate?(s.win_rate*100).toFixed(1)+'%':'0.0%';document.getElementById('profit-factor').textContent=s.profit_factor?Number(s.profit_factor).toFixed(2):'—';document.getElementById('profit-sub').textContent=s.profit_factor?'+ live':'waiting for trades';document.getElementById('exposure-pct').textContent=expPct.toFixed(1)+'%';document.getElementById('exposure-bar').style.width=expPct+'%';document.getElementById('active-positions').textContent=p.length;document.getElementById('positions-sub').textContent='Across '+ASSETS.length+' pairs';document.getElementById('drawdown').textContent=dd.toFixed(2)+'% / 5.00%';document.getElementById('dd-bar').style.width=Math.min(100,dd/5*100)+'%';document.getElementById('leverage').textContent=lev.toFixed(2)+'x / 3.0x';document.getElementById('lev-bar').style.width=Math.min(100,lev/3*100)+'%';document.getElementById('limit-text').textContent=expPct.toFixed(1)+'% / 100%';document.getElementById('limit-bar').style.width=expPct+'%';document.getElementById('risk-ring').textContent=Math.round(Math.max(dd/5*100,expPct))+'%';document.getElementById('side-mode').textContent=(s.mode||'paper').toUpperCase();const ap=s.altfins_permits||{};const avail=ap.available;document.getElementById('altfins-permits').textContent=avail!==undefined?avail+'/1000':'—';const sc=s.altfins_signal_count||0;document.getElementById('altfins-count').textContent=sc;document.getElementById('altfins-sub').textContent=sc?'signals active':'no signals';document.getElementById('sortino').textContent=s.sortino?Number(s.sortino).toFixed(2):'—';document.getElementById('sortino-sub').textContent=s.sortino?'downside-adjusted':'< 30 trades';document.getElementById('rolling-wr').textContent=s.rolling_wr?(s.rolling_wr*100).toFixed(1)+'% ('+s.rolling_trades+'tr)':'—';document.getElementById('rolling-sub').textContent=s.rolling_trades?s.rolling_trades+' trades':'insufficient data';document.getElementById('expectancy').textContent=s.expectancy?Number(s.expectancy).toFixed(3)+'R':'—';document.getElementById('expectancy-sub').textContent=s.expectancy?'avg R per trade':'< 30 trades';document.getElementById('dd-duration').textContent=s.drawdown_duration_hours?s.drawdown_duration_hours.toFixed(1)+'h':'—';document.getElementById('dd-duration-sub').textContent=s.drawdown_duration_hours?'since peak':'no drawdown';['spark-equity','spark-daily','spark-weekly','spark-profit','spark-pos'].forEach((id,i)=>drawLine(id,equitySlice().slice(-40),i===3?'#7d3cff':'#1d9bff',false));drawLine('equity-chart',equitySlice(),'#1d9bff',true)}
function renderMarkets(){const heat=document.getElementById('heatmap');const markets=state.markets.length?state.markets:ASSETS.map(a=>({asset:a,change_24h:0,price:0,funding:0,volume_24h:0}));heat.innerHTML=markets.map(m=>{const n=Number(m.change_24h||0);return '<div class="'+(n<0?'red':'')+'">'+m.asset+'<br><span class="'+(n<0?'loss':'gain')+'">'+pct(n)+'</span></div>'}).join('');const btc=markets.find(m=>m.asset==='BTC')||{};document.getElementById('market-regime').textContent=state.positions.length?'Active Risk':(Number(btc.change_24h||0)>1?'Bullish Trend':'Watching');document.getElementById('volatility').textContent=(state.status.effective_leverage||0)>2?'Elevated':'Normal';document.getElementById('funding').textContent=btc.funding!==undefined?(Number(btc.funding)*100).toFixed(4)+'%':'Enabled'}
function renderStrategies(){const trades=state.trades;const by=n=>trades.filter(t=>(t.strategy||'').toLowerCase().includes(n));const cards=[['Mean Reversion','mr','Active','#10e6ff'],['Trend Following','trend','Active','#7d3cff'],['Breakout','breakout','Paused','#ffb11a'],['Scanner Health','scanner','Active','#ff8a00']];document.getElementById('strategy-cards').innerHTML=cards.map(([name,key,tag,color])=>{const ts=by(key),wins=ts.filter(t=>Number(t.pnl_pct)>0).length,wr=ts.length?wins/ts.length*100:0;return '<div class="strategy"><span class="tag">'+tag+'</span><h4 style="margin:0 0 18px;color:'+color+'">'+name+'</h4><table><tr><th>Trades</th><th>Win Rate</th><th>Avg R</th></tr><tr><td>'+ts.length+'</td><td>'+(wr?wr.toFixed(0):'—')+'%</td><td>'+((ts.reduce((a,t)=>a+Number(t.r_multiple||0),0)/(ts.length||1)).toFixed(2))+'R</td></tr></table><canvas class="spark" id="spark-'+key+'"></canvas></div>'}).join('');cards.forEach(([_,key,,color])=>drawLine('spark-'+key,equitySlice().slice(-24),color,false))}
function renderPositions(){const tbody=document.getElementById('positions-body');document.getElementById('position-count').textContent=state.positions.length;if(!state.positions.length){tbody.innerHTML='<tr><td colspan="8" class="empty">No open paper positions yet. The bot is waiting for valid setups.</td></tr>';return}tbody.innerHTML=state.positions.map(p=>{const price=Number(p.entry_price||0)+Number(p.unrealized_pnl||0)/(Number(p.size||1));return '<tr><td>'+p.asset+'/USDT</td><td><span class="badge '+(p.side==='long'?'positive':'negative')+'">'+p.side+'</span></td><td>'+fmtUSD(p.entry_price)+'</td><td>'+fmtUSD(price)+'</td><td class="'+cls(p.unrealized_pnl)+'">'+fmtUSD(p.unrealized_pnl)+'</td><td>'+fmtUSD(p.stop_loss||0)+'</td><td>'+Number(p.leverage||0).toFixed(1)+'x</td><td>'+Number(p.size||0).toFixed(4)+'</td></tr>'}).join('')}
function renderWatchlist(){const q=state.query.toUpperCase();const markets=(state.markets.length?state.markets:ASSETS.map(a=>({asset:a,price:0,change_24h:0,volume_24h:0}))).filter(m=>!q||m.asset.includes(q));document.getElementById('watchlist').innerHTML=markets.map((m,i)=>{const n=Number(m.change_24h||0),conf=Math.max(35,Math.min(92,55+n*8)),rsi=Math.max(25,Math.min(78,50+n*4)).toFixed(1),sig=conf>78?'Strong Buy':conf>58?'+ Buy':n<0?'Caution':'Neutral';return '<tr><td>'+(i+1)+'</td><td>'+m.asset+'/USDT</td><td>'+fmtUSD(m.price)+'</td><td>'+rsi+'</td><td class="'+(n>=0?'gain':'loss')+'">'+(n>=0?'↑':'↓')+'</td><td>'+(Number(m.volume_24h||0)>0?'live':'—')+'</td><td><div style="display:flex;gap:8px;align-items:center"><div class="confidence"><span style="width:'+conf+'%"></span></div>'+Math.round(conf)+'</div></td><td><span class="badge '+(sig==='Caution'?'negative':sig==='Neutral'?'neutral':'positive')+'">'+sig+'</span></td></tr>'}).join('')}
function renderActivity(){const sigs=state.signals.slice(-8).reverse();const acts=[...sigs.map(s=>({t:(s.time||'').slice(11,16),b:'Signal Detected',d:(s.side||'').toUpperCase()+' '+s.asset+' '+(s.strategy||'')+' conf '+Math.round((s.confidence||0)*100)+'%'})),...state.trades.slice(0,4).map(t=>({t:(t.exit_time||'').slice(11,16),b:'Trade Closed',d:t.asset+' '+(Number(t.pnl_pct||0)>=0?'+':'')+Number(t.pnl_pct||0).toFixed(2)+'%'}))];document.getElementById('activity').innerHTML=(acts.length?acts:[{t:'now',b:'System Nominal',d:'Heartbeat, dashboard, and health timer active.'}]).slice(0,6).map(a=>'<div class="event"><small>'+a.t+'</small><div class="pin"></div><div><b>'+a.b+'</b><span class="sub">'+a.d+'</span></div></div>').join('')}
function renderReflection(){const r=state.reflection;let html='<p><b>Today&#39;s Summary</b></p><p class="sub">Paper mode active. Local TA and Altfins metrics are both feeding entry confidence. Health checks restart the bot if snapshots go stale.</p>';if(r&&r.suggestions&&r.suggestions.length){html+='<p><b>Pending Suggestions</b></p>'+r.suggestions.map(s=>'<p class="sub">'+s.parameter+': '+s.current_value+' → '+s.suggested_value+'</p>').join('')}else html+='<p><b>Key Lesson</b></p><p class="sub">Reflection runs after enough closed paper trades.</p>';document.getElementById('reflection').innerHTML=html}
function renderDailyReflection(){const r=state.dailyReflection;if(!r||!r.assets){document.getElementById('daily-reflection').innerHTML='<p class="sub">Waiting for end-of-day data.</p>';return}const sig=r.significant_moves||0;const miss=r.missed_moves||0;const catchable=r.potentially_catchable||0;let html='<p><b>'+r.date+'</b><span class="sub"> · '+r.bias+'</span></p>';html+='<p class="sub">'+sig+' significant moves · '+miss+' missed · '+catchable+' catchable</p>';if(r.learning&&r.learning.length){r.learning.forEach(l=>{if(l.type==='market_summary')return;html+='<div class="badge '+(l.type==='missed_by_config'?'positive':'neutral')+'">'+l.count+'x '+l.reason+'</div><p class="sub">'+l.action+'</p>'})}html+='<div style="max-height:200px;overflow:auto;margin-top:8px">'+r.assets.filter(a=>a.significant).slice(0,10).map(a=>'<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border)"><span>'+a.asset+'</span><span class="'+(a.change_24h_pct>=0?'gain':'loss')+'">'+pct(a.change_24h_pct)+'</span></div>').join('')+'</div>';document.getElementById('daily-reflection').innerHTML=html}
function renderHealth(){const h=state.health;if(!h||!h.checks){document.getElementById('daily-health').innerHTML='<p class="sub">Waiting for first health check.</p>';return}let html='<p><b>'+h.date+'</b><span class="badge '+(h.passed?'positive':'negative')+'">'+(h.passed?'PASS':'FAIL')+'</span></p>';if(h.warnings&&h.warnings.length){html+='<p><b>Warnings ('+h.warnings.length+')</b></p>'+h.warnings.slice(0,5).map(w=>'<p class="sub" style="color:var(--orange)">'+w+'</p>').join('')}if(h.failures&&h.failures.length){html+='<p><b>Failures ('+h.failures.length+')</b></p>'+h.failures.slice(0,5).map(f=>'<p class="sub" style="color:var(--red)">'+f+'</p>').join('')}if(!h.warnings.length&&!h.failures.length){html+='<p class="sub" style="color:var(--green)">All checks passed — no warnings.</p>'}document.getElementById('daily-health').innerHTML=html}
function renderPerAsset(){
  var el=document.getElementById('ab-per-asset');
  if(!el)return;
  if(!state.perAsset||!state.perAsset.length){el.innerHTML='<p class="sub">No trades yet</p>';return}
  var rows=state.perAsset.slice(0,8).map(function(b){
    var color=b.pnl_dollars>=0?'#39f07a':'#ff5d63';
    return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(36,62,96,.4)"><b style="color:#fff;min-width:50px">'+b.key+'</b><span style="color:#aebbd0">'+b.trades+'t '+b.wins+'W/'+b.losses+'L</span><span style="color:'+color+';font-weight:600">$'+b.pnl_dollars.toFixed(0)+'</span></div>'
  }).join('');
  el.innerHTML=rows
}
function renderPerRegime(){
  var el=document.getElementById('ab-per-regime');
  if(!el)return;
  if(!state.perRegime||!state.perRegime.length){el.innerHTML='<p class="sub">No trades yet</p>';return}
  var rows=state.perRegime.slice(0,8).map(function(b){
    var color=b.pnl_dollars>=0?'#39f07a':'#ff5d63';
    return '<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(36,62,96,.4)"><b style="color:#fff;min-width:80px">'+b.key+'</b><span style="color:#aebbd0">'+b.trades+'t '+(b.win_rate*100).toFixed(0)+'%WR</span><span style="color:'+color+';font-weight:600">$'+b.pnl_dollars.toFixed(0)+'</span></div>'
  }).join('');
  el.innerHTML=rows
}
function renderCompare(){
  if(!state.compare||state.compare.error)return;
  var a=state.compare;
  var con=state.status;
  var eqEl=document.getElementById('ab-aggressive-equity');
  var metaEl=document.getElementById('ab-aggressive-meta');
  var cEqEl=document.getElementById('ab-conservative-equity');
  var cMetaEl=document.getElementById('ab-conservative-meta');
  if(eqEl)eqEl.textContent=fmtUSD(a.equity||0);
  if(metaEl)metaEl.textContent=Number(a.total_trades||0)+' trades '+(a.win_rate?(a.win_rate*100).toFixed(0)+'% WR':'0% WR')+' '+(a.profit_factor?Number(a.profit_factor).toFixed(2):'0.00')+' PF';
  if(cEqEl)cEqEl.textContent=fmtUSD(con.equity||0);
  if(cMetaEl)cMetaEl.textContent=Number(con.total_trades||0)+' trades '+(con.win_rate?(con.win_rate*100).toFixed(0)+'% WR':'0% WR')+' '+(con.profit_factor?Number(con.profit_factor).toFixed(2):'0.00')+' PF'
}
function renderLearnings(){const c=state.learnings&&state.learnings.cumulative;if(!c||!c.total_days){document.getElementById('learnings').innerHTML='<p class="sub">Learning accumulation starts after multiple days of data.</p>';return}let html='<p><b>'+c.total_days+' days tracked</b><span class="sub"> · last update '+c.last_updated.slice(0,10)+'</span></p>';if(c.lessons&&c.lessons.length){html+=c.lessons.map(l=>'<div class="badge neutral">Lesson</div><p class="sub">'+l+'</p>').join('')}if(c.persistent_missed_reasons&&c.persistent_missed_reasons.length){html+='<p><b>Persistent Patterns</b></p>'+c.persistent_missed_reasons.slice(0,5).map(r=>'<p class="sub">'+r.reason+': '+r.count+'x</p>').join('')}if(c.most_frequently_bearish&&c.most_frequently_bearish.length){html+='<p><b>Most Bearish Assets</b> <span class="sub">('+c.most_frequently_bearish[0].days+' days)</span></p><div style="display:flex;flex-wrap:wrap;gap:8px">'+c.most_frequently_bearish.slice(0,6).map(a=>'<span class="badge negative">'+a.asset+'</span>').join('')+'</div>'}document.getElementById('learnings').innerHTML=html}
async function load(){try{const [status,trades,positions,equity,signals,reflection,markets,dailyReflection,learnings,health,compare,perAsset,perRegime]=await Promise.all(['/api/status','/api/trades','/api/positions','/api/equity','/api/signals','/api/reflection','/api/markets','/api/daily-reflection','/api/learnings','/api/health','/api/compare','/api/per-asset','/api/per-regime'].map(u=>fetch(u).then(r=>r.json())));state={...state,status,trades,positions,equity,signals,reflection,markets,dailyReflection,learnings,health,compare:compare||{},perAsset:perAsset||[],perRegime:perRegime||[]};renderKpis();renderMarkets();renderStrategies();renderPositions();renderWatchlist();renderActivity();renderReflection();renderDailyReflection();renderLearnings();renderHealth();renderCompare();renderPerAsset();renderPerRegime()}catch(e){console.error(e)}}
document.querySelectorAll('[data-range]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-range]').forEach(x=>x.classList.remove('active'));b.classList.add('active');state.range=b.dataset.range;renderKpis()});document.getElementById('search').addEventListener('input',e=>{state.query=e.target.value;renderWatchlist()});window.addEventListener('resize',()=>renderKpis());load();setInterval(load,60000);
</script>
</body>
</html>"""



def _aggregate_by(trades, key_fn):
    """Aggregate trades by a key function. Returns list of {key, trades, wins, losses, pnl, pnl_pct, avg_r}."""
    buckets = {}
    for t in trades:
        k = key_fn(t)
        if not k:
            continue
        if k not in buckets:
            buckets[k] = []
        buckets[k].append(t)
    results = []
    for k, ts in sorted(buckets.items(), key=lambda x: -len(x[1])):
        wins = [t for t in ts if (t.get("pnl_pct") or 0) > 0]
        losses = [t for t in ts if (t.get("pnl_pct") or 0) <= 0]
        pnl = sum(t.get("pnl_dollars", 0) or 0 for t in ts)
        pnl_pct = sum(t.get("pnl_pct", 0) or 0 for t in ts)
        win_pnl = sum(t.get("pnl_dollars", 0) or 0 for t in wins)
        loss_pnl = abs(sum(t.get("pnl_dollars", 0) or 0 for t in losses))
        pf = win_pnl / loss_pnl if loss_pnl > 0 else 0
        results.append({
            "key": k,
            "trades": len(ts),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(ts) if ts else 0,
            "profit_factor": round(pf, 2),
            "pnl_dollars": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "avg_r": round(sum(t.get("r_multiple", 0) or 0 for t in ts) / len(ts), 3) if ts else 0,
        })
    return results

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, db_path: Path, compare_db_path: Path | None = None, **kwargs):
        self.db_path = db_path
        self.compare_db_path = compare_db_path
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/":
            self._send_html(HTML)
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
            "/api/daily-reflection": self._api_daily_reflection,
            "/api/health": self._api_health,
            "/api/learnings": self._api_learnings,
            "/api/lessons": self._api_lessons,
            "/api/param-changes": self._api_param_changes,
            "/api/per-asset": self._api_per_asset,
            "/api/per-regime": self._api_per_regime,
            "/api/compare": self._api_compare,
            "/api/readiness": self._api_readiness,
            "/api/intents": self._api_intents,
        }.get(api)
        if handler:
            try:
                handler()
            except Exception as e:
                logger.error("API %s: %s", api, e)
                self.send_error(500, str(e))
        else:
            self.send_error(404)

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _api_status(self):
        conn = self._connect()
        snapshot_path = _repo / "data" / "external_snapshot.json"
        snapshot = {}
        try:
            if snapshot_path.exists():
                snapshot = json.loads(snapshot_path.read_text())
        except Exception:
            pass
        altfins_permits = snapshot.get("altfins_permits", {})
        status = {"mode": "paper", "equity": 10000, "peak_equity": 10000, "daily_pnl_pct": 0, "total_trades": 0,
                  "win_rate": 0, "profit_factor": 0, "sharpe": 0, "sortino": 0, "expectancy": 0, "rolling_wr": 0, "rolling_trades": 0, "drawdown_duration_hours": 0, "gross_exposure": 0, "effective_leverage": 0, "allow_entry": True,
                  "altfins_permits": altfins_permits,
                  "altfins_signal_count": snapshot.get("altfins_signal_count", 0),
                  "snapshot_age": None,
                  "coinbase_requests": snapshot.get("coinbase_requests", 0),
                  "coinbase_rate_limited": snapshot.get("coinbase_rate_limited", False),
                  "kalshi_enabled": snapshot.get("kalshi_enabled", False)}
        try:
            row = conn.execute("SELECT equity, peak_equity FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                status.update({"equity": row["equity"], "peak_equity": row["peak_equity"]})
            day_row = conn.execute("SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 1 OFFSET 1440").fetchone()
            if day_row and day_row["equity"]:
                status["daily_pnl_pct"] = ((status["equity"] - day_row["equity"]) / day_row["equity"]) * 100
            pos_row = conn.execute("SELECT value FROM state WHERE key = 'positions'").fetchone()
            if pos_row:
                positions = json.loads(pos_row["value"])
                gross = sum(float(p.get("entry_price", 0)) * abs(float(p.get("size", 0))) for p in positions)
                status["gross_exposure"] = gross
                status["effective_leverage"] = gross / status["equity"] if status["equity"] else 0
            count_row = conn.execute("SELECT COUNT(*) as cnt FROM trades").fetchone()
            status["total_trades"] = count_row["cnt"] if count_row else 0
            trades = conn.execute("SELECT pnl_pct, side FROM trades ORDER BY id DESC LIMIT 100").fetchall()
            if trades:
                wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
                losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] < 0]
                status["win_rate"] = len(wins) / len(trades) if trades else 0
                if losses and sum(losses) != 0:
                    status["profit_factor"] = abs(sum(wins) / sum(losses))
                if len(trades) >= 5:
                    import math
                    returns = [t["pnl_pct"] for t in trades]
                    m = sum(returns) / len(returns)
                    s = math.sqrt(sum((r - m) ** 2 for r in returns) / len(returns))
                    status["sharpe"] = round((m / s) * math.sqrt(365), 2) if s > 0 else 0.0

                # Sortino ratio: downside deviation only
                neg_returns = [r for r in returns if r < 0]
                if neg_returns:
                    dd = math.sqrt(sum(r * r for r in neg_returns) / len(neg_returns))
                    status["sortino"] = round((m / dd) * math.sqrt(365), 2) if dd > 0 else 0.0
                else:
                    status["sortino"] = status["sharpe"]

                # Expectancy in R-multiples
                r_vals = [abs(t["pnl_pct"] / 100) for t in trades[:100]]
                status["expectancy"] = round(sum(r_vals) / len(r_vals), 3) if r_vals else 0

                # Rolling 20-trade win rate
                last_20 = trades[:20]
                if last_20:
                    wins_20 = sum(1 for t in last_20 if t["pnl_pct"] > 0)
                    status["rolling_wr"] = round(wins_20 / len(last_20), 3)
                    status["rolling_trades"] = len(last_20)

                # Drawdown duration from equity snapshots
                try:
                    snaps = conn.execute(
                        "SELECT equity, timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 100"
                    ).fetchall()
                    if snaps and status["peak_equity"] > 0:
                        peak_idx = 0
                        for i, s in enumerate(snaps):
                            if s["equity"] >= status["peak_equity"]:
                                peak_idx = i
                                break
                        if peak_idx > 0:
                            hours_per_snap = 1.0 / 60.0  # 1 snap per minute = 1/60 hour
                            duration_hours = peak_idx * hours_per_snap
                            status["drawdown_duration_hours"] = round(duration_hours, 1)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            conn.close()
        self._send_json(status)


    def _api_per_asset(self):
        """Return per-asset breakdown of trades."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM trades").fetchall()
            trades = [dict(r) for r in rows]
            self._send_json(_aggregate_by(trades, lambda t: t.get("asset", "")))
        finally:
            conn.close()

    def _api_per_regime(self):
        """Return per-regime breakdown of trades."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM trades").fetchall()
            trades = [dict(r) for r in rows]
            self._send_json(_aggregate_by(trades, lambda t: t.get("regime", "") or "unknown"))
        finally:
            conn.close()
    def _api_compare(self):
        """Return status from the comparison bot DB."""
        if not self.compare_db_path:
            self._send_json({"error": "no compare_db_path configured"})
            return
        conn = sqlite3.connect(str(self.compare_db_path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        data = {"equity": 10000, "peak_equity": 10000, "total_trades": 0, "win_rate": 0, "profit_factor": 0, "sharpe": 0, "sortino": 0}
        try:
            r = conn.execute("SELECT equity, peak_equity FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                data.update({"equity": r["equity"], "peak_equity": r["peak_equity"]})
            cnt = conn.execute("SELECT COUNT(*) as c FROM trades").fetchone()
            data["total_trades"] = cnt["c"] if cnt else 0
            trades = conn.execute("SELECT pnl_pct FROM trades ORDER BY id DESC LIMIT 100").fetchall()
            if trades:
                wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
                losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] < 0]
                data["win_rate"] = len(wins) / len(trades) if trades else 0
                if losses and sum(losses) != 0:
                    data["profit_factor"] = abs(sum(wins) / sum(losses))
                if len(trades) >= 5:
                    import math
                    rets = [t["pnl_pct"] for t in trades]
                    m = sum(rets) / len(rets)
                    s = math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets))
                    data["sharpe"] = round((m / s) * math.sqrt(365), 2) if s > 0 else 0.0
        except Exception:
            pass
        finally:
            conn.close()
        self._send_json(data)

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
            state = conn.execute("SELECT value FROM state WHERE key = 'positions'").fetchone()
            if state:
                self._send_json(json.loads(state["value"]))
            else:
                self._send_json([])
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_equity(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT equity, peak_equity, timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 500").fetchall()
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
            snapshot_path = _repo / "data" / "external_snapshot.json"
            snapshot = {}
            if snapshot_path.exists():
                snapshot = json.loads(snapshot_path.read_text())
            funding = snapshot.get("funding", {})
            oi = snapshot.get("oi", {})
            prices = snapshot.get("prices", {})
            changes_24h = snapshot.get("change_24h", {})

            assets_in_data = set(list(funding.keys())[:20])
            markets = []
            for asset in ASSETS:
                if asset not in assets_in_data:
                    continue
                price = prices.get(asset, 0)
                markets.append({
                    "asset": asset,
                    "price": price,
                    "funding_rate": funding.get(asset, 0),
                    "open_interest": oi.get(asset, 0),
                    "change_24h": changes_24h.get(asset, 0),
                })
            _MARKET_CACHE["data"] = markets
            _MARKET_CACHE["ts"] = now
            self._send_json(markets)
        except Exception:
            self._send_json(_MARKET_CACHE["data"] or [])

    def _api_signals(self):
        conn = self._connect()
        try:
            state = conn.execute("SELECT value FROM state WHERE key = 'daily_signals'").fetchone()
            if state:
                self._send_json(json.loads(state["value"]))
            else:
                self._send_json([])
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_reflection(self):
        conn = self._connect()
        try:
            state = conn.execute("SELECT value FROM state WHERE key = 'weekly_reflection'").fetchone()
            if state:
                self._send_json(json.loads(state["value"]))
            else:
                self._send_json({})
        except Exception:
            self._send_json({})
        finally:
            conn.close()

    def _api_daily_reflection(self):
        conn = self._connect()
        try:
            state = conn.execute("SELECT value FROM state WHERE key = 'daily_reflection'").fetchone()
            if state:
                self._send_json(json.loads(state["value"]))
            else:
                self._send_json({"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "assets": [], "learning": []})
        except Exception:
            self._send_json({})
        finally:
            conn.close()

    def _api_health(self):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM state WHERE key = 'daily_health'").fetchone()
            if row:
                result = json.loads(row["value"])
            else:
                result = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "passed": True, "checks": [], "warnings": [], "failures": []}
            self._send_json(result)
        except Exception:
            self._send_json({"passed": True, "checks": []})
        finally:
            conn.close()

    def _api_lessons(self):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT pattern_category, pattern_detail, SUM(frequency_count) as total, COUNT(DISTINCT date) as days_seen "
                "FROM lessons GROUP BY pattern_category, pattern_detail ORDER BY total DESC LIMIT 30"
            ).fetchall()
            self._send_json([dict(r) for r in rows])
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_param_changes(self):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM parameter_changes ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            self._send_json([dict(r) for r in rows])
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def _api_learnings(self):
        conn = self._connect()
        try:
            cumulative = conn.execute("SELECT value FROM state WHERE key = 'cumulative_learnings'").fetchone()
            asset_learnings = conn.execute("SELECT value FROM state WHERE key = 'asset_learnings'").fetchone()
            result = {
                "cumulative": json.loads(cumulative["value"]) if cumulative else {},
                "asset_learnings": json.loads(asset_learnings["value"]) if asset_learnings else {},
            }
            self._send_json(result)
        except Exception:
            self._send_json({})
        finally:
            conn.close()

    def _api_readiness(self):
        conn = self._connect()
        try:
            trades = conn.execute("SELECT pnl_pct, strategy FROM trades ORDER BY id DESC LIMIT 500").fetchall()
            pnls = [float(t["pnl_pct"] or 0.0) for t in trades]
            mr = [float(t["pnl_pct"] or 0.0) for t in trades if (t["strategy"] or "") == "mr"]
            trend = [float(t["pnl_pct"] or 0.0) for t in trades if (t["strategy"] or "") == "trend"]
            eq = conn.execute("SELECT equity, peak_equity FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            equity = float(eq["equity"]) if eq else 10000.0
            peak = float(eq["peak_equity"]) if eq else equity
            dd = ((peak - equity) / peak * 100) if peak > 0 else 0.0

            def win_rate(vals):
                return sum(1 for v in vals if v > 0) / len(vals) if vals else 0.0

            def profit_factor(vals):
                wins = sum(v for v in vals if v > 0)
                losses = abs(sum(v for v in vals if v < 0))
                return wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)

            checks = {
                "min_trades_50": len(pnls) >= 50,
                "mr_pf_gt_1_5": profit_factor(mr) > 1.5 if mr else False,
                "trend_pf_gt_1_2": profit_factor(trend) > 1.2 if trend else False,
                "drawdown_lt_15": dd < 15.0,
                "mr_wr_gt_0_55": win_rate(mr) > 0.55 if mr else False,
                "trend_wr_gt_0_40": win_rate(trend) > 0.40 if trend else False,
            }
            passed = sum(1 for v in checks.values() if v)
            self._send_json({
                "ready": passed >= 5,
                "checks_passed": passed,
                "checks_total": len(checks),
                "details": checks,
                "stats": {
                    "total_trades": len(pnls),
                    "drawdown_pct": round(dd, 2),
                    "mr_profit_factor": round(profit_factor(mr), 2) if mr else None,
                    "trend_profit_factor": round(profit_factor(trend), 2) if trend else None,
                    "mr_win_rate": round(win_rate(mr), 3) if mr else None,
                    "trend_win_rate": round(win_rate(trend), 3) if trend else None,
                },
            })
        except Exception:
            self._send_json({"ready": False, "error": "readiness_unavailable"})
        finally:
            conn.close()

    def _api_intents(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM trade_intents ORDER BY id DESC LIMIT 100").fetchall()
            intents = []
            for row in rows:
                item = dict(row)
                try:
                    item["components"] = json.loads(item.get("components") or "[]")
                    item["payload"] = json.loads(item.get("payload") or "{}")
                except Exception:
                    pass
                intents.append(item)
            self._send_json(intents)
        except Exception:
            self._send_json([])
        finally:
            conn.close()

    def log_message(self, fmt, *args):
        pass


class ThreadingServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(db_path: Path = Path("data/hermes.db"), port: int = 8081, compare_db_path: Path | None = None):
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
    parser.add_argument("db_path", nargs="?", default="data/hermes.db", help="Primary database path")
    parser.add_argument("port", nargs="?", type=int, default=8081, help="HTTP port")
    parser.add_argument("--second-db", type=str, default=None, help="Aggressive bot DB path for comparison")
    args = parser.parse_args()
    db = Path(args.db_path)
    port = args.port
    compare = Path(args.second_db) if args.second_db else None
    serve(db, port, compare)
