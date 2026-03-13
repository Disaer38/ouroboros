"""
Live Spread Monitor for @vague-sourdough (0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d)

Polls data-api.polymarket.com every POLL_INTERVAL seconds.
Logs new trades to logs/sourdough_live.log and stdout.
Detects when both Up+Down of the same slug were bought recently
and prints the implied spread cost.

Usage:
    python -m bots.polymarket.monitor_sourdough
    # or
    python bots/polymarket/monitor_sourdough.py
"""

import os
import sys
import time
import logging
import requests
from datetime import datetime, timezone
from collections import defaultdict

# ── Config ──────────────────────────────────────────────────────────────────
WALLET       = os.environ.get("SOURDOUGH_WALLET",
               "0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "20"))   # seconds
SPREAD_WINDOW = int(os.environ.get("SPREAD_WINDOW", "300"))  # 5 min look-back
LOG_FILE     = os.environ.get("LOG_FILE",
               os.path.join(os.path.dirname(__file__),
                            "../../logs/sourdough_live.log"))
DATA_API     = "https://data-api.polymarket.com"

# ── Logging ──────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("monitor")


# ── API ───────────────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; sourdough-monitor/1.0)"
})


def fetch_trades(after_ts: int = 0, limit: int = 100) -> list[dict]:
    """Return list of trades newer than after_ts (unix seconds)."""
    url = f"{DATA_API}/trades"
    params = {
        "user": WALLET,
        "limit": limit,
    }
    try:
        resp = SESSION.get(url, params=params, timeout=15)
        resp.raise_for_status()
        trades = resp.json()
        if not isinstance(trades, list):
            log.warning(f"Unexpected response type: {type(trades)}")
            return []
        # Filter to only trades newer than watermark
        new = [t for t in trades if t.get("timestamp", 0) > after_ts]
        return new
    except requests.exceptions.HTTPError as e:
        log.warning(f"HTTP error fetching trades: {e}")
        return []
    except Exception as e:
        log.warning(f"Error fetching trades: {e}")
        return []


# ── Spread detection ─────────────────────────────────────────────────────────
# recent_trades: slug -> {outcome -> list of {price, size, ts}}
recent_trades: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))


def update_recent_state(trade: dict) -> None:
    """Add trade to recent_trades window."""
    slug    = trade.get("slug", "unknown")
    outcome = trade.get("outcome", "?")
    price   = float(trade.get("price", 0))
    size    = float(trade.get("size", 0))
    ts      = int(trade.get("timestamp", 0))
    recent_trades[slug][outcome].append({"price": price, "size": size, "ts": ts})


def prune_old_trades(now_ts: int) -> None:
    """Remove entries older than SPREAD_WINDOW seconds."""
    cutoff = now_ts - SPREAD_WINDOW
    for slug in list(recent_trades.keys()):
        for outcome in list(recent_trades[slug].keys()):
            recent_trades[slug][outcome] = [
                t for t in recent_trades[slug][outcome] if t["ts"] >= cutoff
            ]
        # Remove empty slug entries
        if not any(recent_trades[slug].values()):
            del recent_trades[slug]


def calc_spread(slug: str) -> str | None:
    """
    If both Up and Down sides exist for this slug,
    compute implied spread cost = avg_up_price + avg_down_price.
    Returns formatted string or None.
    """
    sides = recent_trades.get(slug, {})
    up_entries   = sides.get("Up", [])
    down_entries = sides.get("Down", [])
    if not up_entries or not down_entries:
        return None

    def wavg(entries):
        total_size = sum(e["size"] for e in entries)
        if total_size == 0:
            return 0.0
        return sum(e["price"] * e["size"] for e in entries) / total_size

    up_avg   = wavg(up_entries)
    down_avg = wavg(down_entries)
    spread   = up_avg + down_avg
    edge     = 1.0 - spread  # positive = favors buyer
    return (f"Spread cost={spread:.4f}  "
            f"(Up={up_avg:.4f} Down={down_avg:.4f})  "
            f"Edge={'▲'+f'{edge:.4f}' if edge >= 0 else '▼'+f'{abs(edge):.4f}'}")


# ── Processing ────────────────────────────────────────────────────────────────
def process_new_trades(trades: list[dict]) -> None:
    """Log each trade + compute spread if both sides seen."""
    now_ts = int(time.time())
    prune_old_trades(now_ts)

    # Sort oldest first so state builds up correctly
    for trade in sorted(trades, key=lambda t: t.get("timestamp", 0)):
        slug    = trade.get("slug", "unknown")
        outcome = trade.get("outcome", "?")
        price   = float(trade.get("price", 0))
        size    = float(trade.get("size", 0))
        ts      = int(trade.get("timestamp", 0))
        dt      = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
        title   = trade.get("title", slug)

        update_recent_state(trade)
        spread_info = calc_spread(slug)

        spread_str = f" | {spread_info}" if spread_info else ""
        log.info(
            f"[{dt}] [{slug}] BUY {outcome} @ {price:.4f} ({size:.2f})"
            f" | {title}{spread_str}"
        )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 70)
    log.info(f"Sourdough Live Monitor started — wallet: {WALLET}")
    log.info(f"Poll interval: {POLL_INTERVAL}s  |  Spread window: {SPREAD_WINDOW}s")
    log.info(f"Log file: {os.path.abspath(LOG_FILE)}")
    log.info("=" * 70)

    # Bootstrap watermark from the most recent trade timestamp
    seed = fetch_trades(limit=1)
    watermark = seed[0]["timestamp"] if seed else int(time.time())
    log.info(f"Watermark set to {watermark} — listening for new trades…")

    while True:
        try:
            new_trades = fetch_trades(after_ts=watermark, limit=100)
            if new_trades:
                max_ts = max(t.get("timestamp", 0) for t in new_trades)
                watermark = max(watermark, max_ts)
                log.info(f"── {len(new_trades)} new trade(s) ──────────────────────")
                process_new_trades(new_trades)
            else:
                ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log.info(f"[{ts_str}] No new trades (watermark={watermark})")
        except KeyboardInterrupt:
            log.info("Monitor stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
