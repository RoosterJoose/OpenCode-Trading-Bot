#!/usr/bin/env python3
"""
Comprehensive invariant checker — covers every failure mode we've ever hit.
Runs as standalone script and wires into bot cycle via Telegram /audit.
"""
import sqlite3, json, os, time, subprocess, logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("hermes.invariant")

def check_db(path, label):
    issues = []
    aged = {}
    c = sqlite3.connect(path)

    def get_state(key):
        row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if not row: return None
        try: return json.loads(row[0])
        except: return row[0]

    def get_float(key):
        val = get_state(key)
        if val is None: return 0.0
        if isinstance(val, str): return float(val.strip('"'))
        return float(val)

    # ── 1. Budget freshness ──────────────────────────────────────────
    raw = get_state("strategy_budget")
    if isinstance(raw, dict):
        bts = raw.get("timestamp", 0)
        ba = time.time() - bts
        aged["budget_h"] = round(ba / 3600, 1)
        if ba > 7200:
            issues.append(("WARNING", f"[{label}] Budget stale {ba/3600:.1f}h"))
        elif ba > 3600:
            issues.append(("INFO", f"[{label}] Budget age {ba/3600:.1f}h"))
        if "drift_momentum" in raw.get("weights", {}):
            issues.append(("WARNING", f"[{label}] Dead strategy in budget"))
    else:
        issues.append(("WARNING", f"[{label}] No budget data"))

    # ── 2. Trade activity ──────────────────────────────────────────
    lt = c.execute("SELECT COUNT(*) FROM trades WHERE created_at >= datetime('now', '-2 hours')").fetchone()[0]
    if lt == 0:
        issues.append(("WARNING", f"[{label}] No trades in 2h"))

    dpnl = c.execute("SELECT COALESCE(SUM(pnl_dollars),0) FROM trades WHERE created_at >= datetime('now', 'start of day')").fetchone()[0]
    aged["day_pnl"] = round(dpnl, 2)

    # ── 3. Candle staleness ──────────────────────────────────────────
    cutoff = datetime.now(timezone.utc).timestamp() - 3600
    rows = c.execute("SELECT asset, MAX(timestamp) as latest FROM candles GROUP BY asset").fetchall()
    stale = [(r[0], r[1]) for r in rows if r[1] < cutoff]
    if stale:
        worst = max(stale, key=lambda x: time.time() - x[1])
        aged["stale_candles"] = {"count": len(stale), "worst": worst[0], "age_h": round((time.time() - worst[1]) / 3600, 1)}
        if len(stale) > 5:
            issues.append(("WARNING", f"[{label}] {len(stale)} stale candles: worst {worst[0]} {aged['stale_candles']['age_h']:.1f}h"))

    # ── 4. Circuit breakers ──────────────────────────────────────────
    gs = int(get_float("risk_global_loss_streak"))
    recent = get_state("risk_recent_outcomes") or []
    paused = get_state("bot_paused") or False
    if isinstance(paused, str):
        paused = paused.strip('"') == "true"
    aged["gs"] = gs
    aged["paused"] = paused
    if gs >= 3:
        issues.append(("WARNING", f"[{label}] GS={gs}"))
    if paused:
        issues.append(("CRITICAL", f"[{label}] BOT PAUSED"))
    if recent and len(recent) >= 5:
        n = min(len(recent), 20)
        wr = sum(1 for r in recent[-n:] if r) / n
        aged["wr"] = round(wr, 3)
        if wr < 0.15:
            issues.append(("WARNING", f"[{label}] WR {wr*100:.0f}% on last {n}"))

    # ── 5. Errors ────────────────────────────────────────────────────
    service = "hermes-bot-aggressive" if "aggre" in path.lower() else "hermes-bot"
    try:
        r = subprocess.run(["sudo", "journalctl", "-u", service, "--since", "30 minutes ago", "--no-pager"],
                           capture_output=True, text=True, timeout=10)
        real_errs = [l for l in r.stdout.split('\n') if 'ERROR' in l and '429' not in l and 'Unbound' not in l and 'altfins' not in l.lower()]
        aged["errors_30m"] = len(real_errs)
        if len(real_errs) > 5:
            issues.append(("WARNING", f"[{label}] {len(real_errs)} real errors in 30m"))
        elif len(real_errs) > 0:
            issues.append(("INFO", f"[{label}] {len(real_errs)} errors in 30m"))
    except Exception as e:
        aged["errors_30m"] = -1

    # ── 6. Equity/drawdown ──────────────────────────────────────────
    eq = get_float("paper_equity")
    peak = get_float("paper_peak_equity")
    aged["paper_equity"] = round(eq, 0) if eq else 0
    if peak > 0:
        dd = (peak - eq) / peak * 100
        aged["dd_pct"] = round(dd, 1)
        if dd > 8:
            issues.append(("WARNING", f"[{label}] DD {dd:.1f}%"))
        elif dd > 5:
            issues.append(("INFO", f"[{label}] DD {dd:.1f}%"))

    c.close()
    return issues, aged


def format_report(cons_issues, cons_aged, aggr_issues, aggr_aged):
    lines = ["Hermes Invariant Sweep"]
    for label, aged, issues in [("CONS", cons_aged, cons_issues), ("AGGR", aggr_aged, aggr_issues)]:
        parts = [
            f"${aged.get('paper_equity',0):.0f}",
            f"PnL=${aged.get('day_pnl',0):+.0f}",
            f"DD={aged.get('dd_pct',0):.1f}%",
            f"GS={aged.get('gs',0)}",
            f"WR={aged.get('wr',0):.0%}" if 'wr' in aged else "",
        ]
        lines.append(f"  {label}: {' '.join(p for p in parts if p)}")

    all_issues = cons_issues + aggr_issues
    if all_issues:
        lines.append("")
        lines.append("ISSUES:")
        for severity, msg in sorted(all_issues):
            icon = {"CRITICAL": "CRIT", "WARNING": "WARN", "INFO": "INFO"}.get(severity, "?")
            lines.append(f"  [{icon}] {msg}")
    else:
        lines.append("")
        lines.append("All invariants PASS")

    return "\n".join(lines)


if __name__ == "__main__":
    c_issues, c_aged = check_db("/opt/hermes-trading-bot/data/hermes.db", "CONS")
    a_issues, a_aged = check_db("/opt/hermes-trading-bot-aggressive/data_aggressive/hermes.db", "AGGR")
    print(format_report(c_issues, c_aged, a_issues, a_aged))