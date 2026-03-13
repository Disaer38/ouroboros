"""
NegativeRiskStrategy — pure opportunity detection logic.

No side effects. Takes orderbooks, returns Opportunity or None.
"""
from __future__ import annotations
from decimal import Decimal
from typing import Optional
import logging

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import ENTRY_THRESHOLD, FEE_RATE, MIN_EDGE, MAX_SHARES_PER_TRADE
from core.models import MarketMeta, OrderBook, Opportunity

logger = logging.getLogger(__name__)


class NegativeRiskStrategy:
    """
    Detects negative-risk arbitrage opportunities.

    An opportunity exists when:
        best_ask(UP) + best_ask(DOWN) < 1.0 - 2*FEE_RATE - MIN_EDGE
                                        (default threshold = 0.955)

    Sizing: equal shares on both sides (guh123 pattern).
    """

    def __init__(
        self,
        threshold: Decimal = ENTRY_THRESHOLD,
        max_shares: Decimal = MAX_SHARES_PER_TRADE,
    ):
        self.threshold = threshold
        self.max_shares = max_shares

    def check(
        self,
        market: MarketMeta,
        up_book: OrderBook,
        down_book: OrderBook,
    ) -> Optional[Opportunity]:
        """
        Evaluate orderbooks for a neg-risk opportunity.
        Returns Opportunity if one exists, else None.
        """
        up_ask   = up_book.best_ask
        down_ask = down_book.best_ask
        up_size  = up_book.best_ask_size
        down_size = down_book.best_ask_size

        if up_ask is None or down_ask is None:
            logger.debug(f"{market.slug}: missing ask prices")
            return None

        total = up_ask + down_ask

        logger.debug(
            f"{market.slug}: up_ask={float(up_ask):.4f} "
            f"down_ask={float(down_ask):.4f} "
            f"sum={float(total):.4f} "
            f"threshold={float(self.threshold):.4f}"
        )

        if total >= self.threshold:
            return None  # No edge

        # Opportunity found!
        opp = Opportunity(
            market=market,
            up_ask=up_ask,
            down_ask=down_ask,
            up_ask_size=up_size or Decimal("0"),
            down_ask_size=down_size or Decimal("0"),
        )

        logger.info(
            f"🎯 OPPORTUNITY: {market.slug} | "
            f"sum={float(total):.4f} | "
            f"net_edge={float(opp.net_edge):.4f} ({float(opp.net_edge)*100:.2f}%) | "
            f"max_shares={float(opp.max_shares):.1f} | "
            f"max_profit=${float(opp.max_profit):.4f}"
        )
        return opp
