"""
Entry point for the Dominant Side paper trading bot.

Usage:
    python -m bots.polymarket.run_dominant
    python bots/polymarket/run_dominant.py

Environment variables:
    TELEGRAM_BOT_TOKEN   — Telegram bot token (optional, for notifications)
    OWNER_CHAT_ID        — Telegram chat ID (optional)
    DOMINANT_BET_SIZE    — Bet size in USDC (default: 10.0)
    DOMINANT_THRESHOLD   — Min probability threshold (default: 0.60)
    DOMINANT_ASSETS      — Comma-separated assets (default: btc,eth)
    DRIVE_ROOT           — Google Drive root path
"""
import asyncio
import json
import logging
import os
import signal
import sys

from .dominant.config import DominantConfig
from .dominant.engine import TradingEngine
from .dominant.exchange import PaperExchange
from .dominant.reporter import Reporter

# ── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    # Fix Windows cp1252 console encoding for emoji/unicode
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    # Suppress encoding errors on Windows cp1252 consoles
    ch.handleError = lambda record: None
    root.addHandler(ch)
    for lib in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ── Config from env ───────────────────────────────────────────────────────────

def _load_config() -> DominantConfig:
    bet_size = float(os.environ.get("DOMINANT_BET_SIZE", "10.0"))
    threshold = float(os.environ.get("DOMINANT_THRESHOLD", "0.60"))
    assets_str = os.environ.get("DOMINANT_ASSETS", "btc,eth")
    assets = [a.strip().lower() for a in assets_str.split(",") if a.strip()]

    config = DominantConfig(
        min_dominant_prob=threshold,
        bet_size_usdc=bet_size,
        assets=assets,
    )
    config.validate()
    return config


def _load_telegram() -> tuple[str, str]:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN", "")
    chat_id = os.environ.get("OWNER_CHAT_ID", "")

    if not chat_id:
        # Try state.json
        drive_root = os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros")
        state_path = os.path.join(drive_root, "state", "state.json")
        try:
            with open(state_path) as f:
                data = json.load(f)
            chat_id = str(data.get("owner_chat_id", ""))
        except Exception:
            pass

    return bot_token, chat_id


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    _setup_logging()
    logger = logging.getLogger(__name__)

    config = _load_config()
    bot_token, chat_id = _load_telegram()

    logger.info(
        f"Starting Dominant Side Paper Trader\n"
        f"  assets={config.assets}\n"
        f"  threshold={config.min_dominant_prob}\n"
        f"  bet=${config.bet_size_usdc}\n"
        f"  max_open={config.max_open_trades}\n"
        f"  log={config.log_path}"
    )

    exchange = PaperExchange(config)
    reporter = Reporter(config, bot_token, chat_id)
    stop_event = asyncio.Event()
    engine = TradingEngine(config, exchange, stop_event)

    def _handle_stop(*_):
        logger.info("Stop signal received.")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    await reporter.send_startup(exchange)

    try:
        await engine.run()
    finally:
        await reporter.send_shutdown(exchange)
        logger.info(f"Final status: {engine.status()}")


if __name__ == "__main__":
    asyncio.run(main())