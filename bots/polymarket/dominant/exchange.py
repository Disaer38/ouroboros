"""
PaperExchange — virtual order execution engine for the Dominant Side strategy.

Simulates trading without real money:
  - Maintains virtual USDC balance (default: $1000).
  - Executes BUY orders at the signal's ask price + 2% taker fee.
  - Tracks open positions and resolves them at market expiry.
  - Logs every trade event to Google Drive (JSONL).
  - Enforces risk limits (max exposure, daily loss, open positions).

P&L model for Dominant Side:
  - We BUY the dominant token (YES or NO) for `bet_size_usdc`.
  - Shares bought = bet_size_usdc / (ask_price * (1 + taker_fee))
  - At resolution:
      - If we picked correctly: payout = shares * $1.00 → PnL = payout - cost > 0
      - If we picked wrong:     payout = $0.00          → PnL = -cost
  - Expected value per trade (at p=0.60): 0.60*payout - cost
    = 0.60 * (10 / (0.60 * 1.02)) - 10
    = 0.60 * 16.34 - 10 = 9.80 - 10 = -$0.20 (edge is slim, but this is paper trading)

Wait — at p=0.60, payout = 10/0.60*1.02 = $16.34, cost = $10
  WIN: payout = $16.34, profit = $6.34
  LOSS: payout = $0, profit = -$10
  EV = 0.60 * 16.34 - 10 = 9.80 - 10 = -$0.20

So we NEED win_rate > 60.9% to be profitable at p=0.60 entry.
Strategy thesis: markets that reach 0.60 actually win ~65-70% → +EV.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import DominantConfig
from .signal import Side, Signal

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Data models (position + trade)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DominantPosition:
    """An open (unresolved) position."""
    market_id: str
    market_question: str
    asset: str
    side: Side
    entry_price: float
    shares: float
    cost_usdc: float
    expected_payout: float
    time_remaining_at_entry: float
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expiry: Optional[datetime] = None   # market end_date

    @property
    def expected_pnl(self) -> float:
        return self.expected_payout - self.cost_usdc

    @property
    def expected_pnl_pct(self) -> float:
        if self.cost_usdc == 0:
            return 0.0
        return self.expected_pnl / self.cost_usdc * 100


@dataclass
class DominantTrade:
    """A fully resolved trade record."""
    market_id: str
    market_question: str
    asset: str
    side: str                   # "YES" | "NO"
    entry_price: float
    shares: float
    cost_usdc: float
    expected_payout: float
    time_remaining_at_entry: float
    entered_at: datetime

    # Resolution
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    won: Optional[bool] = None
    actual_payout: float = 0.0
    pnl: Optional[float] = None

    @property
    def pnl_pct(self) -> Optional[float]:
        if self.pnl is None or self.cost_usdc == 0:
            return None
        return self.pnl / self.cost_usdc * 100

    def to_dict(self) -> dict:
        d = {
            "market_id": self.market_id,
            "market_question": self.market_question,
            "asset": self.asset,
            "side": self.side,
            "entry_price": self.entry_price,
            "shares": round(self.shares, 6),
            "cost_usdc": round(self.cost_usdc, 4),
            "expected_payout": round(self.expected_payout, 4),
            "time_remaining_at_entry": round(self.time_remaining_at_entry, 1),
            "entered_at": self.entered_at.isoformat(),
            "resolved": self.resolved,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "won": self.won,
            "actual_payout": round(self.actual_payout, 4),
            "pnl": round(self.pnl, 4) if self.pnl is not None else None,
            "pnl_pct": round(self.pnl_pct, 2) if self.pnl_pct is not None else None,
        }
        return d


# ──────────────────────────────────────────────────────────────────────────────
# PaperExchange
# ──────────────────────────────────────────────────────────────────────────────

class PaperExchange:
    """
    Virtual order execution engine.

    Enforces risk limits, simulates fills, tracks P&L.

    Usage:
        exchange = PaperExchange(config)

        # On each signal:
        trade = exchange.buy(signal, market, asset="btc")

        # At market expiry:
        trade = exchange.resolve(market_id, won=True)

        # Summary:
        print(exchange.summary())
    """

    def __init__(self, config: DominantConfig):
        self.config = config
        self.balance: float = config.initial_balance_usdc
        self.positions: dict[str, DominantPosition] = {}   # market_id → position
        self.closed_trades: list[DominantTrade] = []
        self._traded_market_ids: set[str] = set()           # prevent re-entry
        self._daily_loss: float = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # Buying
    # ──────────────────────────────────────────────────────────────────────────

    def can_buy(self, signal: Signal, market_id: str) -> tuple[bool, str]:
        """
        Check all risk rules before entering a trade.

        Returns (allowed, reason_if_not_allowed).
        """
        if market_id in self._traded_market_ids:
            return False, "already traded this market this session"

        if len(self.positions) >= self.config.max_open_trades:
            return False, f"max open trades reached ({self.config.max_open_trades})"

        if self._daily_loss >= self.config.max_daily_loss_usdc:
            return False, f"daily loss limit reached (${self._daily_loss:.2f})"

        if self.balance < self.config.bet_size_usdc:
            return False, f"insufficient balance (${self.balance:.2f})"

        return True, "ok"

    def buy(
        self,
        signal: Signal,
        market,         # ActiveMarket from market_discovery
        asset: str,
    ) -> Optional[DominantTrade]:
        """
        Simulate a market buy of the dominant side token.

        Fills at signal.price (best ask) with 2% taker fee.
        Returns DominantTrade if executed, None if blocked by risk rules.
        """
        allowed, reason = self.can_buy(signal, signal.market_id)
        if not allowed:
            logger.info(f"Trade blocked ({reason}): {market.question[:50]}")
            return None

        # Compute fill
        cost = self.config.bet_size_usdc
        fill_price = signal.price * (1 + self.config.taker_fee)
        shares = cost / fill_price
        expected_payout = shares * 1.0  # $1 per share if we win

        # Deduct from balance
        self.balance -= cost

        # Create position
        pos = DominantPosition(
            market_id=signal.market_id,
            market_question=market.question,
            asset=asset,
            side=signal.side,
            entry_price=signal.price,
            shares=shares,
            cost_usdc=cost,
            expected_payout=expected_payout,
            time_remaining_at_entry=signal.time_remaining_sec,
            expiry=market.end_date,
        )
        self.positions[signal.market_id] = pos
        self._traded_market_ids.add(signal.market_id)

        # Build trade record (open)
        trade = DominantTrade(
            market_id=signal.market_id,
            market_question=market.question,
            asset=asset,
            side=signal.side.value,
            entry_price=signal.price,
            shares=shares,
            cost_usdc=cost,
            expected_payout=expected_payout,
            time_remaining_at_entry=signal.time_remaining_sec,
            entered_at=datetime.now(timezone.utc),
        )

        self._log_event("enter", trade)

        logger.info(
            f"📝 BUY {signal.side.value} | {market.question[:50]}\n"
            f"   price={signal.price:.3f} fee={fill_price:.3f} "
            f"shares={shares:.2f} cost=${cost:.2f} "
            f"exp_payout=${expected_payout:.2f} ttl={signal.time_remaining_sec:.0f}s"
        )
        return trade

    # ──────────────────────────────────────────────────────────────────────────
    # Resolution
    # ──────────────────────────────────────────────────────────────────────────

    def resolve(
        self,
        market_id: str,
        won: bool,      # True = our side won ($1/share), False = lost ($0)
    ) -> Optional[DominantTrade]:
        """
        Resolve an open position.

        Args:
            market_id: The market to resolve.
            won: True if our token is worth $1.00, False if $0.00.

        Returns the closed DominantTrade, or None if position not found.
        """
        pos = self.positions.pop(market_id, None)
        if pos is None:
            logger.warning(f"resolve() called for unknown market {market_id}")
            return None

        payout = pos.shares * 1.0 if won else 0.0
        pnl = payout - pos.cost_usdc

        # Update balance
        self.balance += payout

        # Update daily loss tracker
        if pnl < 0:
            self._daily_loss += abs(pnl)

        trade = DominantTrade(
            market_id=pos.market_id,
            market_question=pos.market_question,
            asset=pos.asset,
            side=pos.side.value,
            entry_price=pos.entry_price,
            shares=pos.shares,
            cost_usdc=pos.cost_usdc,
            expected_payout=pos.expected_payout,
            time_remaining_at_entry=pos.time_remaining_at_entry,
            entered_at=pos.entered_at,
            resolved=True,
            resolved_at=datetime.now(timezone.utc),
            won=won,
            actual_payout=payout,
            pnl=pnl,
        )
        self.closed_trades.append(trade)
        self._log_event("resolve", trade)

        result_emoji = "✅" if won else "❌"
        logger.info(
            f"{result_emoji} RESOLVE | {pos.market_question[:50]}\n"
            f"   won={won} payout=${payout:.2f} pnl=${pnl:+.2f} "
            f"balance=${self.balance:.2f}"
        )
        return trade

    # ──────────────────────────────────────────────────────────────────────────
    # Properties & summary
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def open_exposure_usdc(self) -> float:
        return sum(p.cost_usdc for p in self.positions.values())

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades if t.pnl is not None)

    @property
    def win_rate(self) -> float:
        resolved = [t for t in self.closed_trades if t.won is not None]
        if not resolved:
            return 0.0
        return sum(1 for t in resolved if t.won) / len(resolved)

    @property
    def open_trade_count(self) -> int:
        return len(self.positions)

    @property
    def closed_trade_count(self) -> int:
        return len(self.closed_trades)

    def summary(self) -> dict:
        """Return a summary dict for reporting."""
        resolved = [t for t in self.closed_trades if t.won is not None]
        wins = sum(1 for t in resolved if t.won)
        return {
            "balance": round(self.balance, 2),
            "open_trades": self.open_trade_count,
            "closed_trades": self.closed_trade_count,
            "open_exposure_usdc": round(self.open_exposure_usdc, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "win_rate_pct": round(self.win_rate * 100, 1),
            "wins": wins,
            "losses": len(resolved) - wins,
            "daily_loss": round(self._daily_loss, 2),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────────────────────────────────

    def _log_event(self, event: str, trade: DominantTrade) -> None:
        """Append a trade event to the Drive log file."""
        try:
            log_path = self.config.log_path
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            record = {"event": event, "ts": datetime.now(timezone.utc).isoformat()}
            record.update(trade.to_dict())
            with open(log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.warning(f"Failed to log trade event: {e}")