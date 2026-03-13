"""Core data models."""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional
import time


@dataclass
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    token_id: str
    bids: list[PriceLevel] = field(default_factory=list)
    asks: list[PriceLevel] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    @property
    def best_ask(self) -> Optional[Decimal]:
        if self.asks:
            return min(self.asks, key=lambda x: x.price).price
        return None

    @property
    def best_ask_size(self) -> Optional[Decimal]:
        if self.asks:
            best = min(self.asks, key=lambda x: x.price)
            return best.size
        return None

    @property
    def best_bid(self) -> Optional[Decimal]:
        if self.bids:
            return max(self.bids, key=lambda x: x.price).price
        return None


@dataclass
class MarketMeta:
    """Metadata for a single binary market (one event = UP + DOWN)."""
    condition_id: str
    slug: str             # e.g. "btc-updown-5m-1741000000"
    question: str
    end_date_iso: str
    up_token_id: str
    down_token_id: str
    active: bool = True

    def __repr__(self):
        return f"MarketMeta(slug={self.slug!r})"


@dataclass
class Opportunity:
    """A detected negative-risk arbitrage opportunity."""
    market: MarketMeta
    up_ask: Decimal
    down_ask: Decimal
    up_ask_size: Decimal
    down_ask_size: Decimal
    timestamp: float = field(default_factory=time.time)

    @property
    def total_cost(self) -> Decimal:
        return self.up_ask + self.down_ask

    @property
    def gross_edge(self) -> Decimal:
        """Edge BEFORE fees: 1 - total_cost."""
        return Decimal("1.0") - self.total_cost

    @property
    def net_edge(self) -> Decimal:
        """Edge AFTER fees (2% per leg)."""
        from config import FEE_RATE
        return self.gross_edge - FEE_RATE * 2

    @property
    def max_shares(self) -> Decimal:
        """Max shares limited by available liquidity (equal-shares sizing)."""
        return min(self.up_ask_size, self.down_ask_size)

    @property
    def max_profit(self) -> Decimal:
        """Max dollar profit at max_shares."""
        return self.net_edge * self.max_shares

    def __repr__(self):
        return (
            f"Opportunity(slug={self.market.slug!r}, "
            f"sum={float(self.total_cost):.4f}, "
            f"net_edge={float(self.net_edge):.4f}, "
            f"max_shares={float(self.max_shares):.1f}, "
            f"max_profit=${float(self.max_profit):.4f})"
        )
