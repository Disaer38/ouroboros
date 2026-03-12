"""
Dynamic kappa (κ) estimator for Avellaneda-Stoikov market making.

κ controls the fill probability decay: λ(δ) = A · e^(-κ·δ)
Higher κ → fills drop off fast (liquid market, need tight spreads).
Lower κ → fills stay reasonable even at wider spreads (thin market).

We estimate κ from:
1. Rolling history of our own quote placements vs actual fills.
2. Bootstrap from Polymarket public trade history (arrival rate estimation).
3. Observed market spread as a soft prior.

When no history is available, use empirical default for binary prediction markets.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Default κ for thin binary prediction markets (empirical: 5-min BTC markets)
DEFAULT_KAPPA = 50.0
DEFAULT_A = 200.0  # arrival rate constant (orders per unit time)

WINDOW_SIZE = 100  # rolling window of fill observations
POLYMARKET_DATA_API = "https://data-api.polymarket.com"


@dataclass
class FillObservation:
    """Record of a single quote placement outcome."""
    spread_from_mid: float  # δ = |quote_price - mid_price|
    filled: bool             # was this order filled?
    timestamp: float = field(default_factory=time.time)


class KappaEstimator:
    """
    Estimates κ from rolling fill history using MLE-like approach.

    The model: P(fill | δ) = e^(-κ·δ)
    Given N observations, κ = -mean(ln(fill_rate_per_δ_bucket)) / mean(δ)
    """

    def __init__(self, default_kappa: float = DEFAULT_KAPPA):
        self.default_kappa = default_kappa
        self.observations: deque[FillObservation] = deque(maxlen=WINDOW_SIZE)
        self._cached_kappa: Optional[float] = None
        self._cache_ts: float = 0.0
        self._bootstrapped: bool = False

    def record_fill(self, spread_from_mid: float, filled: bool) -> None:
        """Record whether a quote at distance δ from mid was filled."""
        self.observations.append(FillObservation(
            spread_from_mid=max(0.001, spread_from_mid),
            filled=filled,
        ))
        self._cached_kappa = None  # invalidate cache

    def record_order_fill(self, quote_price: float, mid_price: float, filled: bool) -> None:
        """
        Record fill outcome from a placed order.

        Computes δ = |quote_price - mid_price| and records it.
        """
        delta = abs(quote_price - mid_price)
        self.record_fill(spread_from_mid=delta, filled=filled)

    def bootstrap_from_polymarket(self, token_id: str, limit: int = 100) -> bool:
        """
        Bootstrap κ from Polymarket public trade history for a token.

        Uses the Data API to fetch recent trades and estimate the arrival rate.
        We infer κ by computing how trades are distributed around the mid price.

        Returns True if successfully bootstrapped, False otherwise.
        """
        if self._bootstrapped:
            return True
        try:
            resp = requests.get(
                f"{POLYMARKET_DATA_API}/trades",
                params={"asset_id": token_id, "limit": limit},
                timeout=10.0,
            )
            resp.raise_for_status()
            trades = resp.json()

            if not trades or len(trades) < 5:
                logger.debug(f"Too few trades for κ bootstrap: {len(trades) if trades else 0}")
                return False

            # Extract prices to compute rolling mid
            prices = [float(t.get("price", 0)) for t in trades if t.get("price")]
            if len(prices) < 5:
                return False

            # Compute rolling mid from recent trade prices
            mid = sum(prices[:20]) / min(20, len(prices))

            # Treat each trade as a "fill at distance δ from mid"
            deltas = [abs(p - mid) for p in prices if 0 < p < 1]

            if not deltas:
                return False

            # Bootstrap: inject synthetic filled observations
            # All trade-records are fills (they completed) at their respective δ
            for delta in deltas[:50]:  # limit synthetic bootstrap size
                self.observations.append(FillObservation(
                    spread_from_mid=max(0.001, delta),
                    filled=True,
                    timestamp=time.time(),
                ))

            self._bootstrapped = True
            kappa = self.estimate_kappa()
            logger.info(
                f"κ bootstrapped from {len(deltas)} Polymarket trades: "
                f"κ={kappa:.3f} | mid={mid:.4f}"
            )
            return True

        except Exception as e:
            logger.warning(f"Polymarket κ bootstrap failed: {e}")
            return False

    def estimate_kappa(self) -> float:
        """
        Estimate κ from recorded observations.

        Falls back to default_kappa if insufficient data.
        """
        # Return cached value if fresh (< 30s)
        if self._cached_kappa is not None and (time.time() - self._cache_ts) < 30:
            return self._cached_kappa

        obs = list(self.observations)
        if len(obs) < 10:
            return self.default_kappa

        filled = [o for o in obs if o.filled]

        if not filled:
            # No fills observed → use wider default (high κ)
            return min(self.default_kappa * 2, 200.0)

        fill_rate = len(filled) / len(obs)
        avg_spread = sum(o.spread_from_mid for o in obs) / len(obs)

        if fill_rate <= 0 or avg_spread <= 0:
            return self.default_kappa

        # κ = -ln(fill_rate) / avg_spread
        kappa = -math.log(fill_rate) / avg_spread
        kappa = max(0.1, min(10.0, kappa))

        self._cached_kappa = kappa
        self._cache_ts = time.time()
        logger.debug(
            f"κ estimated: {kappa:.3f} "
            f"(fill_rate={fill_rate:.2%}, avg_spread={avg_spread:.4f}, n={len(obs)})"
        )
        return kappa

    def fill_probability(self, delta: float, A: float = DEFAULT_A) -> float:
        """
        Compute arrival rate λ(δ) = A · e^(-κ·δ).

        Args:
            delta: Distance from mid price (0 = at mid, 0.5 = far side).
            A: Baseline arrival rate.

        Returns:
            Expected fill probability per unit time.
        """
        kappa = self.estimate_kappa()
        return A * math.exp(-kappa * delta)

    def adapt_from_spread(self, observed_spread: float) -> None:
        """
        Heuristic: if we observe a wide market spread, κ is likely low.
        Seed the estimator with this knowledge without actual fill data.
        """
        # A spread of 0.10 with ~50% fill rate implies κ ≈ -ln(0.5)/0.05 ≈ 13.8
        # A spread of 0.40 with ~50% fill rate implies κ ≈ -ln(0.5)/0.20 ≈ 3.5
        implied_kappa = -math.log(0.5) / max(observed_spread / 2, 0.001)
        implied_kappa = max(5.0, min(200.0, implied_kappa))
        self.default_kappa = implied_kappa
        logger.debug(f"κ prior updated from spread {observed_spread:.3f}: κ_prior={implied_kappa:.2f}")
