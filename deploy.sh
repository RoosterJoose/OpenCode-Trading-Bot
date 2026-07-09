#!/bin/bash
set -euo pipefail
REPO_CONS=/opt/hermes-trading-bot
REPO_AGGR=/opt/hermes-trading-bot-aggressive
BRANCH=master

echo "=== HERMES DEPLOY v2 ==="
echo ""

# Step 1: static analysis
echo "[1/6] Running verify_method_calls.py..."
python3 $REPO_CONS/scripts/verify_method_calls.py $REPO_CONS/src/core/loop.py $REPO_CONS/src/strategies/*.py || { echo "FAILED: bad method calls"; exit 1; }
echo "  PASS"

# Step 2: commit and push CONS
echo "[2/6] Pushing CONS..."
cd $REPO_CONS
sudo -u hermes git add -A
if ! sudo -u hermes git diff --cached --quiet; then
    sudo -u hermes git commit -m "auto-deploy $(date '+%Y-%m-%d %H:%M UTC')"
    sudo -u hermes git push origin $BRANCH
    echo "  Pushed"
else
    echo "  No changes to push"
fi

# Step 3: pull AGGR
echo "[3/6] Pulling AGGR..."
sudo -u hermes git -C $REPO_AGGR fetch origin $BRANCH
LOCAL=$(sudo -u hermes git -C $REPO_AGGR rev-parse $BRANCH)
REMOTE=$(sudo -u hermes git -C $REPO_AGGR rev-parse origin/$BRANCH)
if [ "$LOCAL" != "$REMOTE" ]; then
    sudo -u hermes git -C $REPO_AGGR pull origin $BRANCH
    echo "  Pulled ($(sudo -u hermes git -C $REPO_AGGR rev-list --count $LOCAL..$REMOTE) new commits)"
else
    echo "  AGGR already up to date"
fi

# Step 4: clear pycache
echo "[4/6] Clearing __pycache__..."
find $REPO_CONS/src -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find $REPO_AGGR/src -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "  Done"

# Step 5: compile-check both bots
echo "[5/6] Compile-checking both bots..."
sudo -u hermes python3 -m py_compile $REPO_CONS/src/core/loop.py && echo "  CONS: loop.py OK"
sudo -u hermes python3 -m py_compile $REPO_AGGR/src/core/loop.py && echo "  AGGR: loop.py OK"

# Step 6: restart both bots
echo "[6/6] Restarting both bots..."
sudo systemctl reset-failed hermes-bot hermes-bot-aggressive 2>/dev/null || true
sudo systemctl restart hermes-bot
sudo systemctl restart hermes-bot-aggressive
echo "  Restarted. Waiting 20s..."
sleep 20

# Verify
CONS_OK=$(sudo systemctl is-active hermes-bot)
AGGR_OK=$(sudo systemctl is-active hermes-bot-aggressive)
echo "  CONS: $CONS_OK"
echo "  AGGR: $AGGR_OK"

if [ "$CONS_OK" != "active" ] || [ "$AGGR_OK" != "active" ]; then
    echo "DEPLOY FAILED: one or both bots not active"
    exit 1
fi

# Run invariant sweep
echo ""
echo "=== Post-deploy Invariant Sweep ==="
python3 $REPO_CONS/scripts/invariant_sweep.py 2>/dev/null || true

echo ""
echo "=== DEPLOY COMPLETE === $(date)"