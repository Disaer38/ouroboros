"""
Polymarket Negative Risk Arbitrage Bot — Phase 1
Read-only opportunity detector (no trading).

Usage:
    cd polymarket_arb
    python main.py

    # Debug mode (see all book fetches):
    LOG_LEVEL=DEBUG python main.py
"""
import asyncio
import logging
import sys
import os
import time
from decimal import Decimal

# Add parent dir so imports work when run from polymarket_arb/
sys.path.insert(0, os.path.dirname(__file__))

from config import POLL_INTERVAL_SEC, LOG_LEVEL
from services.market_data import MarketDataService
from services.execution import ExecutionService
from strategy.neg_risk import NegativeRiskStrategy

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

MARKET_REFRESH_INTERVAL = 30   # refresh active market list every N seconds


async def run():
    strategy = NegativeRiskStrategy()
    executor = ExecutionService(simulation=True)
    opportunities_seen = 0
    polls = 0
    last_market_refresh = 0.0

    async with MarketDataService() as mds:
        markets = []

        logger.info("=" * 60)
        logger.info("Polymarket Neg-Risk Arb Bot | Phase 1 (simulation mode)")
        logger.info(f"Threshold : sum < {float(strategy.threshold):.4f}")
        logger.info(f"Fees      : 2% per leg (4% round-trip)")
        logger.info(f"Execution : simulation=True (no real orders)")
        logger.info("=" * 60)

        while True:
            now = time.time()

            # Refresh market list periodically
            if now - last_market_refresh > MARKET_REFRESH_INTERVAL:
                markets = await mds.fetch_active_markets()
                last_market_refresh = now
                if not markets:
                    logger.warning("No active markets found. Retrying in 10s...")
                    await asyncio.sleep(10)
                    continue
                logger.info(f"Monitoring {len(markets)} markets: {[m.slug for m in markets]}")

            if not markets:
                await asyncio.sleep(5)
                continue

            # Poll all markets concurrently
            polls += 1
            tasks = [mds.fetch_both_books(market) for market in markets]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for market, result in zip(markets, results):
                if isinstance(result, Exception):
                    logger.warning(f"Error fetching {market.slug}: {result}")
                    continue

                up_book, down_book = result
                if up_book is None or down_book is None:
                    continue

                opp = strategy.check(market, up_book, down_book)
                if opp:
                    opportunities_seen += 1
                    print(f"\n{'='*60}")
                    print(f"  ARBITRAGE OPPORTUNITY #{opportunities_seen}")
                    print(f"  Market  : {opp.market.slug}")
                    print(f"  Question: {opp.market.question}")
                    print(f"  UP ask  : {float(opp.up_ask):.4f} ({float(opp.up_ask_size):.1f} shares avail)")
                    print(f"  DOWN ask: {float(opp.down_ask):.4f} ({float(opp.down_ask_size):.1f} shares avail)")
                    print(f"  Sum     : {float(opp.total_cost):.4f}  (threshold: {float(strategy.threshold):.4f})")
                    print(f"  Gross   : +{float(opp.gross_edge)*100:.2f}%")
                    print(f"  Net     : +{float(opp.net_edge)*100:.2f}% (after 4% fees)")
                    print(f"  Max     : {float(opp.max_shares):.1f} shares → ${float(opp.max_profit):.4f} profit")
                    print(f"{'='*60}\n")

                    # Execute (simulated)
                    result = await executor.execute_neg_risk(
                        market_slug=opp.market.slug,
                        up_token_id=opp.market.up_token_id,
                        down_token_id=opp.market.down_token_id,
                        up_ask=opp.up_ask,
                        down_ask=opp.down_ask,
                        shares=min(opp.max_shares, Decimal("100")),  # cap at 100 shares for safety
                    )

            if polls % 60 == 0:
                logger.info(f"Poll #{polls} | Opportunities: {opportunities_seen} | Sim P&L: {executor.stats}")

            await asyncio.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
