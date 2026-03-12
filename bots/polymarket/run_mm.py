"""
Market Maker runner — entry point for the BTC Up/Down MM strategy.

Discovers active "Bitcoin Up or Down" markets on Polymarket, then runs
BTCUpDownMM on each one concurrently using Avellaneda-Stoikov.

Usage:
  python -m bots.polymarket.run_mm               # paper mode
  python -m bots.polymarket.run_mm --live        # live mode (needs POLYMARKET_PRIVATE_KEY)
  python -m bots.polymarket.run_mm --once        # single quote cycle, then exit
  python -m bots.polymarket.run_mm --once -v     # verbose single cycle (debug)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

import httpx

from .auth import get_client as get_clob_client
from .scanner import discover_markets
from .strategy_btc_mm import BTCUpDownMM, is_updown_market, parse_window_from_question
from .fair_price import bootstrap_from_klines, get_btc_state

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for lib in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def select_markets(all_markets, max_markets: int = 3):
    """
    Filter and rank Up/Down markets.

    Criteria:
    1. Must match is_updown_market() pattern.
    2. Must have parseable time window.
    3. Must expire in the future (> 30s away).
    4. Prefer nearest-expiry (quote markets expiring soonest first).
    5. Limit to max_markets.
    """
    now = time.time()
    tradeable = []
    for m in all_markets:
        if not is_updown_market(m.question):
            logger.debug(f"Skip (not up/down): {m.question[:50]}")
            continue
        window = parse_window_from_question(m.question)
        if not window:
            logger.debug(f"Skip (no parseable window): {m.question[:50]}")
            continue
        start_ts, end_ts = window
        if end_ts - now < 60:
            logger.debug(f"Skip (expiring <60s): {m.question[:50]}")
            continue
        tradeable.append((m, start_ts, end_ts))

    # Sort by soonest expiry (quote markets closest to resolution first)
    tradeable.sort(key=lambda x: x[2])
    return [m for m, _, _ in tradeable[:max_markets]]


async def run_mm(
    *,
    live: bool = False,
    once: bool = False,
    verbose: bool = False,
    gamma: float = 0.5,
    order_size_usdc: float = 10.0,
    requote_interval: float = 15.0,
    max_markets: int = 3,
) -> None:
    """
    Main loop: discover markets → launch BTCUpDownMM per market → profit.
    """
    clob = None
    if live:
        clob = get_clob_client()
        if not clob:
            logger.error("Live mode requires POLYMARKET_PRIVATE_KEY env var.")
            sys.exit(1)
        logger.info("Live mode: real orders will be placed.")
    else:
        logger.info("Paper mode: no real orders. Set --live for real trading.")

    print(f"\n🤖 BTC Up/Down Market Maker (Avellaneda-Stoikov)")
    print(f"   Mode: {'LIVE' if live else 'PAPER'}")
    print(f"   γ={gamma} | order_size=${order_size_usdc:.0f} | requote={requote_interval}s")
    print(f"   Max concurrent markets: {max_markets}")
    print()

    # Bootstrap BTC price data once at startup
    state = get_btc_state()
    if len(state.prices) < 3:
        print("Bootstrapping Binance price data...")
        bootstrap_from_klines()

    async with httpx.AsyncClient() as client:
        while True:
            # Discover active BTC markets
            all_markets = await discover_markets(client)
            logger.info(f"Discovered {len(all_markets)} total markets")

            # Filter to Up/Down tradeable subset
            selected = select_markets(all_markets, max_markets=max_markets)

            if not selected:
                logger.warning(
                    f"No tradeable Up/Down markets found "
                    f"(total={len(all_markets)}). Retry in 30s."
                )
                if once:
                    print("\n❌ No markets available. Check market discovery filters.")
                    break
                await asyncio.sleep(30)
                continue

            logger.info(f"Selected {len(selected)} markets to quote:")
            for m in selected:
                w = parse_window_from_question(m.question)
                if w:
                    duration = w[1] - w[0]
                    remaining = max(0, w[1] - time.time())
                    logger.info(
                        f"  • {m.question[:65]} "
                        f"| window={duration:.0f}s | remaining={remaining:.0f}s"
                    )

            if once:
                # Single quote cycle: compute and print quotes, then exit
                for m in selected:
                    try:
                        mm = BTCUpDownMM(
                            market=m,
                            clob_client=clob,
                            gamma=gamma,
                            order_size_usdc=order_size_usdc,
                            requote_interval=requote_interval,
                            paper=not live,
                        )
                        bid, ask = mm.compute_quotes()
                        q = mm.last_quotes
                        w = parse_window_from_question(m.question)
                        duration = (w[1] - w[0]) if w else 0
                        remaining = max(0, w[1] - time.time()) if w else 0
                        print(
                            f"\n  Market:    {m.question}\n"
                            f"  Window:    {duration:.0f}s | remaining: {remaining:.0f}s\n"
                            f"  P_fair:    {q.p_fair:.4f} (P(Up) incl. momentum)\n"
                            f"  Reserve:   {q.reservation:.4f}\n"
                            f"  Bid YES:   {bid:.4f} ({bid*100:.2f}¢)\n"
                            f"  Ask YES:   {ask:.4f} ({ask*100:.2f}¢)\n"
                            f"  Spread:    {q.spread:.4f} ({q.spread*100:.2f}pp)\n"
                            f"  κ={q.kappa:.2f}  σ={q.sigma:.2%}  γ={q.gamma}"
                        )
                    except Exception as e:
                        logger.error(f"Failed to compute quotes for {m.question[:40]}: {e}", exc_info=verbose)
                break

            # Run all selected MMs concurrently until they expire
            tasks = []
            for m in selected:
                try:
                    mm = BTCUpDownMM(
                        market=m,
                        clob_client=clob,
                        gamma=gamma,
                        order_size_usdc=order_size_usdc,
                        requote_interval=requote_interval,
                        paper=not live,
                    )
                    tasks.append(asyncio.create_task(mm.run()))
                except Exception as e:
                    logger.error(f"Failed to start MM for {m.question[:40]}: {e}")

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            if once:
                break

            # All expired → rediscover
            logger.info("All markets expired. Rediscovering in 10s...")
            await asyncio.sleep(10)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BTC Up/Down Market Maker (Avellaneda-Stoikov)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m bots.polymarket.run_mm --once         # Single quote cycle (dry run)
  python -m bots.polymarket.run_mm --once -v      # Verbose dry run
  python -m bots.polymarket.run_mm                # Paper MM loop
  python -m bots.polymarket.run_mm --live         # Live MM (needs POLYMARKET_PRIVATE_KEY)
  python -m bots.polymarket.run_mm --gamma 0.3 --size 5 --requote 30
        """,
    )
    parser.add_argument("--live", action="store_true", help="Live trading mode")
    parser.add_argument("--once", action="store_true", help="Single cycle then exit")
    parser.add_argument("--gamma", type=float, default=0.5, help="Risk aversion γ (default: 0.5)")
    parser.add_argument("--size", type=float, default=10.0, dest="order_size_usdc",
                        help="Order size in USDC (default: 10)")
    parser.add_argument("--requote", type=float, default=15.0, dest="requote_interval",
                        help="Requote interval in seconds (default: 15)")
    parser.add_argument("--max-markets", type=int, default=3, dest="max_markets",
                        help="Max concurrent markets (default: 3)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)
    asyncio.run(run_mm(
        live=args.live,
        once=args.once,
        verbose=args.verbose,
        gamma=args.gamma,
        order_size_usdc=args.order_size_usdc,
        requote_interval=args.requote_interval,
        max_markets=args.max_markets,
    ))


if __name__ == "__main__":
    main()
