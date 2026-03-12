"""
Paper trading engine.
Simulates trades without executing on-chain. Tracks P&L over time.

All trades and opportunities are logged to Google Drive JSONL files.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import ArbOpportunity, PaperTrade

logger = logging.getLogger(__name__)

DRIVE_ROOT = "/content/drive/MyDrive/Ouroboros/logs"
PAPER_TRADES_LOG = f"{DRIVE_ROOT}/paper_trades.jsonl"
OPPORTUNITIES_LOG = f"{DRIVE_ROOT}/opportunities.jsonl"


def _append_jsonl(path: str, record: dict) -> None:
    """Append a JSON record to a JSONL file. Creates file if missing."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.error(f"Failed to write {path}: {e}")


class PaperTrader:
    """
    Paper trading engine for Polymarket neg-risk arb.

    Tracks open trades in memory, resolves them when market expires,
    and logs everything to Drive.
    """

    def __init__(
        self,
        max_position_usdc: float = 10.0,
        trade_log_path: str = PAPER_TRADES_LOG,
        opp_log_path: str = OPPORTUNITIES_LOG,
    ):
        self.max_position_usdc = max_position_usdc
        self.trade_log_path = trade_log_path
        self.opp_log_path = opp_log_path
        self.open_trades: list[PaperTrade] = []
        self.closed_trades: list[PaperTrade] = []
        self.total_opportunities = 0

    def log_opportunity(self, opp: ArbOpportunity) -> None:
        """Log a discovered opportunity (regardless of whether we trade)."""
        self.total_opportunities += 1
        record = {
            "ts": opp.detected_at.isoformat(),
            "market_id": opp.market.id,
            "question": opp.market.question,
            "strategy": opp.strategy,
            "yes_ask": opp.yes_ask,
            "no_ask": opp.no_ask,
            "total_cost": opp.total_cost,
            "profit_pct": opp.profit_pct,
            "max_size_usdc": opp.max_size_usdc,
        }
        _append_jsonl(self.opp_log_path, record)

    def simulate_trade(
        self,
        opp: ArbOpportunity,
        size_usdc: Optional[float] = None,
    ) -> Optional[PaperTrade]:
        """
        Simulate entering a neg-risk arb position.

        size_usdc: how much USDC to deploy. Capped at max_position_usdc.
        Returns PaperTrade or None if opportunity is too small.
        """
        if size_usdc is None:
            size_usdc = min(self.max_position_usdc, opp.max_size_usdc)

        if size_usdc < 1.0:
            logger.debug(f"Trade too small ({size_usdc:.2f} USDC), skipping")
            return None

        actual_cost = opp.total_cost  # already includes fee
        shares = size_usdc / actual_cost

        trade = PaperTrade(
            id=str(uuid.uuid4())[:8],
            market_id=opp.market.id,
            market_question=opp.market.question,
            market_end_date=opp.market.end_date,
            strategy=opp.strategy,
            size_usdc=size_usdc,
            yes_fill_price=opp.yes_ask,
            no_fill_price=opp.no_ask,
            actual_cost=actual_cost,
            shares=shares,
            expected_payout=1.0,
        )
        self.open_trades.append(trade)
        _append_jsonl(self.trade_log_path, {**trade.to_dict(), "event": "opened"})
        logger.info(
            f"PAPER TRADE {trade.id}: {trade.market_question[:40]} "
            f"size={size_usdc:.2f} USDC shares={shares:.3f} "
            f"expected_pnl={((1.0 - actual_cost) * shares):.4f} USDC"
        )
        return trade

    def check_resolutions(self) -> list[PaperTrade]:
        """
        Resolve trades whose markets have expired.

        Neg-risk logic: payout = 1.0 per share (one leg wins $1, one loses $0).
        PnL = (1.0 - actual_cost) * shares
        """
        now = datetime.now(timezone.utc)
        resolved = []

        still_open = []
        for trade in self.open_trades:
            end = trade.market_end_date
            # Ensure timezone-aware comparison
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)

            if now >= end:
                pnl_per_share = trade.expected_payout - trade.actual_cost
                trade.pnl = round(pnl_per_share * trade.shares, 4)
                trade.resolved_at = now
                trade.status = "resolved"
                self.closed_trades.append(trade)
                resolved.append(trade)
                _append_jsonl(self.trade_log_path, {**trade.to_dict(), "event": "resolved"})
                logger.info(
                    f"RESOLVED {trade.id}: pnl={trade.pnl:+.4f} USDC "
                    f"({'WIN' if trade.pnl > 0 else 'LOSS'})"
                )
            else:
                still_open.append(trade)

        self.open_trades = still_open
        return resolved

    def get_stats(self) -> dict:
        """Return summary statistics."""
        all_closed = self.closed_trades
        total_pnl = sum(t.pnl for t in all_closed if t.pnl is not None)
        wins = sum(1 for t in all_closed if t.pnl is not None and t.pnl > 0)
        win_rate = wins / len(all_closed) if all_closed else 0.0

        total_invested = sum(t.size_usdc for t in all_closed)
        roi_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0

        return {
            "total_opportunities": self.total_opportunities,
            "total_trades": len(all_closed) + len(self.open_trades),
            "open_trades": len(self.open_trades),
            "closed_trades": len(all_closed),
            "total_pnl_usdc": round(total_pnl, 4),
            "win_rate_pct": round(win_rate * 100, 1),
            "roi_pct": round(roi_pct, 2),
            "total_invested_usdc": round(total_invested, 2),
        }
