#!/usr/bin/env bash
# =============================================================================
# server_setup.sh — One-time provisioning for a fresh Linux server
# Usage: bash scripts/server_setup.sh
# Works on: Ubuntu 20.04+, Debian 11+
# =============================================================================

set -euo pipefail

BOT_DIR="/opt/polymarket_bot"
REPO_URL="https://github.com/Disaer38/ouroboros.git"
BRANCH="main"
PYTHON_MIN="3.11"
SERVICE_NAME="polymarket_bot"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root: sudo bash $0"
  fi
}

# -----------------------------------------------------------------------------
# 1. System packages
# -----------------------------------------------------------------------------
install_system_deps() {
  info "[1/7] Updating system packages..."
  apt-get update -qq
  apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    git curl wget unzip \
    build-essential libssl-dev libffi-dev \
    supervisor
  success "System packages installed."
}

# -----------------------------------------------------------------------------
# 2. Python version check
# -----------------------------------------------------------------------------
check_python() {
  info "[2/7] Checking Python version..."
  PYTHON_BIN=$(command -v python3 || true)
  if [[ -z "$PYTHON_BIN" ]]; then
    error "python3 not found after install — check apt output."
  fi

  PY_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  info "Found Python $PY_VER at $PYTHON_BIN"

  # If system python3 < 3.11, install from deadsnakes PPA (Ubuntu only)
  if [[ "$(printf '%s\n' "$PYTHON_MIN" "$PY_VER" | sort -V | head -1)" != "$PYTHON_MIN" ]]; then
    warn "Python $PY_VER < $PYTHON_MIN — installing from deadsnakes PPA..."
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y python3.11 python3.11-venv python3.11-dev
    PYTHON_BIN=$(command -v python3.11)
  fi
  success "Using Python at: $PYTHON_BIN"
}

# -----------------------------------------------------------------------------
# 3. Clone / update repo
# -----------------------------------------------------------------------------
setup_repo() {
  info "[3/7] Setting up repository at $BOT_DIR..."
  if [[ -d "$BOT_DIR/.git" ]]; then
    info "Repository already exists — pulling latest $BRANCH..."
    git -C "$BOT_DIR" fetch origin
    git -C "$BOT_DIR" reset --hard "origin/$BRANCH"
    success "Repository updated."
  else
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$BOT_DIR"
    success "Repository cloned."
  fi
}

# -----------------------------------------------------------------------------
# 4. Virtual environment + dependencies
# -----------------------------------------------------------------------------
setup_venv() {
  info "[4/7] Creating virtual environment..."
  "$PYTHON_BIN" -m venv "$BOT_DIR/.venv"
  "$BOT_DIR/.venv/bin/pip" install --upgrade pip wheel -q
  "$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q
  success "Python dependencies installed."
}

# -----------------------------------------------------------------------------
# 5. Create .env file (only if it doesn't exist)
# -----------------------------------------------------------------------------
setup_env() {
  info "[5/7] Creating .env file..."
  ENV_FILE="$BOT_DIR/.env"

  if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists at $ENV_FILE — not overwriting."
    return
  fi

  cat > "$ENV_FILE" <<'EOF'
# Polymarket Credentials
# Fill in your real API credentials before starting the bot
POLYMARKET_PRIVATE_KEY=FILL_IN_PRIVATE_KEY
POLYMARKET_WALLET_ADDRESS=FILL_IN_WALLET_ADDRESS

# Proxy (HTTP)
PROXY_URL=http://user86515:xqysl1@193.243.191.182:4650

# Trading parameters
DRY_RUN=true
MAX_ORDER_SIZE_USDC=10
SLIPPAGE_TOLERANCE=0.02
EOF

  chmod 600 "$ENV_FILE"
  success ".env created at $ENV_FILE"
  warn "IMPORTANT: Edit $ENV_FILE and fill in your real credentials before starting!"
}

# -----------------------------------------------------------------------------
# 6. Create logs directory
# -----------------------------------------------------------------------------
setup_dirs() {
  info "[6/7] Creating required directories..."
  mkdir -p "$BOT_DIR/logs"
  chown -R "$SUDO_USER:$SUDO_USER" "$BOT_DIR" 2>/dev/null || true
  success "Directories ready."
}

# -----------------------------------------------------------------------------
# 7. Create systemd service (auto-start on reboot)
# -----------------------------------------------------------------------------
setup_service() {
  info "[7/7] Registering systemd service: $SERVICE_NAME..."

  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Polymarket Arbitrage Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SUDO_USER:-root}
WorkingDirectory=${BOT_DIR}
EnvironmentFile=${BOT_DIR}/.env
ExecStart=${BOT_DIR}/.venv/bin/python polymarket_arb/main.py
Restart=on-failure
RestartSec=10
StandardOutput=append:${BOT_DIR}/logs/bot.log
StandardError=append:${BOT_DIR}/logs/bot_err.log

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  success "Service registered. Start with: systemctl start $SERVICE_NAME"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
  echo ""
  echo -e "${CYAN}=====================================================${NC}"
  echo -e "${CYAN}     Polymarket Bot — Linux Server Setup             ${NC}"
  echo -e "${CYAN}=====================================================${NC}"
  echo ""

  require_root
  install_system_deps
  check_python
  setup_repo
  setup_venv
  setup_env
  setup_dirs
  setup_service

  echo ""
  echo -e "${GREEN}=====================================================${NC}"
  echo -e "${GREEN} Setup complete!${NC}"
  echo -e "${GREEN}=====================================================${NC}"
  echo ""
  echo "  Bot directory : $BOT_DIR"
  echo "  Logs          : $BOT_DIR/logs/"
  echo "  Service       : $SERVICE_NAME"
  echo ""
  echo "  Next steps:"
  echo "    1. Edit $BOT_DIR/.env  — add real API credentials"
  echo "    2. sudo systemctl start $SERVICE_NAME"
  echo "    3. sudo journalctl -fu $SERVICE_NAME   — follow logs"
  echo ""
}

main "$@"
