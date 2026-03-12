"""
Polymarket Arbitrage Bot — main loop.

Strategies:
  - neg_risk: Buy YES + NO when total cost < threshold (guaranteed $1 payout)

Modes:
  - paper: Simulated trades (no wallet needed)
  - live:  Real trades via CLOB API (requires POLYMARKET_PRIVATE_KEY)

Usage:
  python -m bots.polymarket.bot --mode paper --once
  python -m bots.polymarket.bot --mode paper --interval 30
  python -m bots.polymarket.bot --mode live --max-position 5

See README.md for full documentation.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

import httpx

from .paper_trader import PaperTrader
from .scanner import discover_markets, scan_all, _is_intraday

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy third-party loggers
    for lib in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


async def run_paper(
    *,
    interval: float = 30.0,
    max_position: float = 10.0,
    max_exposure: float = 50.0,
    profit_threshold: float = 0.97,
    min_depth: float = 2.0,
    once: bool = False,
    verbose: bool = False,
) -> None:
    """
    Paper trading loop.

    Scans for neg-risk opportunities every `interval` seconds and
    simulates trades without real money.
    """
    trader = PaperTrader(
        max_position_usdc=max_position,
        max_exposure_usdc=max_exposure,
    )

    print(f"\n🤖 Polymarket Paper Trader")
    print(f"   Strategy: neg-risk (YES+NO < {profit_threshold})")
    print(f"   Max position: ${max_position:.0f} | Max exposure: ${max_exposure:.0f}")
    print(f"   Scan interval: {interval}s {'(single scan)' if once else ''}")
    print()

    scan_count = 0
    opp_log_path = os.path.join(
        os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros"),
        "logs",
        "opportunities.jsonl",
    )

    async with httpx.AsyncClient() as client:
        while True:
            scan_count += 1
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] Scan #{scan_count} — discovering markets...")

            try:
                markets = await discover_markets(client)
            except Exception as e:
                logger.error(f"Market discovery failed: {e}")
                if once:
                    break
                await asyncio.sleep(interval)
                continue

            if not markets:
                print("  ⚠️  No active markets found. Markets may not be open yet.")
                print("  ℹ️  BTC/ETH 5min/15min markets run on weekdays, ~9AM-5PM ET")
                if once:
                    break
                await asyncio.sleep(interval)
                continue

            intraday_count = sum(1 for m in markets if _is_intraday(m.question))
            print(f"  Found {len(markets)} markets ({intraday_count} intraday)")

            # Scan for opportunities
            try:
                opportunities = await scan_all(
                    client,
                    markets,
                    profit_threshold=profit_threshold,
                    min_depth_usdc=min_depth,
                )
            except Exception as e:
                logger.error(f"Scan failed: {e}")
                if once:
                    break
                await asyncio.sleep(interval)
                continue

            # Log all opportunities to Drive
            if opportunities:
                _log_opportunities(opportunities, opp_log_path)

            # Enter trades for best opportunities
            for opp in opportunities:
                print(
                    f"  💡 ARB: {opp.market.question[:55]}\n"
                    f"     YES={opp.yes_ask:.3f} + NO={opp.no_ask:.3f} = "
                    f"{opp.total_cost:.3f} → {opp.profit_pct:.1f}% gross margin"
                )
                trader.enter_trade(opp)

            if not opportunities:
                print("  📭 No opportunities this scan.")

            if once:
                break

            print()
            await asyncio.sleep(interval)

    # Final summary
    trader.print_summary()


async def run_live(
    *,
    interval: float = 30.0,
    max_position: float = 5.0,
    max_exposure: float = 50.0,
    profit_threshold: float = 0.97,
    min_depth: float = 5.0,
    once: bool = False,
) -> None:
    """
    Live trading loop.

    Requires POLYMARKET_PRIVATE_KEY environment variable.
    """
    from .auth import get_client as get_clob_client

    clob = get_clob_client()
    if not clob:
        logger.error(
            "No POLYMARKET_PRIVATE_KEY set. "
            "Set the env var or use --mode paper for paper trading."
        )
        sys.exit(1)

    print(f"\n🚨 LIVE TRADING MODE")
    print(f"   Max position: ${max_position:.0f} | Max exposure: ${max_exposure:.0f}")
    print(f"   Profit threshold: {profit_threshold}")
    print()

    async with httpx.AsyncClient() as http:
        while True:
            markets = await discover_markets(http)
            if not markets:
                print("No markets. Waiting...")
                if once:
                    break
                await asyncio.sleep(interval)
                continue

            opportunities = await scan_all(
                http,
                markets,
                profit_threshold=profit_threshold,
                min_depth_usdc=min_depth,
            )

            for opp in opportunities:
                # TODO: execute via CLOB API using clob client
                # This is the paper-to-live bridge — implement after paper testing
                logger.info(f"[LIVE TODO] Would trade: {opp.market.question[:50]}")

            if once:
                break

            await asyncio.sleep(interval)


def _log_opportunities(opportunities: list, path: str) -> None:
    """Append detected opportunities to Drive log."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with open(path, "a") as f:
            for opp in opportunities:
                record = {
                    "ts": ts,
                    "market_id": opp.market.id,
                    "question": opp.market.question,
                    "yes_ask": opp.yes_ask,
                    "no_ask": opp.no_ask,
                    "total_cost": opp.total_cost,
                    "profit_pct": opp.profit_pct,
                    "yes_depth": opp.yes_depth,
                    "no_depth": opp.no_depth,
                    "max_size_usdc": opp.max_size_usdc,
                }
                f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning(f"Failed to log opportunity: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket neg-risk arbitrage bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m bots.polymarket.bot --once              # Single scan (dry run)
  python -m bots.polymarket.bot --interval 30       # Paper trade every 30s
  python -m bots.polymarket.bot --mode live         # Live (needs private key)
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Scan interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--max-position",
        type=float,
        default=10.0,
        dest="max_position",
        help="Max USDC per trade (default: 10)",
    )
    parser.add_argument(
        "--max-exposure",
        type=float,
        default=50.0,
        dest="max_exposure",
        help="Max total USDC deployed (default: 50)",
    )
    parser.add_argument(
        "--profit-threshold",
        type=float,
        default=0.97,
        dest="profit_threshold",
        help="Enter if YES+NO < this value (default: 0.97)",
    )
    parser.add_argument(
        "--min-depth",
        type=float,
        default=2.0,
        dest="min_depth",
        help="Minimum USDC depth per side (default: 2)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    common = dict(
        interval=args.interval,
        max_position=args.max_position,
        max_exposure=args.max_exposure,
        profit_threshold=args.profit_threshold,
        min_depth=args.min_depth,
        once=args.once,
    )

    if args.mode == "paper":
        asyncio.run(run_paper(**common, verbose=args.verbose))
    else:
        asyncio.run(run_live(**common))


if __name__ == "__main__":
    main()
