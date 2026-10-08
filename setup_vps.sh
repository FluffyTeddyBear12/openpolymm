#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "  Polymarket Bot London VPS Setup Script"
echo "=========================================================="

sudo apt update && sudo apt install -y python3 python3-pip python3-venv git tmux curl ufw

# Configure 2GB swapfile to prevent OOM errors on 512MB/1GB instances
if [ ! -f /swapfile ]; then
    echo "Configuring 2GB swapfile for memory stability..."
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
fi

echo "Activating virtual environment..."
source venv/bin/activate

echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "Running local test suite..."
python -m pytest test_liquidity_filter.py test_maker_taker_engine.py test_rollback_protector.py test_dashboard.py test_start_bot.py

echo "=========================================================="
echo "  Setup Verified! All core test suites passed."
echo "  Verify your .env file contains:"
echo "    POLYMARKET_PRIVATE_KEY"
echo "    POLYMARKET_ADDRESS"
echo "    POLYMARKET_SIGNATURE_TYPE"
echo ""
echo "  To start the bot in the background:"
echo "    tmux new -s polybot"
echo "    source venv/bin/activate"
echo "    python start_bot.py"
echo "=========================================================="
