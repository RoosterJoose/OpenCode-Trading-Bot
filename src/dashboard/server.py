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
from functools import lru_cache
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

# Resolve project root
_repo = Path(__file__).resolve().parent.parent.parent

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes v2 — Dashboard</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9; --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; background: var(--bg); color: var(--text); padding: 20px; }
  h1 { font-size: 1.4rem; margin-bottom: 4px; color: #fff; }
  .subtitle { color: #8b949e; font-size: 0.85rem; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .card h3 { font-size: 0.75rem; text-transform: uppercase; color: #8b949e; margin-bottom: 8px; letter-spacing: 0.5px; }
  .card .value { font-size: 1.5rem; font-weight: 600; }
  .card .value.green { color: var(--green); }
  .card .value.red { color: var(--red); }
  .card .value.yellow { color: var(--yellow); }
  .card .sub { font-size: 0.8rem; color: #8b949e; margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: #8b949e; font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }
  .section-title { font-size: 1rem; margin: 24px 0 12px; color: #fff; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: 500; }
  .badge.positive { background: rgba(63, 185, 80, 0.15); color: var(--green); }
  .badge.negative { background: rgba(248, 81, 73, 0.15); color: var(--red); }
  .badge.neutral { background: rgba(88, 166, 255, 0.15); color: var(--blue); }
  .badge.warning { background: rgba(210, 153, 34, 0.15); color: var(--yellow); }
  #equity-chart { width: 100%; height: 200px; }
  .flex-row { display: flex; gap: 20px; flex-wrap: wrap; }
  .flex-row > * { flex: 1; min-width: 300px; }
  .signal-entry { padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 0.82rem; }
  .signal-entry:last-child { border: none; }
  .signal-time { color: #8b949e; font-size: 0.75rem; }
  pre.reflection { font-size: 0.8rem; white-space: pre-wrap; color: #8b949e; line-height: 1.5; }
  .loading { color: #8b949e; text-align: center; padding: 40px; }
  @media (max-width: 700px) { .grid { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
  <h1>Hermes v2</h1>
  <div class="subtitle">Hyperliquid Perp Bot <span id="mode">—</span> <span id="uptime"></span></div>

  <div class="grid" id="stats-grid">
    <div class="card"><h3>Equity</h3><div class="value" id="equity">—</div><div class="sub" id="equity-change"></div></div>
    <div class="card"><h3>Drawdown</h3><div class="value" id="dd">—</div><div class="sub">peak: <span id="peak">—</span></div></div>
    <div class="card"><h3>Open Positions</h3><div class="value" id="positions">—</div><div class="sub" id="exposure">exposure: —</div></div>
    <div class="card"><h3>Total Trades</h3><div class="value" id="trades">—</div><div class="sub">win rate: <span id="wr">—</span></div></div>
    <div class="card"><h3>Profit Factor</h3><div class="value" id="pf">—</div><div class="sub">sharpe: <span id="sharpe">—</span></div></div>
    <div class="card"><h3>Risk Status</h3><div class="value" id="risk-status">—</div><div class="sub">leverage: <span id="lev">—</span>x</div></div>
  </div>

  <div class="flex-row">
    <div>
      <div class="section-title">Equity Curve</div>
      <div class="card"><canvas id="equity-chart"></canvas></div>
    </div>
    <div>
      <div class="section-title">Open Positions</div>
      <div class="card" id="positions-table"><div class="loading">Loading...</div></div>
    </div>
  </div>

  <div class="section-title">Recent Trades</div>
  <div class="card" id="trades-table"><div class="loading">Loading...</div></div>

  <div class="section-title">Signal Log</div>
  <div class="card" id="signals"><div class="loading">Loading...</div></div>

  <div class="section-title">Weekly Reflection</div>
  <div class="card" id="reflection"><div class="loading">Loading...</div></div>

<script>
async function load() {
  const [status, trades, positions, equity, signals, reflection] = await Promise.all([
    fetch('/api/status').then(r=>r.json()),
    fetch('/api/trades').then(r=>r.json()),
    fetch('/api/positions').then(r=>r.json()),
    fetch('/api/equity').then(r=>r.json()),
    fetch('/api/signals').then(r=>r.json()),
    fetch('/api/reflection').then(r=>r.json()),
  ]);
  const mode = status.mode || 'paper'; const eq = status.equity || 0; const peak = status.peak_equity || eq;
  const dd = peak > 0 ? ((peak - eq) / peak * 100) : 0;
  document.getElementById('mode').textContent = mode;
  document.getElementById('equity').textContent = '$' + eq.toFixed(0);
  document.getElementById('equity-change').textContent = (status.daily_pnl_pct || 0) >= 0 ? '+' + (status.daily_pnl_pct || 0).toFixed(2) + '% today' : (status.daily_pnl_pct || 0).toFixed(2) + '% today';
  document.getElementById('equity').className = 'value ' + ((status.daily_pnl_pct || 0) >= 0 ? 'green' : 'red');
  document.getElementById('dd').textContent = dd.toFixed(1) + '%';
  document.getElementById('dd').className = 'value ' + (dd > 8 ? 'red' : dd > 4 ? 'yellow' : 'green');
  document.getElementById('peak').textContent = '$' + peak.toFixed(0);
  document.getElementById('positions').textContent = (positions || []).length;
  document.getElementById('exposure').textContent = 'exposure: $' + (status.gross_exposure || 0).toFixed(0);
  document.getElementById('trades').textContent = status.total_trades || 0;
  document.getElementById('wr').textContent = status.win_rate ? (status.win_rate * 100).toFixed(0) + '%' : '—';
  document.getElementById('pf').textContent = status.profit_factor ? status.profit_factor.toFixed(2) : '—';
  document.getElementById('sharpe').textContent = status.sharpe ? status.sharpe.toFixed(2) : '—';
  document.getElementById('risk-status').textContent = status.allow_entry ? 'Trading' : 'Halted';
  document.getElementById('risk-status').className = 'value ' + (status.allow_entry ? 'green' : 'red');
  document.getElementById('lev').textContent = (status.effective_leverage || 0).toFixed(2);

  // Equity chart
  const canvas = document.getElementById('equity-chart');
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.parentElement.clientWidth;
  canvas.height = 200;
  const pts = (equity || []).reverse();
  if (pts.length > 1) {
    const min = Math.min(...pts.map(p => p.equity || 0));
    const max = Math.max(...pts.map(p => p.equity || 0));
    const range = max - min || 1;
    ctx.fillStyle = '#161b22';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = (i / (pts.length - 1)) * canvas.width;
      const y = canvas.height - (((p.equity || 0) - min) / range) * (canvas.height - 20) - 10;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#3fb950';
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = '#3fb95020';
    ctx.lineTo(canvas.width, canvas.height);
    ctx.lineTo(0, canvas.height);
    ctx.closePath();
    ctx.fill();
  }

  // Positions
  const ptbl = document.getElementById('positions-table');
  if (positions && positions.length > 0) {
    ptbl.innerHTML = '<table><tr><th>Asset</th><th>Side</th><th>Size</th><th>Entry</th><th>uPnL</th><th>Liq</th></tr>' +
      positions.map(p => `<tr><td>${p.asset}</td><td><span class="badge ${p.side === 'long' ? 'positive' : 'negative'}">${p.side.toUpperCase()}</span></td><td>${p.size.toFixed(4)}</td><td>$${p.entry_price.toFixed(2)}</td><td class="${p.unrealized_pnl >= 0 ? 'green' : 'red'}">${p.unrealized_pnl >= 0 ? '+' : ''}$${p.unrealized_pnl.toFixed(0)}</td><td>$${p.liquidation_price.toFixed(2)}</td></tr>`).join('') +
      '</table>';
  } else { ptbl.innerHTML = '<div style="color:#8b949e;padding:8px;">No open positions</div>'; }

  // Trades
  const ttbl = document.getElementById('trades-table');
  if (trades && trades.length > 0) {
    ttbl.innerHTML = '<table><tr><th>Time</th><th>Asset</th><th>Side</th><th>P&L</th><th>R</th><th>Reason</th><th>Strategy</th></tr>' +
      trades.slice(0, 20).map(t => `<tr><td>${(t.exit_time || '').slice(0, 16)}</td><td>${t.asset}</td><td><span class="badge ${t.side === 'long' ? 'positive' : 'negative'}">${t.side.toUpperCase()}</span></td><td class="${t.pnl_pct >= 0 ? 'green' : 'red'}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(1)}%</td><td>${t.r_multiple ? t.r_multiple.toFixed(2) : '—'}</td><td>${t.exit_reason || '—'}</td><td>${t.strategy || '—'}</td></tr>`).join('') +
      '</table>';
  } else { ttbl.innerHTML = '<div style="color:#8b949e;padding:8px;">No closed trades yet</div>'; }

  // Signals
  const sigs = document.getElementById('signals');
  if (signals && signals.length > 0) {
    sigs.innerHTML = signals.slice(-20).reverse().map(s => {
      const conf = s.confidence || 0;
      return `<div class="signal-entry"><span class="signal-time">${(s.time || '').slice(11, 19)}</span> <span class="badge ${s.side === 'long' ? 'positive' : 'negative'}">${s.side.toUpperCase()}</span> <strong>${s.asset}</strong> ${s.strategy} conf=${(conf * 100).toFixed(0)}% entry=$${s.entry_price} stop=$${s.stop_price}</div>`;
    }).join('');
  } else { sigs.innerHTML = '<div style="color:#8b949e;padding:8px;">No signals generated yet</div>'; }

  // Reflection
  const ref = document.getElementById('reflection');
  if (reflection && reflection.suggestions && reflection.suggestions.length > 0) {
    ref.innerHTML = '<pre class="reflection">' + JSON.stringify(reflection, null, 2) + '</pre>';
  } else { ref.innerHTML = '<div style="color:#8b949e;padding:8px;">No reflection data yet (runs Sundays)</div>'; }
}
load();
setInterval(load, 15000);
</script>
</body>
</html>"""


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, db_path: Path, **kwargs):
        self.db_path = db_path
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
        handlers = {
            "/api/status": self._api_status,
            "/api/trades": self._api_trades,
            "/api/positions": self._api_positions,
            "/api/equity": self._api_equity,
            "/api/signals": self._api_signals,
            "/api/reflection": self._api_reflection,
        }
        handler = handlers.get(api)
        if handler:
            handler()
        else:
            self.send_error(404)

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _api_status(self):
        conn = self._connect()
        status = {"mode": "paper", "equity": 10000, "peak_equity": 10000, "daily_pnl_pct": 0, "total_trades": 0,
                  "win_rate": 0, "profit_factor": 0, "sharpe": 0, "gross_exposure": 0, "effective_leverage": 0, "allow_entry": True}
        try:
            row = conn.execute("SELECT equity, peak_equity FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                status.update({"equity": row["equity"], "peak_equity": row["peak_equity"]})
            trades = conn.execute("SELECT pnl_pct, side FROM trades ORDER BY id DESC LIMIT 100").fetchall()
            if trades:
                status["total_trades"] = len(trades)
                wins = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
                losses = [t["pnl_pct"] for t in trades if t["pnl_pct"] < 0]
                status["win_rate"] = len(wins) / len(trades) if trades else 0
                if losses and sum(losses) != 0:
                    status["profit_factor"] = abs(sum(wins) / sum(losses))
        except Exception:
            pass
        finally:
            conn.close()
        self._send_json(status)

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

    def log_message(self, fmt, *args):
        pass


def serve(db_path: Path = Path("data/hermes.db"), port: int = 8080):
    class Handler(DashboardHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, db_path=db_path, **kwargs)

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard: http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/hermes.db")
    serve(db)
