#!/usr/bin/env bash
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Error: setup_vps.sh must be run as root or via sudo." >&2
    exit 1
fi

APP_DIR="${APP_DIR:-/home/ubuntu/polymarket_bot}"
APP_USER="${APP_USER:-ubuntu}"

echo "=== Polymarket US VPS Provisioning ==="
echo "App Directory: $APP_DIR"
echo "App User:      $APP_USER"

# 1. 2GB Swapfile Configuration
if [ ! -f /swapfile ]; then
    echo "[1/6] Allocating 2GB swapfile..."
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    if ! grep -q '/swapfile' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    echo "Swapfile successfully created and activated."
else
    echo "[1/6] Swapfile already exists, skipping."
fi

# 2. Package Installation & Basic Firewall
echo "[2/6] Installing required system packages..."
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3-venv \
    python3-pip \
    git \
    curl \
    ufw

echo "Configuring firewall..."
ufw allow OpenSSH 2>/dev/null || ufw allow 22/tcp
ufw --force enable

# 3. Python Virtual Environment Setup & Dependencies
echo "[3/6] Setting up virtual environment..."
mkdir -p "$APP_DIR"
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi

echo "Installing Python dependencies..."
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/pmus/requirements.txt"

# 4. /etc/pmus.env Environment File Template
echo "[4/6] Configuring /etc/pmus.env template..."
if [ ! -f /etc/pmus.env ]; then
    cat << 'EOF' > /etc/pmus.env
# Polymarket US Environment Configuration
PMUS_KEY_ID=""
PMUS_SECRET_KEY=""
PMUS_MODE="shadow"
PMUS_GATEWAY_URL="https://gateway.polymarket.us"
PMUS_TOTAL_BUDGET="60.00"
PMUS_MAX_PER_MARKET="25.00"
PMUS_MAX_COLLATERAL="70.00"
PMUS_MAX_COLLATERAL_PER_MARKET="30.00"
PMUS_MAX_MARKETS="4"
PMUS_MAX_CONTRACTS_PER_MARKET="6000"
PMUS_DAILY_LOSS_LIMIT="5.00"
PMUS_DAILY_LOSS_STOP="10.00"
PMUS_WEBHOOK_URL=""
EOF
    chmod 600 /etc/pmus.env
    chown "$APP_USER:$APP_USER" /etc/pmus.env
    echo "/etc/pmus.env created."
else
    echo "/etc/pmus.env already exists, preserving."
fi

# 5. Systemd Service Installation
echo "[5/6] Installing systemd service..."
if [ -f "$APP_DIR/deploy/pmus.service" ]; then
    cp "$APP_DIR/deploy/pmus.service" /etc/systemd/system/pmus.service
    systemctl daemon-reload
    systemctl enable pmus.service
    echo "pmus.service enabled."
else
    echo "Warning: $APP_DIR/deploy/pmus.service not found; skipping service install."
fi

# 6. Verification Test Suite Execution
echo "[6/6] Running pytest test suite..."
sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -m pytest "$APP_DIR/pmus/tests" -v

echo "=== VPS Provisioning Complete ==="
