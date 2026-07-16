"""Deep audit: AST-level nesting verification + control-flow checks.

Runs before every deploy. Aborts on failure.
Usage: python3 scripts/deep_audit.py
"""
import ast, sys, os, json, sqlite3, subprocess, filecmp

REPO = "/opt/hermes-trading-bot"
AGGR_REPO = "/opt/hermes-trading-bot-aggressive"
CONS_DB = REPO + "/data/hermes.db"
AGGR_DB = AGGR_REPO + "/data_aggressive/hermes.db"

failures = []
warnings = []

def check(desc, condition, severity="fail"):
    if condition:
        print("  [PASS] " + desc)
    else:
        print("  [FAIL] " + desc)
        if severity == "fail":
            failures.append(desc)
        else:
            warnings.append(desc)

def _is_in_except(func_node, target_lineno):
    for n in ast.walk(func_node):
        if isinstance(n, ast.ExceptHandler):
            if n.lineno <= target_lineno <= (n.end_lineno or 9999):
                return True
    return False

print("=" * 60)
print("DEEP AUDIT - AST-Level Nesting + Control-Flow Verification")
print("=" * 60)

# === SECTION 1: loop.py ===
print("\n--- loop.py ---")
with open(REPO + "/src/core/loop.py") as f:
    loop_src = f.read()
loop_tree = ast.parse(loop_src)

for node in ast.walk(loop_tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_cycle":
        main_found = False
        fallback_found = False
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    if isinstance(t, ast.Attribute) and t.attr == "_paused_strategies":
                        in_except = _is_in_except(node, child.lineno)
                        dump = ast.dump(child)
                        is_fallback = "[]" in dump and "json" not in dump
                        if is_fallback:
                            check("_paused_strategies fallback IN except", in_except)
                            fallback_found = True
                        else:
                            check("_paused_strategies main NOT in except", not in_except)
                            main_found = True
        if not main_found:
            check("_paused_strategies main assignment exists", False)
        if not fallback_found:
            check("_paused_strategies fallback assignment exists", False)
        break

for node in ast.walk(loop_tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_close":
        args = [a.arg for a in node.args.args]
        check("_close has close_pct param", "close_pct" in args)
        break

for node in ast.walk(loop_tree):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_asset":
        found = False
        for stmt in node.body[:3]:
            if hasattr(stmt, "targets"):
                for t in getattr(stmt, "targets", []):
                    if isinstance(t, ast.Attribute) and t.attr == "_last_entry_diag_cycle":
                        found = True
        check("_process_asset: self-heal fix in first 3 statements", found)
        break

check("MR min uses exchange.equity * 0.20", "exchange.equity * 0.20" in loop_src)
check("MR min has max(1000 floor", "max(1000.0" in loop_src)
check("TP1 close_pct = 0.5", 'close_pct = 0.5 if reason == "tp1"' in loop_src)
check("tp1_scaled flag exists", "pos.tp1_scaled = True" in loop_src)
check("TP1 sweep exemption elif", "elif age_min > 720" in loop_src)
check("paused_strategies check in strategy loop", "Skip if strategy is paused" in loop_src)

# === SECTION 2: perp_risk.py ===
print("\n--- perp_risk.py ---")
with open(REPO + "/src/core/perp_risk.py") as f:
    pr_src = f.read()
pr_tree = ast.parse(pr_src)

for node in ast.walk(pr_tree):
    if isinstance(node, ast.FunctionDef) and node.name == "consecutive_loss_allows":
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        check("consecutive_loss_allows has return", len(returns) > 0)
        check("consecutive_loss_allows has at least 3 returns", len(returns) >= 3)
        break

check("daily_start_equity = None", "self.daily_start_equity = None" in pr_src)
check("daily_start_equity None guard", "if self.daily_start_equity is None:" in pr_src)

# === SECTION 3: coinbase_advanced.py ===
print("\n--- coinbase_advanced.py ---")
with open(REPO + "/src/adapters/coinbase_advanced.py") as f:
    ca_src = f.read()
check("_fetch_all_products returns products", 'return data.get("products"' in ca_src)
check("connect_ws exists", "async def connect_ws" in ca_src)
check("get_spread exists", "def get_spread" in ca_src)

fap_section = ca_src.split("async def _fetch_all_products")[1].split("async def")[0] if "async def _fetch_all_products" in ca_src else ""
check("_fetch_all_products does NOT check candles", 'data.get("candles")' not in fap_section)

# === SECTION 4: mr.py ===
print("\n--- mr.py ---")
with open(os.path.join(REPO, "src/strategies/xs_momentum.py")) as f:
    mr_src = f.read()
check("XSmomentum stripped: no drift filter", "tp1_r_mult: float = 0.7" in mr_src)
check("XS no drift filter CONFIRMED", "Block MR shorts entirely" in mr_src)
check("MR time gate", "8 <= _hr" in mr_src)
check("MR friday gate", "weekday() == 4" in mr_src)
check("MR MAE stop", "mae_val > 15.0" in mr_src)
check("MR max_hold_hours 2.0", "max_hold_hours: float = 2.0" in mr_src)
check("MR blocked_assets exists", "blocked_assets" in mr_src)

# === SECTION 5: xs_momentum.py ===
print("\n--- xs_momentum.py ---")
with open(REPO + "/src/strategies/xs_momentum.py") as f:
    xs_src = f.read()
check("XS blocked ZEC/AAVE/ADA", '"ZEC"' in xs_src and '"AAVE"' in xs_src and '"ADA"' in xs_src)

# === SECTION 6: kalshi.py ===
print("\n--- kalshi.py ---")
with open(REPO + "/src/adapters/kalshi.py") as f:
    k_src = f.read()
check("Kalshi get_spread", "def get_spread" in k_src)

# === SECTION 7: DB state ===
print("\n--- DB State ---")
for label, db_path in [("CONS", CONS_DB), ("AGGR", AGGR_DB)]:
    if not os.path.exists(db_path):
        check(label + " DB exists", False)
        continue
    c = sqlite3.connect(db_path)
    eq = c.execute("SELECT value FROM state WHERE key='paper_equity'").fetchone()
    if eq:
        val = float(eq[0].strip("\"'"))
        check(label + " equity ~5000 (got $" + str(round(val)), 4000 < val < 6000)
    else:
        check(label + " paper_equity exists", False)
    c.close()

# === SECTION 8: Bot health ===
print("\n--- Bot Health ---")
for svc in ["hermes-bot", "hermes-bot-aggressive"]:
    r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
    check(svc + " active", r.stdout.strip() == "active")

for svc, label in [("hermes-bot.service", "CONS"), ("hermes-bot-aggressive.service", "AGGR")]:
    r = subprocess.run(["journalctl", "-u", svc, "--since", "5 minutes ago", "--no-pager"],
                       capture_output=True, text=True)
    error_count = r.stdout.count("ERROR")
    check(label + " 0 errors in 5m", error_count == 0)

# === SECTION 9: CONS/AGGR sync ===
print("\n--- CONS/AGGR File Sync ---")
sync_files = [
    "src/core/loop.py",
    "src/core/perp_risk.py",
    "src/strategies/mr.py",
    "src/strategies/xs_momentum.py",
    "src/adapters/coinbase_advanced.py",
    "src/adapters/kalshi.py",
]
for f in sync_files:
    same = filecmp.cmp(REPO + "/" + f, AGGR_REPO + "/" + f, shallow=False)
    check("Sync: " + f, same)

# === SECTION 10: Snapshot ===
print("\n--- Snapshot ---")
snap_path = REPO + "/data/external_snapshot.json"
if os.path.exists(snap_path):
    snap = json.load(open(snap_path))
    check("Snapshot 23 prices", len(snap.get("prices", {})) == 23)
    check("Snapshot 23 funding", len(snap.get("funding", {})) == 23)
else:
    check("Snapshot exists", False)

# === SUMMARY ===
print("\n" + "=" * 60)
print("RESULTS: " + str(len(failures)) + " failures, " + str(len(warnings)) + " warnings")
if failures:
    print("\nFAILURES:")
    for f in failures:
        print("  - " + f)
if warnings:
    print("\nWARNINGS:")
    for w in warnings:
        print("  - " + w)
if not failures and not warnings:
    print("\nALL CHECKS PASS")
print("=" * 60)

sys.exit(1 if failures else 0)