"""
Paper MM runner — BTC Up/Down Market Maker, paper mode with real fill simulation.

Runs A-S market making against real Polymarket orderbooks (no real money).
Simulates fills when our quotes cross the real market's opposing side.

Reports to Telegram every REPORT_INTERVAL_SEC seconds.
Logs everything to Drive.

Usage:
  python -m bots.polymarket.run_paper_mm
  python -m bots.polymarket.run_paper_mm --gamma 0.3 --size 5
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .fair_price import bootstrap_from_klines, get_btc_state
from .paper_mm import PaperMMEngine
from .scanner import discover_markets, get_orderbook
from .strategy_btc_mm import (
    BTCUpDownMM,
    is_updown_market,
    parse_window_from_question,
)

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN", "")
DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros")

SCAN_INTERVAL_SEC = float(os.environ.get("SCAN_INTERVAL_SEC", "30"))
REPORT_INTERVAL_SEC = float(os.environ.get("REPORT_INTERVAL_SEC", "300"))  # 5min default
MAX_MARKETS = 5
LOG_PATH = os.path.join(DRIVE_ROOT, "logs", "paper_mm_runner.log")


def _load_owner_chat_id() -> str:
    v = os.environ.get("OWNER_CHAT_ID", "")
    if v:
        return v
    state_path = os.path.join(DRIVE_ROOT, "state", "state.json")
    try:
        with open(state_path) as f:
            return str(json.load(f).get("owner_chat_id", ""))
    except Exception:
        return ""


OWNER_CHAT_ID = _load_owner_chat_id()


# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        fh = logging.FileHandler(LOG_PATH, "a", "utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:
        logger.warning(f"Could not open log file: {e}")

    for lib in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ── Telegram ───────────────────────────────────────────────────────────────────

def _esc(v) -> str:
    return html.escape(str(v))


async def send_telegram(client: httpx.AsyncClient, msg: str) -> None:
    if not BOT_TOKEN or not OWNER_CHAT_ID:
        logger.warning("Telegram not configured — skipping send")
        return
    try:
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": OWNER_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.warning(f"Telegram error: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")


# ── Market selection (same logic as run_mm.py) ─────────────────────────────────

def select_markets(all_markets, max_markets: int = MAX_MARKETS):
    now = time.time()
    tradeable = []
    for m in all_markets:
        if not is_updown_market(m.question):
            continue
        window = parse_window_from_question(m.question)
        if not window:
            continue
        _, end_ts = window
        if end_ts - now < 60:
            continue
        tradeable.append((m, end_ts))
    tradeable.sort(key=lambda x: x[1])
    return [m for m, _ in tradeable[:max_markets]]


# ── Per-market MM task ─────────────────────────────────────────────────────────

async def run_market_mm(
    market,
    engine: PaperMMEngine,
    *,
    gamma: float,
    order_size_usdc: float,
    requote_interval: float,
    http_client: httpx.AsyncClient,
) -> None:
    """
    Run paper MM for a single market until it expires.
    Fetches real orderbook each cycle for fill simulation.
    """
    window = parse_window_from_question(market.question)
    if not window:
        logger.warning(f"Cannot parse window: {market.question}")
        return

    start_ts, end_ts = window
    mm = BTCUpDownMM(
        market=market,
        clob_client=None,
        gamma=gamma,
        order_size_usdc=order_size_usdc,
        requote_interval=requote_interval,
        paper=True,
    )

    logger.info(
        f"Starting paper MM: {market.question[:60]}\n"
        f"  window={end_ts - start_ts:.0f}s | "
        f"remaining={max(0, end_ts - time.time()):.0f}s"
    )

    while time.time() < end_ts - 5:
        try:
            # 1. Compute A-S quotes
            bid, ask = mm.compute_quotes()
            p_fair = mm.last_p_fair

            # 2. Fetch real YES orderbook for fill simulation
            ob_yes = await get_orderbook(http_client, market.yes_token_id)

            # 3. Simulate fills
            fills = engine.process_quote(market, bid, ask, p_fair, ob_yes)

            # 4. Update MM inventory from fills (so A-S adjusts next cycle)
            for fill in fills:
                if fill.direction == "BUY":
                    mm.inventory.update_buy("YES", fill.qty, fill.price)
                else:
                    mm.inventory.update_sell("YES", fill.qty, fill.price)
                mm.fill_count += 1

            mm.quote_count += 1

        except Exception as e:
            logger.warning(f"Quote cycle error for {market.question[:40]}: {e}")

        # Sleep until next requote
        remaining = max(0, end_ts - time.time())
        sleep = min(requote_interval, remaining)
        if sleep <= 0:
            break
        await asyncio.sleep(sleep)

    # Market expired — resolve using last p_fair as proxy
    # p_fair > 0.5 = model thinks UP is more likely (use as paper outcome)
    state = engine.states.get(market.id)
    if state and not state.resolved:
        last_p = mm.last_p_fair
        # Fetch final price to decide outcome
        from .fair_price import fetch_btc_price
        fetch_btc_price()
        btc_state = get_btc_state()
        # Use momentum: positive recent return = UP
        recent_ret = btc_state.momentum_return(lookback_seconds=300)
        outcome = "UP" if recent_ret >= 0 else "DOWN"
        pnl = engine.resolve_market(market.id, outcome)
        logger.info(
            f"Market expired: {market.question[:55]}\n"
            f"  outcome={outcome} (recent_ret={recent_ret:+.4f}) | pnl={pnl:+.4f}"
        )


# ── Report builders ────────────────────────────────────────────────────────────

def build_status_report(engine: PaperMMEngine, elapsed_min: int) -> str:
    states = list(engine.states.values())
    if not states:
        return f"📭 No markets traded yet (running {elapsed_min}m)"

    total_pnl = engine.get_total_pnl()
    total_quotes = sum(s.quote_count for s in states)
    total_fills = sum(len(s.fills) for s in states)
    fill_rate = total_fills / total_quotes * 100 if total_quotes else 0

    open_states = [s for s in states if not s.resolved]
    resolved_states = [s for s in states if s.resolved]

    sign = "+" if total_pnl >= 0 else ""
    lines = [
        f"🤖 <b>Paper MM — {elapsed_min}m report</b>",
        "",
        f"📈 Total P&amp;L: <b>{sign}${total_pnl:.4f}</b>",
        f"🔄 Quotes: {total_quotes} | Fills: {total_fills} ({fill_rate:.1f}%)",
        f"📊 Markets: {len(resolved_states)} resolved, {len(open_states)} open",
    ]

    if resolved_states:
        lines.append("")
        lines.append("<b>Resolved markets:</b>")
        for s in resolved_states[-5:]:
            m_pnl = s.realized_pnl + (s.resolution_pnl or 0.0)
            sign_m = "+" if m_pnl >= 0 else ""
            lines.append(
                f"  [{_esc(s.outcome)}] {_esc(s.market_question[:40])} "
                f"→ {sign_m}${m_pnl:.4f}"
            )

    if open_states:
        lines.append("")
        lines.append("<b>Open positions:</b>")
        for s in open_states[:3]:
            lines.append(
                f"  [OPEN] {_esc(s.market_question[:45])}\n"
                f"    q={s.quote_count} fills={len(s.fills)} "
                f"realized={s.realized_pnl:+.4f}"
            )

    return "\n".join(lines)


def build_final_report(engine: PaperMMEngine, duration_min: float) -> str:
    states = list(engine.states.values())
    total_pnl = engine.get_total_pnl()
    total_quotes = sum(s.quote_count for s in states)
    total_fills = sum(len(s.fills) for s in states)
    fill_rate = total_fills / total_quotes * 100 if total_quotes else 0
    resolved = [s for s in states if s.resolved]

    sign = "+" if total_pnl >= 0 else ""
    return "\n".join([
        f"🛑 <b>Paper MM stopped</b> (ran {duration_min:.0f}m)",
        "",
        f"📈 Total P&amp;L: <b>{sign}${total_pnl:.4f}</b>",
        f"📊 Markets: {len(states)} ({len(resolved)} resolved)",
        f"🔄 Quotes: {total_quotes} | Fills: {total_fills} ({fill_rate:.1f}%)",
        f"💰 Realized: ${engine.total_realized_pnl:+.4f} | Resolution: ${engine.total_resolution_pnl:+.4f}",
    ])


# ── Main loop ──────────────────────────────────────────────────────────────────

async def main(
    gamma: float = 0.5,
    order_size_usdc: float = 10.0,
    requote_interval: float = 15.0,
) -> None:
    setup_logging()

    engine = PaperMMEngine(order_size_usdc=order_size_usdc)
    start_time = time.monotonic()
    last_report = time.monotonic()
    stop_event = asyncio.Event()
    active_tasks: dict[str, asyncio.Task] = {}

    def _handle_sigterm(*_):
        logger.info("SIGTERM — stopping...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    print(f"\n🤖 Paper MM Started")
    print(f"   γ={gamma} | size=${order_size_usdc:.0f} | requote={requote_interval}s")
    print(f"   report every {REPORT_INTERVAL_SEC:.0f}s\n")

    # Bootstrap Binance data
    state = get_btc_state()
    if len(state.prices) < 3:
        print("Bootstrapping Binance price data...")
        bootstrap_from_klines()

    async with httpx.AsyncClient() as client:
        await send_telegram(
            client,
            f"▶️ <b>Paper MM started</b>\n"
            f"γ={gamma} | size=${order_size_usdc:.0f} | requote={requote_interval:.0f}s\n"
            f"Reports every {int(REPORT_INTERVAL_SEC // 60)}m",
        )

        try:
            while not stop_event.is_set():
                # Clean up finished tasks
                for mid, task in list(active_tasks.items()):
                    if task.done():
                        del active_tasks[mid]
                        if task.exception():
                            logger.error(f"MM task error: {task.exception()}")

                # Discover + launch new markets
                try:
                    all_markets = await discover_markets(client)
                    selected = select_markets(all_markets)

                    for market in selected:
                        if market.id not in active_tasks and market.id not in engine.states:
                            logger.info(f"Launching MM for: {market.question[:60]}")
                            task = asyncio.create_task(
                                run_market_mm(
                                    market,
                                    engine,
                                    gamma=gamma,
                                    order_size_usdc=order_size_usdc,
                                    requote_interval=requote_interval,
                                    http_client=client,
                                )
                            )
                            active_tasks[market.id] = task

                    if not selected:
                        logger.info("No tradeable markets right now. Retrying in 30s.")

                except Exception as e:
                    logger.error(f"Discovery error: {e}")

                # Periodic report
                now = time.monotonic()
                if now - last_report >= REPORT_INTERVAL_SEC:
                    elapsed_min = int((now - start_time) / 60)
                    msg = build_status_report(engine, elapsed_min)
                    await send_telegram(client, msg)
                    last_report = time.monotonic()

                # Sleep (interruptible)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=SCAN_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    pass

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — stopping...")
        finally:
            # Cancel active tasks
            for task in active_tasks.values():
                task.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks.values(), return_exceptions=True)

            # Final report
            duration_min = (time.monotonic() - start_time) / 60
            final = build_final_report(engine, duration_min)
            await send_telegram(client, final)
            engine.print_summary()
            logger.info("Paper MM stopped.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC Up/Down Paper MM")
    p.add_argument("--gamma", type=float, default=0.5)
    p.add_argument("--size", type=float, default=10.0, dest="order_size_usdc")
    p.add_argument("--requote", type=float, default=15.0, dest="requote_interval")
    return p.parse_args()


def main_cli() -> None:
    args = parse_args()
    asyncio.run(main(
        gamma=args.gamma,
        order_size_usdc=args.order_size_usdc,
        requote_interval=args.requote_interval,
    ))


if __name__ == "__main__":
    main_cli()
