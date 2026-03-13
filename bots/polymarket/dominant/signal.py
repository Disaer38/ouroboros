"""
Signal generation for the Dominant Side strategy.

Core logic:
  - Fetch YES and NO orderbook best asks.
  - Treat best_ask as market-implied probability (standard on Polymarket AMM).
  - If P(YES) >= threshold → BUY_YES signal.
  - If P(NO) >= threshold → BUY_NO signal (i.e. P(YES) <= 1 - threshold).
  - Otherwise → NO_SIGNAL.

The ask price IS the probability on Polymarket because:
  1. The AMM sets prices such that YES_price + NO_price ≈ 1.0.
  2. Asking to buy YES at price X means paying $X for $1 payout if YES wins.
     Therefore, X = market consensus probability of YES.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..clob_client import Orderbook

logger = logging.getLogger(__name__)


class Side(str, Enum):
    YES = "YES"
    NO = "NO"
    NONE = "NONE"


@dataclass
class Signal:
    """A trading signal from the Dominant Side strategy."""
    market_id: str
    side: Side                   # Which token to buy (or NONE)
    prob: float                  # Implied probability of dominant side
    price: float                 # Ask price to fill at
    depth_usdc: float            # Available liquidity at that price
    time_remaining_sec: float    # Seconds to market expiry

    @property
    def is_actionable(self) -> bool:
        """True if signal meets all requirements for a trade."""
        return self.side != Side.NONE

    def __str__(self) -> str:
        if self.side == Side.NONE:
            return f"[NO_SIGNAL] {self.market_id[:12]} (p_yes={1 - self.prob:.3f}–{self.prob:.3f})"
        return (
            f"[{self.side}] {self.market_id[:12]} "
            f"p={self.prob:.3f} @ {self.price:.3f} "
            f"depth=${self.depth_usdc:.1f} ttl={self.time_remaining_sec:.0f}s"
        )


class SignalGenerator:
    """
    Stateless signal generator.

    Usage:
        gen = SignalGenerator(config)
        signal = gen.generate(market, ob_yes, ob_no)
        if signal.is_actionable:
            exchange.buy(signal, market)
    """

    def __init__(self, config):
        self.config = config

    def generate(
        self,
        market,           # ActiveMarket from market_discovery
        ob_yes: Orderbook,
        ob_no: Orderbook,
    ) -> Signal:
        """
        Compute signal from current orderbooks.

        Args:
            market: The active market (carries end_date, token IDs).
            ob_yes: YES token orderbook.
            ob_no:  NO token orderbook.

        Returns:
            Signal with side=YES, NO, or NONE.
        """
        # Use best ask as implied probability
        p_yes = ob_yes.best_ask
        p_no = ob_no.best_ask
        time_remaining = market.seconds_to_expiry

        # Sanity checks
        if not (0.01 < p_yes < 0.99 and 0.01 < p_no < 0.99):
            logger.debug(f"Unrealistic prices: p_yes={p_yes} p_no={p_no}")
            return self._no_signal(market.market_id, p_yes, time_remaining)

        # Time filter: don't enter in last N seconds
        if time_remaining < self.config.min_time_remaining_sec:
            logger.debug(f"Too close to expiry ({time_remaining:.0f}s): skipping")
            return self._no_signal(market.market_id, p_yes, time_remaining)

        threshold = self.config.min_dominant_prob

        if p_yes >= threshold:
            # YES is dominant
            depth = ob_yes.depth_usdc("ask", levels=5)
            logger.info(
                f"BUY_YES signal: {market.question[:50]} "
                f"p_yes={p_yes:.3f} depth=${depth:.1f} ttl={time_remaining:.0f}s"
            )
            return Signal(
                market_id=market.market_id,
                side=Side.YES,
                prob=p_yes,
                price=p_yes,
                depth_usdc=depth,
                time_remaining_sec=time_remaining,
            )

        if p_no >= threshold:
            # NO is dominant
            depth = ob_no.depth_usdc("ask", levels=5)
            logger.info(
                f"BUY_NO signal: {market.question[:50]} "
                f"p_no={p_no:.3f} depth=${depth:.1f} ttl={time_remaining:.0f}s"
            )
            return Signal(
                market_id=market.market_id,
                side=Side.NO,
                prob=p_no,
                price=p_no,
                depth_usdc=depth,
                time_remaining_sec=time_remaining,
            )

        return self._no_signal(market.market_id, p_yes, time_remaining)

    def _no_signal(self, market_id: str, p_yes: float, time_remaining: float) -> Signal:
        return Signal(
            market_id=market_id,
            side=Side.NONE,
            prob=p_yes,
            price=p_yes,
            depth_usdc=0.0,
            time_remaining_sec=time_remaining,
        )