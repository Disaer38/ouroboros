"""
Paper trading engine for Polymarket neg-risk arbitrage.

Simulates trade execution and tracks P&L without real money.
All trades are logged to Google Drive for review.

P&L accounting for neg-risk strategy:
  - You buy YES + NO tokens for the same market
  - Exactly one side always pays $1/share at resolution
  - Total payout = shares × $1.00  (guaranteed, regardless of outcome)
  - cost = shares × (yes_ask × (1+fee) + no_ask × (1+fee))
  - profit = payout - cost
  - pnl_pct = profit / cost × 100

Taker fee (2%) is applied independently to each side's fill.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .models import ArbOpportunity, PaperTrade

logger = logging.getLogger(__name__)

# Polymarket taker fee per side (2%)
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

        # raw_size is USDC to deploy. Shares = raw_size / total_cost_per_share
        # where total_cost_per_share = yes_ask + no_ask (before fees)
        shares = raw_size / opportunity.total_cost

        # Taker fee applied to each side independently
        yes_cost = opportunity.yes_ask * shares * (1 + TAKER_FEE)
        no_cost = opportunity.no_ask * shares * (1 + TAKER_FEE)
        total_cost = yes_cost + no_cost

        # Neg-risk payout: exactly one side pays $1/share regardless of outcome
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

    def resolve_trade(self, trade: PaperTrade, won: bool = True) -> None:
        """
        Mark a trade as resolved.

        For neg-risk strategy: won=True means one side paid $1/share (normal outcome).
        This should ALWAYS be True for neg-risk — both YES and NO are held,
        so one side always wins. The `won` parameter exists for edge cases
        (e.g., market cancelled/resolved invalid → $0 payout).

        Args:
            won: True (default) → payout = shares × $1.00 (standard neg-risk outcome)
                 False → payout = $0 (only for cancelled/invalid markets)
        """
        if trade.resolved:
            return

        trade.resolved = True
        trade.resolved_at = datetime.now(timezone.utc)

        # Neg-risk: one side ALWAYS wins → payout = shares × $1.00
        # The 'won' flag handles the rare case of a cancelled/invalid market
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


class DualSideMMStrategy:
    """
    Dual-side market making strategy inspired by vague-sourdough.

    Core logic:
    - Scan 5-min BTC/ETH/SOL/XRP markets every 30 seconds
    - If Up_ask + Down_ask < 0.97 (>3% spread): buy both sides
    - Ladder into position with 3-5 orders per side
    - Let market resolve naturally (5 min)
    - Track realized PnL per market pair
    """

    def __init__(self, min_spread_pct: float = 0.03, max_position_usdc: float = 20.0):
        self.min_spread_pct = min_spread_pct  # Minimum spread to enter
        self.max_position_usdc = max_position_usdc  # Max USDC per market pair
        self.active_pairs: dict = {}  # conditionId -> {up_cost, down_cost, entered_at}
        self.completed_pairs: list = []  # resolved pairs with PnL

    def check_spread(self, up_ask: float, down_ask: float) -> tuple[bool, float]:
        """
        Check if there's an arbitrage opportunity.
        Returns (has_opportunity, spread_pct)
        """
        total_cost = up_ask + down_ask
        spread_pct = 1.0 - total_cost
        return spread_pct >= self.min_spread_pct, spread_pct

    def calculate_entry(self, up_ask: float, down_ask: float,
                        budget_usdc: float) -> dict:
        """
        Calculate how much to buy on each side.
        Proportional to probability (buy more of the cheaper/uncertain side).
        """
        total = up_ask + down_ask
        # Split budget proportionally — but weight toward cheaper side for bigger gain
        down_weight = (1 - up_ask) / total  # more weight to uncertain outcome
        up_weight = 1 - down_weight

        return {
            "up_usdc": round(budget_usdc * up_weight, 2),
            "down_usdc": round(budget_usdc * down_weight, 2),
            "expected_pnl": round((1.0 - total) * budget_usdc, 2),
            "spread_pct": round((1.0 - total) * 100, 2),
        }

    def record_pair_entry(self, condition_id: str, up_usdc: float,
                          down_usdc: float, market_title: str):
        """Record that we entered a dual-side position."""
        self.active_pairs[condition_id] = {
            "market_title": market_title,
            "up_cost": up_usdc,
            "down_cost": down_usdc,
            "total_cost": up_usdc + down_usdc,
            "entered_at": datetime.now(timezone.utc).isoformat(),
        }

    def resolve_pair(self, condition_id: str, winning_side: str,
                     winning_tokens: float) -> float:
        """
        Resolve a market pair. Returns realized PnL.
        winning_side: 'Up' or 'Down'
        winning_tokens: how many tokens we hold on winning side
        """
        if condition_id not in self.active_pairs:
            return 0.0

        pair = self.active_pairs.pop(condition_id)
        payout = winning_tokens  # each token pays $1
        cost = pair["total_cost"]
        pnl = payout - cost

        self.completed_pairs.append({
            **pair,
            "winning_side": winning_side,
            "payout": payout,
            "pnl": round(pnl, 4),
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        })
        return pnl

    def get_stats(self) -> dict:
        """Get strategy performance statistics."""
        if not self.completed_pairs:
            return {"completed": 0, "total_pnl": 0, "win_rate": 0}

        total_pnl = sum(p["pnl"] for p in self.completed_pairs)
        winning = sum(1 for p in self.completed_pairs if p["pnl"] > 0)

        return {
            "completed": len(self.completed_pairs),
            "active": len(self.active_pairs),
            "total_pnl": round(total_pnl, 4),
            "win_rate": round(winning / len(self.completed_pairs) * 100, 1),
            "avg_pnl_per_trade": round(total_pnl / len(self.completed_pairs), 4),
        }
