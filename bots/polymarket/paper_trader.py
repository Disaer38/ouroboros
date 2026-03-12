"""
Paper trading engine for Polymarket neg-risk arbitrage.

Simulates trade execution and tracks P&L without real money.
All trades are logged to Google Drive for review.

P&L accounting:
  - cost = (yes_ask + no_ask) * size * (1 + fee_rate)^2
    (taker fee applied to each side independently)
  - payout = size * 1.0  (one side always wins $1/share)
  - profit = payout - cost
  - pnl_pct = profit / cost * 100
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .models import ArbOpportunity, PaperTrade

logger = logging.getLogger(__name__)

# Polymarket taker fee per side
TAKER_FEE = 0.02


class PaperTrader:
    """
    Simulates neg-risk arbitrage trades.

    Tracks:
    - Total USDC deployed (open exposure)
    - Realized P&L (from resolved trades)
    - All trades log (Drive jsonl)
    """

    def __init__(
        self,
        max_position_usdc: float = 10.0,
        max_exposure_usdc: float = 50.0,
        log_path: Optional[str] = None,
    ):
        self.max_position = max_position_usdc
        self.max_exposure = max_exposure_usdc
        self.log_path = log_path or self._default_log_path()

        self.trades: list[PaperTrade] = []
        self.realized_pnl: float = 0.0
        self.total_invested: float = 0.0

        # Prevent trading the same market twice per session
        self._traded_market_ids: set[str] = set()

        logger.info(
            f"PaperTrader initialized "
            f"(max_position={max_position_usdc}, max_exposure={max_exposure_usdc})"
        )

    @staticmethod
    def _default_log_path() -> str:
        drive = os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros")
        return os.path.join(drive, "logs", "paper_trades.jsonl")

    @property
    def open_exposure(self) -> float:
        """Total USDC in unresolved trades."""
        return sum(t.cost for t in self.trades if not t.resolved)

    @property
    def open_trade_count(self) -> int:
        return sum(1 for t in self.trades if not t.resolved)

    def can_trade(self, opportunity: ArbOpportunity) -> tuple[bool, str]:
        """
        Check if we can enter this trade within risk limits.

        Returns (allowed, reason).
        """
        market_id = opportunity.market.id

        if market_id in self._traded_market_ids:
            return False, "already traded this market this session"

        if self.open_exposure >= self.max_exposure:
            return False, f"max exposure reached (${self.max_exposure:.0f})"

        return True, "ok"

    def enter_trade(self, opportunity: ArbOpportunity) -> Optional[PaperTrade]:
        """
        Simulate entering a neg-risk arbitrage trade.

        Position size is capped at:
        - max_position per trade
        - available depth on both sides
        - remaining exposure headroom
        """
        allowed, reason = self.can_trade(opportunity)
        if not allowed:
            logger.info(f"Trade skipped ({reason}): {opportunity.market.question[:50]}")
            return None

        # Size determination
        headroom = self.max_exposure - self.open_exposure
        raw_size = min(
            self.max_position,
            headroom,
            opportunity.max_size_usdc,
        )
        if raw_size < 0.5:
            logger.info(f"Trade too small (size={raw_size:.2f}): skipping")
            return None

        # Size is in USDC. Shares = size / total_cost
        # e.g., $5 invested at YES=0.48, NO=0.48 → ~5.21 shares (5/0.96)
        shares = raw_size / opportunity.total_cost

        # Apply taker fee to each side
        yes_cost = opportunity.yes_ask * shares * (1 + TAKER_FEE)
        no_cost = opportunity.no_ask * shares * (1 + TAKER_FEE)
        total_cost = yes_cost + no_cost

        # Expected payout: shares × $1 (one side always wins)
        payout = shares * 1.0
        expected_profit = payout - total_cost
        expected_pnl_pct = expected_profit / total_cost * 100

        trade = PaperTrade(
            market_id=opportunity.market.id,
            market_question=opportunity.market.question,
            yes_ask=opportunity.yes_ask,
            no_ask=opportunity.no_ask,
            shares=shares,
            cost=total_cost,
            expected_payout=payout,
            expected_profit=expected_profit,
            expected_pnl_pct=expected_pnl_pct,
            entered_at=datetime.now(timezone.utc),
        )

        self.trades.append(trade)
        self._traded_market_ids.add(opportunity.market.id)
        self.total_invested += total_cost
        self._log_trade(trade, "enter")

        logger.info(
            f"📝 Paper trade entered: {opportunity.market.question[:50]}\n"
            f"   YES={opportunity.yes_ask:.3f} + NO={opportunity.no_ask:.3f} = "
            f"{opportunity.total_cost:.3f} | {shares:.2f} shares | "
            f"cost=${total_cost:.2f} | expected profit={expected_pnl_pct:.1f}%"
        )
        return trade

    def resolve_trade(self, trade: PaperTrade, won: bool) -> None:
        """
        Mark a trade as resolved (market has ended).

        Args:
            won: True if trade was profitable (one side paid out $1/share),
                 False if somehow both sides lost (shouldn't happen with neg-risk,
                 but useful for testing).
        """
        if trade.resolved:
            return

        trade.resolved = True
        trade.resolved_at = datetime.now(timezone.utc)
        trade.actual_payout = trade.expected_payout if won else 0.0
        trade.actual_profit = trade.actual_payout - trade.cost
        trade.actual_pnl_pct = trade.actual_profit / trade.cost * 100

        self.realized_pnl += trade.actual_profit
        self._log_trade(trade, "resolve")

        outcome = "✅ profit" if trade.actual_profit > 0 else "❌ loss"
        logger.info(
            f"Trade resolved ({outcome}): {trade.market_question[:50]}\n"
            f"   payout=${trade.actual_payout:.2f} cost=${trade.cost:.2f} "
            f"profit=${trade.actual_profit:.2f} ({trade.actual_pnl_pct:.1f}%)"
        )

    def print_summary(self) -> None:
        """Print a summary of all paper trades."""
        open_trades = [t for t in self.trades if not t.resolved]
        closed_trades = [t for t in self.trades if t.resolved]

        print("\n" + "=" * 60)
        print("📊 PAPER TRADING SUMMARY")
        print("=" * 60)
        print(f"Total trades entered:   {len(self.trades)}")
        print(f"Open (unresolved):      {len(open_trades)}")
        print(f"Closed (resolved):      {len(closed_trades)}")
        print(f"Total invested (ever):  ${self.total_invested:.2f}")
        print(f"Current open exposure:  ${self.open_exposure:.2f}")
        print(f"Realized P&L:           ${self.realized_pnl:.2f}")

        if open_trades:
            exp_profit = sum(t.expected_profit for t in open_trades)
            print(f"Unrealized expected:    ${exp_profit:.2f}")

        if closed_trades:
            win_count = sum(1 for t in closed_trades if (t.actual_profit or 0) > 0)
            print(f"Win rate:               {win_count}/{len(closed_trades)}")

        print("=" * 60)

        if self.trades:
            print("\nRecent trades:")
            for t in self.trades[-5:]:
                status = "OPEN" if not t.resolved else (
                    f"{'WIN' if (t.actual_profit or 0) > 0 else 'LOSS'} "
                    f"${t.actual_profit:.2f}"
                )
                print(
                    f"  {t.entered_at.strftime('%H:%M:%S')} "
                    f"[{status}] {t.market_question[:40]}\n"
                    f"    cost=${t.cost:.2f} expected={t.expected_pnl_pct:.1f}%"
                )

    def _log_trade(self, trade: PaperTrade, event: str) -> None:
        """Append trade to Drive log."""
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            record = {
                "event": event,
                "ts": datetime.now(timezone.utc).isoformat(),
                "market_id": trade.market_id,
                "market_question": trade.market_question,
                "yes_ask": trade.yes_ask,
                "no_ask": trade.no_ask,
                "shares": trade.shares,
                "cost": trade.cost,
                "expected_profit": trade.expected_profit,
                "expected_pnl_pct": trade.expected_pnl_pct,
                "resolved": trade.resolved,
                "actual_pnl_pct": trade.actual_pnl_pct,
            }
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.warning(f"Failed to log trade: {e}")
