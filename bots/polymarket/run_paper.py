"""
Paper trading runner with Telegram reporting.

Runs the neg-risk paper trading loop and sends periodic status reports to
Telegram. All output is also written to a log file on Google Drive.

Configuration (env vars):
  BOT_TOKEN            Telegram bot token
  OWNER_CHAT_ID        Telegram chat ID to send reports to
  SCAN_INTERVAL_SEC    Seconds between market scans (default: 30)
  REPORT_INTERVAL_SEC  Seconds between Telegram reports (default: 600)
  DRIVE_ROOT           Google Drive root (default: /content/drive/MyDrive/Ouroboros)

Usage:
  python -m bots.polymarket.run_paper
  python bots/polymarket/run_paper.py
"""
import asyncio
import html
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .paper_trader import PaperTrader
from .scanner import _is_intraday, discover_markets, scan_all

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN", "")


def _load_owner_chat_id() -> str:
    # First try env
    v = os.environ.get("OWNER_CHAT_ID", "")
    if v:
        return v
    # Then try state.json
    import json
    drive_root = os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros")
    state_path = os.path.join(drive_root, "state", "state.json")
    try:
        with open(state_path) as f:
            data = json.load(f)
        return str(data.get("owner_chat_id", ""))
    except Exception:
        return ""

OWNER_CHAT_ID = _load_owner_chat_id()
SCAN_INTERVAL_SEC = float(os.environ.get("SCAN_INTERVAL_SEC", "30"))
REPORT_INTERVAL_SEC = float(os.environ.get("REPORT_INTERVAL_SEC", "600"))
DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros")

MAX_POSITION = 10.0
MAX_EXPOSURE = 100.0
PROFIT_THRESHOLD = 0.97
MIN_DEPTH = 2.0

LOG_PATH = os.path.join(DRIVE_ROOT, "logs", "paper_trader.log")


# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_logging() -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
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
    root.addHandler(ch)

    try:
        fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        logger.info(f"Logging to {LOG_PATH}")
    except Exception as e:
        logger.warning(f"Could not open log file {LOG_PATH}: {e}")

    for lib in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ── Telegram helpers ───────────────────────────────────────────────────────────

def _esc(text: object) -> str:
    """Escape text for Telegram HTML parse_mode."""
    return html.escape(str(text))


async def send_telegram(client: httpx.AsyncClient, message: str) -> None:
    """Send a message to Telegram. Logs errors but never raises."""
    if not BOT_TOKEN or not OWNER_CHAT_ID:
        logger.warning("Telegram not configured (BOT_TOKEN / OWNER_CHAT_ID missing) — skipping send")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": OWNER_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = await client.post(url, json=payload, timeout=15.0)
        if resp.status_code != 200:
            logger.warning(
                f"Telegram send failed: HTTP {resp.status_code} — {resp.text[:200]}"
            )
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")


# ── Period statistics ──────────────────────────────────────────────────────────

@dataclass
class PeriodStats:
    """Accumulates statistics for a single reporting period."""
    scans: int = 0
    markets_scanned: int = 0
    opps_found: int = 0
    trades_entered: int = 0
    recent_trades: list = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

    def reset(self) -> None:
        self.scans = 0
        self.markets_scanned = 0
        self.opps_found = 0
        self.trades_entered = 0
        self.recent_trades = []
        self.started_at = time.monotonic()


# ── Report builders ────────────────────────────────────────────────────────────

def _build_period_report(
    period: PeriodStats,
    trader: PaperTrader,
    report_minutes: int,
) -> str:
    if period.trades_entered == 0:
        return (
            f"📭 No opportunities in last {report_minutes}m. "
            f"Total scanned: {_esc(period.markets_scanned)} markets."
        )

    open_exposure = trader.open_exposure
    open_trades = [t for t in trader.trades if not t.resolved]
    resolved_trades = [t for t in trader.trades if t.resolved]

    exp_profit = sum(t.expected_profit for t in open_trades)
    exp_pct = (exp_profit / open_exposure * 100) if open_exposure > 0 else 0.0
    realized_pnl = trader.realized_pnl

    def _signed(v: float, fmt: str = ".2f") -> str:
        return ("+" if v >= 0 else "") + format(v, fmt)

    lines = [
        f"📊 <b>Paper Trader — {report_minutes}m report</b>",
        "",
        f"🔍 Scans: {period.scans} | Opportunities found: {period.opps_found}",
        f"📝 Trades entered: {period.trades_entered}",
        f"💰 Open exposure: ${open_exposure:.2f}",
        f"📈 Expected P&amp;L: {_signed(exp_profit)} ({_signed(exp_pct, '.1f')}%)",
        f"✅ Resolved: {len(resolved_trades)} | Realized P&amp;L: {_signed(realized_pnl)}",
    ]

    if period.recent_trades:
        lines.append("")
        lines.append("Recent trades:")
        for t in period.recent_trades[-5:]:
            sign = "+" if t.expected_pnl_pct >= 0 else ""
            lines.append(
                f"• {_esc(t.market_question)} — "
                f"YES={t.yes_ask:.2f} NO={t.no_ask:.2f} → "
                f"exp {sign}{t.expected_pnl_pct:.1f}%"
            )

    return "\n".join(lines)


def _build_final_report(trader: PaperTrader) -> str:
    open_trades = [t for t in trader.trades if not t.resolved]
    resolved_trades = [t for t in trader.trades if t.resolved]
    exp_profit = sum(t.expected_profit for t in open_trades)
    realized = trader.realized_pnl
    sign_r = "+" if realized >= 0 else ""
    sign_e = "+" if exp_profit >= 0 else ""

    lines = [
        "🛑 <b>Paper Trader stopped</b>",
        "",
        f"📝 Total trades: {len(trader.trades)} "
        f"(open: {len(open_trades)}, resolved: {len(resolved_trades)})",
        f"💰 Total invested: ${trader.total_invested:.2f}",
        f"💼 Open exposure: ${trader.open_exposure:.2f}",
        f"📈 Unrealized expected: {sign_e}${exp_profit:.2f}",
        f"✅ Realized P&amp;L: {sign_r}${realized:.2f}",
    ]
    return "\n".join(lines)


# ── Main loop ──────────────────────────────────────────────────────────────────

async def main() -> None:
    setup_logging()

    report_minutes = max(1, int(REPORT_INTERVAL_SEC // 60))
    logger.info(
        f"Paper trader starting | scan={SCAN_INTERVAL_SEC:.0f}s "
        f"report={REPORT_INTERVAL_SEC:.0f}s ({report_minutes}m) | "
        f"threshold={PROFIT_THRESHOLD} max_pos=${MAX_POSITION:.0f} "
        f"max_exp=${MAX_EXPOSURE:.0f}"
    )

    trader = PaperTrader(
        max_position_usdc=MAX_POSITION,
        max_exposure_usdc=MAX_EXPOSURE,
    )
    period = PeriodStats()
    last_report_at = time.monotonic()

    stop_event = asyncio.Event()

    def _handle_sigterm(*_):
        logger.info("SIGTERM received — stopping...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    async with httpx.AsyncClient() as client:
        # ── Startup message ────────────────────────────────────────────────────
        await send_telegram(
            client,
            f"▶️ <b>Paper trader started</b>\n"
            f"Scan every {SCAN_INTERVAL_SEC:.0f}s | Report every {report_minutes}m\n"
            f"threshold={PROFIT_THRESHOLD} | "
            f"max_pos=${MAX_POSITION:.0f} | max_exp=${MAX_EXPOSURE:.0f}",
        )

        try:
            while not stop_event.is_set():
                scan_start = time.monotonic()
                period.scans += 1
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                logger.info(f"[{ts}] Scan #{period.scans} — discovering markets...")

                # 1. Discover markets
                try:
                    markets = await discover_markets(client)
                except Exception as e:
                    logger.error(f"Market discovery failed: {e}")
                    await _interruptible_sleep(stop_event, SCAN_INTERVAL_SEC)
                    continue

                if not markets:
                    logger.warning("No active markets found. BTC/ETH 5min markets run weekdays ~9AM-5PM ET.")
                    await _interruptible_sleep(stop_event, SCAN_INTERVAL_SEC)
                    continue

                intraday_count = sum(1 for m in markets if _is_intraday(m.question))
                logger.info(f"  Found {len(markets)} markets ({intraday_count} intraday)")
                period.markets_scanned += len(markets)

                # 2. Scan for opportunities
                try:
                    opportunities = await scan_all(
                        client,
                        markets,
                        profit_threshold=PROFIT_THRESHOLD,
                        min_depth_usdc=MIN_DEPTH,
                    )
                except Exception as e:
                    logger.error(f"Scan failed: {e}")
                    await _interruptible_sleep(stop_event, SCAN_INTERVAL_SEC)
                    continue

                period.opps_found += len(opportunities)

                if opportunities:
                    for opp in opportunities:
                        logger.info(
                            f"  💡 ARB: {opp.market.question[:55]}\n"
                            f"     YES={opp.yes_ask:.3f} + NO={opp.no_ask:.3f} = "
                            f"{opp.total_cost:.3f} → {opp.profit_pct:.1f}% gross"
                        )
                        trade = trader.enter_trade(opp)
                        if trade is not None:
                            period.trades_entered += 1
                            period.recent_trades.append(trade)
                else:
                    logger.info("  📭 No opportunities this scan.")

                # 3. Periodic report
                now = time.monotonic()
                if now - last_report_at >= REPORT_INTERVAL_SEC:
                    msg = _build_period_report(period, trader, report_minutes)
                    await send_telegram(client, msg)
                    period.reset()
                    last_report_at = time.monotonic()

                # 4. Sleep until next scan (interruptible by stop_event)
                elapsed = time.monotonic() - scan_start
                sleep_remaining = max(0.0, SCAN_INTERVAL_SEC - elapsed)
                await _interruptible_sleep(stop_event, sleep_remaining)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — stopping paper trader...")

        finally:
            logger.info("Sending final summary to Telegram...")
            final_msg = _build_final_report(trader)
            await send_telegram(client, final_msg)
            trader.print_summary()
            logger.info("Paper trader stopped.")


async def _interruptible_sleep(stop_event: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds`, but wake early if stop_event is set."""
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
