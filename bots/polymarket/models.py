"""
Data models for the Polymarket arbitrage bot.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


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
    bids: list  # list of (price: float, size: float)
    asks: list  # list of (price: float, size: float)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def best_bid(self) -> float:
        return max((p for p, _ in self.bids), default=0.0)

    def best_ask(self) -> float:
        return min((p for p, _ in self.asks), default=1.0)

    def depth_at(self, price: float, side: str) -> float:
        """Total size available at or better than price."""
        if side == "ask":
            return sum(s for p, s in self.asks if p <= price)
        return sum(s for p, s in self.bids if p >= price)


@dataclass
class ArbOpportunity:
    market: Market
    strategy: str          # "neg_risk"
    yes_ask: float
    no_ask: float
    total_cost: float      # yes_ask + no_ask
    profit_pct: float      # (1.0 - total_cost) * 100
    yes_depth: float       # available USDC at yes_ask
    no_depth: float        # available USDC at no_ask
    max_size_usdc: float   # min(yes_depth, no_depth) capped at max position
    detected_at: datetime = field(default_factory=datetime.utcnow)

    def __str__(self) -> str:
        return (
            f"[{self.strategy}] {self.market.question[:40]} | "
            f"YES={self.yes_ask:.3f} NO={self.no_ask:.3f} "
            f"total={self.total_cost:.3f} profit={self.profit_pct:.2f}%"
        )


@dataclass
class PaperTrade:
    id: str
    market_id: str
    market_question: str
    market_end_date: datetime
    strategy: str
    size_usdc: float
    yes_fill_price: float
    no_fill_price: float
    actual_cost: float     # yes + no per share
    shares: float          # size_usdc / actual_cost
    expected_payout: float = 1.0
    executed_at: datetime = field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None
    pnl: Optional[float] = None   # USDC
    status: str = "open"          # open, resolved, expired

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "market_id": self.market_id,
            "market_question": self.market_question,
            "market_end_date": self.market_end_date.isoformat(),
            "strategy": self.strategy,
            "size_usdc": self.size_usdc,
            "yes_fill_price": self.yes_fill_price,
            "no_fill_price": self.no_fill_price,
            "actual_cost": self.actual_cost,
            "shares": self.shares,
            "expected_payout": self.expected_payout,
            "executed_at": self.executed_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "pnl": self.pnl,
            "status": self.status,
        }
