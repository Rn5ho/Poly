#!/bin/bash
# Poly Bot — Hetzner deployment script
#
# Usage:
#   scp -r . root@YOUR_SERVER:/opt/poly
#   ssh root@YOUR_SERVER 'bash /opt/poly/deploy/setup.sh'
#
# What this does:
#   1. Creates 'poly' user and directories
#   2. Sets up Python venv and installs deps
#   3. Creates .env template for secrets
#   4. Installs systemd services (bot + dashboard)
#   5. Starts services
#
# After running, configure:
#   nano /opt/poly/.env   # add your keys
#   systemctl restart poly-bot

set -euo pipefail

echo "=== Poly Bot Setup ==="

# 1. System deps
echo "[1/6] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git > /dev/null

# 2. User and directories
echo "[2/6] Creating user and directories..."
id -u poly &>/dev/null || useradd -r -m -s /bin/bash poly
mkdir -p /opt/poly /var/log/poly
chown -R poly:poly /opt/poly /var/log/poly

# 3. Copy code (if running from repo)
if [ -f "$(dirname "$0")/../poly/__init__.py" ]; then
    echo "  Copying code to /opt/poly..."
    cp -r "$(dirname "$0")/../poly" /opt/poly/
    cp "$(dirname "$0")/../requirements.txt" /opt/poly/
    chown -R poly:poly /opt/poly
fi

# 4. Python venv
echo "[3/6] Setting up Python environment..."
su - poly -c '
    cd /opt/poly
    python3 -m venv venv
    venv/bin/pip install --upgrade pip -q
    venv/bin/pip install -r requirements.txt -q
    venv/bin/pip install flask -q
'

# 5. Environment file
echo "[4/6] Creating .env template..."
if [ ! -f /opt/poly/.env ]; then
    cat > /opt/poly/.env << 'ENVEOF'
# Telegram notifications (get from @BotFather)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Polymarket live trading (only needed for --live mode)
# POLY_PRIVATE_KEY=0x...
# POLY_FUNDER=0x...
ENVEOF
    chown poly:poly /opt/poly/.env
    chmod 600 /opt/poly/.env
    echo "  Created /opt/poly/.env — edit with your keys!"
else
    echo "  .env already exists, skipping"
fi

# 6. Systemd services
echo "[5/6] Installing systemd services..."
cp "$(dirname "$0")/poly-bot.service" /etc/systemd/system/
cp "$(dirname "$0")/poly-dashboard.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable poly-bot poly-dashboard

# 7. Start
echo "[6/6] Starting services..."
systemctl start poly-dashboard
echo "  Dashboard started on :8080"
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit secrets:     nano /opt/poly/.env"
echo "  2. Start bot:        systemctl start poly-bot"
echo "  3. View logs:        journalctl -u poly-bot -f"
echo "  4. Dashboard:        http://YOUR_SERVER:8080"
echo "  5. Bot status:       cd /opt/poly && venv/bin/python -m poly.main status"
echo ""
echo "Commands:"
echo "  systemctl status poly-bot        # bot status"
echo "  systemctl status poly-dashboard  # dashboard status"
echo "  systemctl restart poly-bot       # restart bot"
echo "  systemctl stop poly-bot          # stop bot"
echo "  tail -f /var/log/poly/bot.log    # live logs"
