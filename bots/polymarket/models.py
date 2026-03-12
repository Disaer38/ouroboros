"""
Data models for the Polymarket arbitrage bot.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Market:
    id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_date: datetime
    active: bool
    min_tick: float = 0.001
    min_size: float = 5.0


@dataclass
class Orderbook:
    token_id: str
    bids: list      # list of (price: float, size: float)
    asks: list      # list of (price: float, size: float)
    timestamp: datetime = field(default_factory=_now)

    @property
    def best_bid(self) -> float:
        return max((p for p, _ in self.bids), default=0.0)

    @property
    def best_ask(self) -> float:
        return min((p for p, _ in self.asks), default=1.0)

    def depth_at(self, price: float, side: str) -> float:
        """Total USDC depth available at or better than price."""
        if side == "ask":
            return sum(p * s for p, s in self.asks if p <= price)
        return sum(p * s for p, s in self.bids if p >= price)


@dataclass
class ArbOpportunity:
    market: Market
    strategy: str           # "neg_risk"
    yes_ask: float
    no_ask: float
    total_cost: float       # yes_ask + no_ask (cost per share before fees)
    profit_pct: float       # gross margin: (1.0 - total_cost) * 100
    yes_depth: float        # USDC available on YES side at yes_ask
    no_depth: float         # USDC available on NO side at no_ask
    max_size_usdc: float    # tradeable size: min(yes_depth, no_depth)
    detected_at: datetime = field(default_factory=_now)

    def __str__(self) -> str:
        return (
            f"[{self.strategy}] {self.market.question[:40]} | "
            f"YES={self.yes_ask:.3f} NO={self.no_ask:.3f} "
            f"total={self.total_cost:.3f} profit={self.profit_pct:.2f}%"
        )


@dataclass
class PaperTrade:
    """A simulated trade for paper trading mode."""
    market_id: str
    market_question: str
    yes_ask: float
    no_ask: float
    shares: float
    cost: float                  # total USDC paid (both sides + fees)
    expected_payout: float       # shares × $1.00
    expected_profit: float       # expected_payout - cost
    expected_pnl_pct: float      # expected_profit / cost × 100
    entered_at: datetime = field(default_factory=_now)

    # Resolution fields (set when market resolves)
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    actual_payout: Optional[float] = None
    actual_profit: Optional[float] = None
    actual_pnl_pct: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "market_question": self.market_question,
            "yes_ask": self.yes_ask,
            "no_ask": self.no_ask,
            "shares": self.shares,
            "cost": self.cost,
            "expected_payout": self.expected_payout,
            "expected_profit": self.expected_profit,
            "expected_pnl_pct": self.expected_pnl_pct,
            "entered_at": self.entered_at.isoformat(),
            "resolved": self.resolved,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "actual_payout": self.actual_payout,
            "actual_profit": self.actual_profit,
            "actual_pnl_pct": self.actual_pnl_pct,
        }
