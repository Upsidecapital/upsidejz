#!/bin/bash
# ============================================================
#  UPSIDE - POLYTRACKER
#  Double-click this file to install and run on macOS/Linux.
# ============================================================

set -e
cd "$(dirname "$0")"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  UPSIDE - POLYTRACKER${NC}"
echo -e "${CYAN}  Polymarket Latency Arbitrage Bot${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""

# --- Detect Python ---
PYTHON=""
for candidate in python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${RED}[ERROR] Python 3.11 or higher is required.${NC}"
    echo ""
    echo "  macOS:  brew install python@3.11"
    echo "  Linux:  sudo apt install python3.11 python3.11-venv"
    echo ""
    read -p "Press Enter to exit..."
    exit 1
fi

echo -e "${GREEN}[OK]${NC} Python found: $($PYTHON --version)"
echo ""

# --- Create venv ---
if [ ! -d ".venv" ]; then
    echo "  [1/3] First-time setup: creating virtual environment..."
    $PYTHON -m venv .venv
    echo -e "${GREEN}[OK]${NC} Virtual environment created."
    echo ""
fi

# --- Activate ---
# shellcheck disable=SC1091
source .venv/bin/activate

# --- Install deps once ---
if [ ! -f ".venv/.deps_installed" ]; then
    echo "  [2/3] Installing dependencies (one-time, ~2 min)..."
    python -m pip install --upgrade pip >/dev/null 2>&1
    python -m pip install -r polytracker/requirements.txt
    touch .venv/.deps_installed
    echo -e "${GREEN}[OK]${NC} Dependencies installed."
    echo ""
else
    echo -e "${GREEN}[OK]${NC} Dependencies already installed."
    echo ""
fi

# --- Create .env from template ---
if [ ! -f ".env" ] && [ -f "polytracker/.env.example" ]; then
    cp polytracker/.env.example .env
    echo "  [INFO] Created .env file. Edit it to add API keys."
    echo ""
fi

# --- Launch ---
echo "  [3/3] Launching Polytracker Dashboard..."
echo ""
echo "  Close the dashboard window or press Ctrl+C to stop the bot."
echo -e "${CYAN}============================================================${NC}"
echo ""

python launcher.py "$@"

echo ""
echo "Polytracker has stopped."
read -p "Press Enter to exit..."
