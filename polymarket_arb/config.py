"""Configuration — constants and env vars."""
import os
from decimal import Decimal

# API Endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

# Strategy parameters
FEE_RATE         = Decimal("0.02")    # 2% taker fee per leg
MIN_EDGE         = Decimal("0.005")   # minimum net edge after fees
# Entry threshold: ask_up + ask_down must be BELOW this
ENTRY_THRESHOLD  = Decimal("1.0") - (FEE_RATE * 2) - MIN_EDGE   # = 0.955

# Market filter
TARGET_SLUGS = ["btc-updown-5m", "eth-updown-5m", "btc-updown-15m", "eth-updown-15m"]

# Risk defaults (Phase 2)
MAX_POSITION_PER_MARKET = Decimal("200")
MIN_USDC_RESERVE        = Decimal("50")
MAX_SHARES_PER_TRADE    = Decimal("500")

# Polling interval (seconds) — Phase 1 uses REST polling
POLL_INTERVAL_SEC = 1.0

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
