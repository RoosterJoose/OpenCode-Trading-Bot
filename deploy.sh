#!/usr/bin/env bash
set -euo pipefail

# ── Hermes v2 — Oracle Cloud Deploy ──────────────────────────────────────
# Run this ONCE on your Oracle VPS. It sets up everything:
#   1. Clones the repo
#   2. Installs system packages + Python deps
#   3. Creates a systemd service that runs on boot
#   4. Sets up a 5-min auto-update timer (pulls new commits, restarts)
#   5. Starts the bot
#
# Usage:
#   curl -sL https://raw.githubusercontent.com/.../deploy.sh | bash
#   (or scp this file to the VPS and run it)
# ───────────────────────────────────────────────────────────────────────────

REPO="https://github.com/RoosterJoose/OpenCode-Trading-Bot.git"
BRANCH="master"
INSTALL_DIR="/opt/hermes-trading-bot"
HERMES_USER="hermes"

echo "==> 1. Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv git curl

echo "==> 2. Creating '$HERMES_USER' user (no login)..."
sudo id -u $HERMES_USER &>/dev/null || sudo useradd --system --create-home --shell /usr/sbin/nologin $HERMES_USER

echo "==> 3. Cloning repo to $INSTALL_DIR..."
sudo rm -rf "$INSTALL_DIR"
sudo git clone --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
sudo chown -R "$HERMES_USER":"$HERMES_USER" "$INSTALL_DIR"

echo "==> 4. Creating Python virtual environment..."
sudo -u "$HERMES_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$HERMES_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet httpx websockets

echo "==> 5. Creating systemd service..."
sudo tee /etc/systemd/system/hermes-bot.service > /dev/null <<'SERVICE'
[Unit]
Description=Hermes v2 — Hyperliquid Perp Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hermes
Group=hermes
WorkingDirectory=/opt/hermes-trading-bot
ExecStart=/opt/hermes-trading-bot/.venv/bin/python -m src.main
Restart=on-failure
RestartSec=10
# Logs go to journald by default. View with: journalctl -u hermes-bot -f

# Environment (set your Hyperliquid keys when ready):
# Environment=HERMES_HYPERLIQUID__WALLET=0x...
# Environment=HERMES_HYPERLIQUID__PRIVATE_KEY=...

[Install]
WantedBy=multi-user.target
SERVICE

echo "==> 6. Creating auto-update timer (checks every 5 min)..."
sudo mkdir -p /opt/hermes-trading-bot/scripts
sudo tee /opt/hermes-trading-bot/scripts/auto-update.sh > /dev/null <<'UPDATE'
#!/usr/bin/env bash
export GIT_SSH_COMMAND="ssh -i /home/hermes/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new"
cd /opt/hermes-trading-bot
sudo -u hermes git fetch origin master &>/dev/null
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)
if [ "$LOCAL" != "$REMOTE" ]; then
    echo "[$(date)] New commit detected: $REMOTE. Updating and restarting..."
    sudo -u hermes git pull origin master --ff-only
    sudo -u hermes /opt/hermes-trading-bot/.venv/bin/pip install --quiet httpx websockets
    echo "[$(date)] Update complete. Restarting service..."
    sudo systemctl restart hermes-bot
fi
UPDATE
sudo chmod +x /opt/hermes-trading-bot/scripts/auto-update.sh

sudo tee /etc/systemd/system/hermes-auto-update.service > /dev/null <<'USVC'
[Unit]
Description=Hermes auto-update checker
[Service]
Type=oneshot
ExecStart=/opt/hermes-trading-bot/scripts/auto-update.sh
User=hermes
USVC

sudo tee /etc/systemd/system/hermes-auto-update.timer > /dev/null <<'UTIMER'
[Unit]
Description=Check for Hermes updates every 5 minutes
[Timer]
OnBootSec=30
OnUnitActiveSec=300
Persistent=true
[Install]
WantedBy=timers.target
UTIMER

echo "==> 6b. Creating audit timer (checks trading invariants every 15 min)..."
sudo tee /etc/systemd/system/hermes-audit.service > /dev/null <<'AUDITSVC'
[Unit]
Description=Hermes invariant/API/dashboard audit
[Service]
Type=oneshot
User=hermes
Group=hermes
WorkingDirectory=/opt/hermes-trading-bot
ExecStart=/opt/hermes-trading-bot/.venv/bin/python scripts/audit.py --db /opt/hermes-trading-bot/data/hermes.db --dashboard http://127.0.0.1:8081
AUDITSVC

sudo tee /etc/systemd/system/hermes-audit.timer > /dev/null <<'AUDITTIMER'
[Unit]
Description=Run Hermes audit every 15 minutes
[Timer]
OnBootSec=120
OnUnitActiveSec=900
Persistent=true
[Install]
WantedBy=timers.target
AUDITTIMER

echo "==> 7. Creating data directory..."
sudo -u "$HERMES_USER" mkdir -p "$INSTALL_DIR/data"

echo "==> 8. Enabling and starting services..."
sudo systemctl daemon-reload
sudo systemctl enable hermes-bot
sudo systemctl enable hermes-auto-update.timer
sudo systemctl enable hermes-audit.timer
sudo systemctl start hermes-bot
sudo systemctl start hermes-auto-update.timer
sudo systemctl start hermes-audit.timer

echo ""
echo "=============================================="
echo "  Hermes v2 deployed!"
echo "  Status: sudo systemctl status hermes-bot"
echo "  Logs:   sudo journalctl -u hermes-bot -f"
echo "  Stop:   sudo systemctl stop hermes-bot"
echo "  Start:  sudo systemctl start hermes-bot"
echo ""
echo "  To set Hyperliquid keys later:"
echo "    sudo systemctl edit hermes-bot"
echo "    (add Environment= lines, then restart)"
echo "=============================================="
