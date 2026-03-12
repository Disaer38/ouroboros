"""
Polymarket Arbitrage Bot — main loop.

Modes:
  paper   — scan and simulate trades, log P&L, no real transactions
  live    — scan and execute real trades (requires wallet + API keys)

Usage:
  python -m bots.polymarket.bot --mode paper
  python -m bots.polymarket.bot --mode paper --interval 30 --max-position 10
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

import httpx

from .paper_trader import PaperTrader
from .scanner import discover_markets, scan_neg_risk

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("polymarket.bot")


def print_stats(paper: PaperTrader) -> None:
    stats = paper.get_stats()
    print("\n" + "="*55)
    print("  PAPER TRADING STATS")
    print("="*55)
    print(f"  Opportunities scanned : {stats['total_opportunities']}")
    print(f"  Trades opened         : {stats['total_trades']}")
    print(f"  Open                  : {stats['open_trades']}")
    print(f"  Resolved              : {stats['closed_trades']}")
    print(f"  Total PnL             : {stats['total_pnl_usdc']:+.4f} USDC")
    print(f"  Win rate              : {stats['win_rate_pct']:.1f}%")
    print(f"  ROI                   : {stats['roi_pct']:+.2f}%")
    print(f"  Total invested        : {stats['total_invested_usdc']:.2f} USDC")
    print("="*55 + "\n")


async def run_paper(
    interval_seconds: int = 30,
    max_position_usdc: float = 10.0,
    profit_threshold: float = 0.97,
    min_depth_usdc: float = 2.0,
    max_total_exposure: float = 50.0,
) -> None:
    """
    Paper trading loop.

    Every interval_seconds:
      1. Discover active BTC/ETH 5/15-min markets
      2. Fetch orderbooks and detect neg-risk arb opportunities
      3. Simulate trades (log to Drive, no on-chain execution)
      4. Resolve expired trades and track P&L
      5. Print stats
    """
    paper = PaperTrader(max_position_usdc=max_position_usdc)
    total_invested = 0.0

    logger.info(f"Starting PAPER mode | interval={interval_seconds}s | max_pos={max_position_usdc} USDC")
    logger.info(f"Profit threshold: {(1 - profit_threshold)*100:.1f}%+ net | Max exposure: {max_total_exposure} USDC")

    scan_count = 0

    async with httpx.AsyncClient() as client:
        # Discover markets once — they don't change frequently
        markets = await discover_markets(client)
        if not markets:
            logger.warning("No BTC/ETH 5/15-min markets found. Retrying in 60s...")
            await asyncio.sleep(60)
            markets = await discover_markets(client)

        if not markets:
            logger.error("Still no markets found. Check API connectivity.")
            return

        logger.info(f"Monitoring {len(markets)} markets")

        while True:
            scan_count += 1
            logger.info(f"--- Scan #{scan_count} | {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---")

            # 1. Resolve expired paper trades
            resolved = paper.check_resolutions()
            if resolved:
                for t in resolved:
                    logger.info(f"  Resolved: {t.market_question[:40]} pnl={t.pnl:+.4f}")

            # 2. Refresh market list every 10 scans
            if scan_count % 10 == 0:
                new_markets = await discover_markets(client)
                if new_markets:
                    markets = new_markets
                    logger.info(f"Refreshed market list: {len(markets)} markets")

            # 3. Scan for opportunities
            opportunities = await scan_neg_risk(
                client,
                markets,
                threshold=profit_threshold,
                min_depth_usdc=min_depth_usdc,
                max_position_usdc=max_position_usdc,
            )

            if not opportunities:
                logger.info("  No opportunities this scan.")
            else:
                logger.info(f"  Found {len(opportunities)} opportunities:")
                for opp in opportunities[:5]:  # show top 5
                    logger.info(f"    {opp}")
                    paper.log_opportunity(opp)

                    # Trade if under max exposure
                    remaining_budget = max_total_exposure - total_invested
                    if remaining_budget < 1.0:
                        logger.info("  Max exposure reached, not trading.")
                        continue

                    trade_size = min(opp.max_size_usdc, max_position_usdc, remaining_budget)
                    trade = paper.simulate_trade(opp, size_usdc=trade_size)
                    if trade:
                        total_invested += trade.size_usdc

            # 4. Print stats every 5 scans
            if scan_count % 5 == 0:
                print_stats(paper)

            await asyncio.sleep(interval_seconds)


async def run_live(
    private_key: str,
    interval_seconds: int = 10,
    max_position_usdc: float = 10.0,
    profit_threshold: float = 0.97,
) -> None:
    """
    Live trading mode (stub — implement after paper trading validation).

    Requires:
      - POLYMARKET_PRIVATE_KEY env var
      - POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE
    """
    raise NotImplementedError(
        "Live trading not yet implemented. Run --mode paper first to validate strategy."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage Bot")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Scan interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--max-position",
        type=float,
        default=10.0,
        help="Max USDC per position (default: 10)",
    )
    parser.add_argument(
        "--max-exposure",
        type=float,
        default=50.0,
        help="Max total USDC exposure (default: 50)",
    )
    parser.add_argument(
        "--profit-threshold",
        type=float,
        default=0.97,
        help="Enter trade when YES+NO < threshold (default: 0.97 = 3%+ profit)",
    )
    parser.add_argument(
        "--min-depth",
        type=float,
        default=2.0,
        help="Min USDC depth required on each side (default: 2)",
    )
    args = parser.parse_args()

    if args.mode == "paper":
        asyncio.run(run_paper(
            interval_seconds=args.interval,
            max_position_usdc=args.max_position,
            profit_threshold=args.profit_threshold,
            min_depth_usdc=args.min_depth,
            max_total_exposure=args.max_exposure,
        ))
    elif args.mode == "live":
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not pk:
            print("ERROR: Set POLYMARKET_PRIVATE_KEY env var for live trading")
            sys.exit(1)
        asyncio.run(run_live(
            private_key=pk,
            interval_seconds=args.interval,
            max_position_usdc=args.max_position,
            profit_threshold=args.profit_threshold,
        ))


if __name__ == "__main__":
    main()
