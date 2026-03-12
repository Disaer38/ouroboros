"""
Dynamic kappa (κ) estimator for Avellaneda-Stoikov market making.

κ controls the fill probability decay: λ(δ) = A · e^(-κ·δ)
Higher κ → fills drop off fast (liquid market, need tight spreads).
Lower κ → fills stay reasonable even at wider spreads (thin market).

We estimate κ from the rolling history of our own quote placements
vs. actual fills. When no history is available, we use a market-adaptive
default based on observed bid-ask spread width.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Default κ for thin binary prediction markets (empirical)
DEFAULT_KAPPA = 1.5
DEFAULT_A = 10.0  # arrival rate constant (orders per unit time)

WINDOW_SIZE = 50  # rolling window of fill observations


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

    def record_fill(self, spread_from_mid: float, filled: bool) -> None:
        """Record whether a quote at distance δ from mid was filled."""
        self.observations.append(FillObservation(
            spread_from_mid=max(0.001, spread_from_mid),
            filled=filled,
        ))
        self._cached_kappa = None  # invalidate cache

    def estimate_kappa(self) -> float:
        """
        Estimate κ from recorded observations.

        Falls back to default_kappa if insufficient data.
        """
        # Return cached value if fresh (< 30s)
        if self._cached_kappa and (time.time() - self._cache_ts) < 30:
            return self._cached_kappa

        obs = list(self.observations)
        if len(obs) < 10:
            return self.default_kappa

        # Group by spread buckets and compute fill rate per bucket
        # Simple approach: use each observation directly
        # κ ≈ -ln(fill_rate) / mean_spread where fill_rate = filled_count / total
        filled = [o for o in obs if o.filled]
        unfilled = [o for o in obs if not o.filled]

        if not filled:
            # No fills observed → market is very liquid or spreads are too wide
            # Use wider default (high κ = fills only at very tight spreads)
            return min(self.default_kappa * 2, 5.0)

        fill_rate = len(filled) / len(obs)
        avg_spread = sum(o.spread_from_mid for o in obs) / len(obs)

        if fill_rate <= 0 or avg_spread <= 0:
            return self.default_kappa

        kappa = -math.log(fill_rate) / avg_spread
        kappa = max(0.1, min(10.0, kappa))  # clamp to reasonable range

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
        # Use as a soft prior: inject synthetic observations
        implied_kappa = -math.log(0.5) / max(observed_spread / 2, 0.001)
        implied_kappa = max(0.5, min(8.0, implied_kappa))
        # Override default if observed spread suggests different regime
        self.default_kappa = implied_kappa
        logger.debug(f"κ prior updated from spread {observed_spread:.3f}: κ_prior={implied_kappa:.2f}")
