"""
Paper trading engine for BTC Up/Down MM strategy.

Fill simulation logic:
  - On each requote cycle we fetch the real CLOB orderbook.
  - Our BID gets filled if our bid price >= real market best_ask (passive cross).
  - Our ASK gets filled if our ask price <= real market best_bid (passive cross).
  - Fill size is min(our order size, available depth at that level).
  - This simulates a realistic maker-fill scenario.

P&L accounting:
  - Each fill is a buy (bid hit) or sell (ask hit) of YES tokens.
  - At market resolution, YES tokens worth $1.00 (if BTC Up) or $0.00 (if BTC Down).
  - We don't know resolution outcome in advance — we track unrealized PnL at mid.
  - Realized PnL comes from round-trips: buy YES at bid, sell YES at ask.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

TAKER_FEE = 0.02  # 2% per fill (polymarket taker fee)
DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros")


@dataclass
class MMFill:
    """A single simulated fill in paper MM."""
    ts: float
    side: str           # "YES" or "NO"
    direction: str      # "BUY" (bid hit) or "SELL" (ask hit)
    price: float        # fill price
    size_usdc: float    # USDC notional
    qty: float          # shares = size_usdc / price
    our_quote: float    # our quoted price at time of fill
    market_price: float # market's opposing best price
    market_question: str
    market_id: str


@dataclass
class MarketResult:
    """Resolution of a market."""
    market_id: str
    outcome: str        # "UP" or "DOWN"
    resolved_at: float


@dataclass
class PaperMMState:
    """State for a single market's paper MM position."""
    market_id: str
    market_question: str
    start_ts: float = field(default_factory=time.time)

    # Position tracking (YES tokens)
    yes_long_qty: float = 0.0     # bought at bid
    yes_long_cost: float = 0.0    # total USDC spent buying
    yes_short_qty: float = 0.0    # sold at ask (short)
    yes_short_proceeds: float = 0.0  # total USDC received selling

    # Round-trip realized PnL (ask_fill - bid_fill on matching pairs)
    realized_pnl: float = 0.0

    # Fill history
    fills: list[MMFill] = field(default_factory=list)

    # Quote tracking
    quote_count: int = 0
    last_bid: float = 0.0
    last_ask: float = 0.0
    last_p_fair: float = 0.5

    # Resolution
    resolved: bool = False
    outcome: Optional[str] = None      # "UP" or "DOWN"
    resolution_pnl: Optional[float] = None

    @property
    def net_yes_qty(self) -> float:
        """Net long YES position (positive = long)."""
        return self.yes_long_qty - self.yes_short_qty

    @property
    def total_buy_cost(self) -> float:
        return self.yes_long_cost

    @property
    def total_sell_proceeds(self) -> float:
        return self.yes_short_proceeds

    def unrealized_pnl(self, yes_mid: float) -> float:
        """
        Unrealized PnL at current mid price.
        Long YES @ avg_cost, currently worth yes_mid each.
        """
        if self.yes_long_qty <= 0:
            return 0.0
        avg_cost = self.yes_long_cost / self.yes_long_qty
        return self.yes_long_qty * (yes_mid - avg_cost)

    def record_fill(self, fill: MMFill) -> None:
        """Record a fill and update position."""
        self.fills.append(fill)
        if fill.direction == "BUY":
            self.yes_long_qty += fill.qty
            self.yes_long_cost += fill.size_usdc * (1 + TAKER_FEE)
        else:  # SELL
            self.yes_short_qty += fill.qty
            self.yes_short_proceeds += fill.size_usdc * (1 - TAKER_FEE)
            # Compute realized PnL if we have a long position to close
            if self.yes_long_qty > 0:
                avg_buy = self.yes_long_cost / self.yes_long_qty
                closed_qty = min(fill.qty, self.yes_long_qty)
                self.realized_pnl += closed_qty * (fill.price - avg_buy) * (1 - TAKER_FEE)

    def resolve(self, outcome: str) -> float:
        """
        Compute final PnL at market resolution.

        Args:
            outcome: "UP" → YES tokens worth $1.00
                     "DOWN" → YES tokens worth $0.00

        Returns realized + resolution PnL.
        """
        self.resolved = True
        self.outcome = outcome
        yes_value = 1.0 if outcome == "UP" else 0.0

        # Net YES position at resolution
        net_qty = self.net_yes_qty
        if net_qty != 0:
            # Long YES: worth yes_value each at resolution
            if net_qty > 0:
                avg_cost = (self.yes_long_cost / self.yes_long_qty) if self.yes_long_qty > 0 else 0
                self.resolution_pnl = net_qty * (yes_value - avg_cost)
            else:
                # Net short — shouldn't happen in pure MM but handle it
                avg_proceeds = (self.yes_short_proceeds / self.yes_short_qty) if self.yes_short_qty > 0 else 0
                self.resolution_pnl = abs(net_qty) * (avg_proceeds - yes_value)
        else:
            self.resolution_pnl = 0.0

        total = self.realized_pnl + self.resolution_pnl
        logger.info(
            f"Market resolved [{outcome}]: {self.market_question[:50]}\n"
            f"  realized={self.realized_pnl:+.4f} resolution={self.resolution_pnl:+.4f} "
            f"total={total:+.4f} USDC | fills={len(self.fills)}"
        )
        return total

    def summary_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "market_question": self.market_question,
            "quote_count": self.quote_count,
            "fill_count": len(self.fills),
            "yes_long_qty": round(self.yes_long_qty, 4),
            "yes_long_cost": round(self.yes_long_cost, 4),
            "yes_short_qty": round(self.yes_short_qty, 4),
            "net_yes_qty": round(self.net_yes_qty, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "resolution_pnl": round(self.resolution_pnl, 4) if self.resolution_pnl is not None else None,
            "outcome": self.outcome,
            "resolved": self.resolved,
        }


class PaperMMEngine:
    """
    Paper trading engine for multiple concurrent MM positions.

    Usage:
        engine = PaperMMEngine()
        # On each requote cycle:
        fills = engine.process_quote(market, bid, ask, p_fair, ob_yes)
        # On market expiry with known outcome:
        pnl = engine.resolve_market(market_id, "UP")
        # Summary:
        engine.print_summary()
    """

    def __init__(
        self,
        order_size_usdc: float = 10.0,
        log_path: Optional[str] = None,
    ):
        self.order_size_usdc = order_size_usdc
        self.log_path = log_path or os.path.join(DRIVE_ROOT, "logs", "paper_mm.jsonl")
        self.states: dict[str, PaperMMState] = {}
        self.total_realized_pnl: float = 0.0
        self.total_resolution_pnl: float = 0.0

    def _get_or_create(self, market) -> PaperMMState:
        if market.id not in self.states:
            self.states[market.id] = PaperMMState(
                market_id=market.id,
                market_question=market.question,
            )
        return self.states[market.id]

    def process_quote(
        self,
        market,
        bid: float,
        ask: float,
        p_fair: float,
        ob_yes,          # Orderbook for YES token (from scanner.get_orderbook)
    ) -> list[MMFill]:
        """
        Simulate fills against the current real orderbook.

        Returns list of fills that occurred this cycle.
        """
        state = self._get_or_create(market)
        state.quote_count += 1
        state.last_bid = bid
        state.last_ask = ask
        state.last_p_fair = p_fair

        fills = []
        market_best_ask = ob_yes.best_ask()
        market_best_bid = ob_yes.best_bid()

        # BID FILL: our bid >= market's best ask → we pay ask, we get YES tokens
        # This happens when market moves against us or we quote too aggressively
        if bid >= market_best_ask and market_best_ask > 0:
            # Available size at market ask
            depth = ob_yes.depth_at(market_best_ask, "ask")
            size_usdc = min(self.order_size_usdc, depth)
            if size_usdc >= 0.5:
                qty = size_usdc / market_best_ask
                fill = MMFill(
                    ts=time.time(),
                    side="YES",
                    direction="BUY",
                    price=market_best_ask,     # fill at market's ask (we're the aggressor)
                    size_usdc=size_usdc,
                    qty=qty,
                    our_quote=bid,
                    market_price=market_best_ask,
                    market_question=market.question,
                    market_id=market.id,
                )
                state.record_fill(fill)
                fills.append(fill)
                logger.info(
                    f"[PAPER BID FILL] {market.question[:50]}\n"
                    f"  BUY YES @ {market_best_ask:.4f} (our_bid={bid:.4f}) "
                    f"size=${size_usdc:.2f} qty={qty:.2f}"
                )

        # ASK FILL: our ask <= market's best bid → someone buys YES from us
        # We sell YES tokens at our ask price
        if ask <= market_best_bid and market_best_bid > 0:
            # Available size at market bid
            depth = ob_yes.depth_at(market_best_bid, "bid")
            size_usdc = min(self.order_size_usdc, depth)
            if size_usdc >= 0.5:
                qty = size_usdc / ask
                fill = MMFill(
                    ts=time.time(),
                    side="YES",
                    direction="SELL",
                    price=ask,                 # fill at our ask
                    size_usdc=size_usdc,
                    qty=qty,
                    our_quote=ask,
                    market_price=market_best_bid,
                    market_question=market.question,
                    market_id=market.id,
                )
                state.record_fill(fill)
                fills.append(fill)
                logger.info(
                    f"[PAPER ASK FILL] {market.question[:50]}\n"
                    f"  SELL YES @ {ask:.4f} (market_bid={market_best_bid:.4f}) "
                    f"size=${size_usdc:.2f} qty={qty:.2f}"
                )

        # Log this quote cycle
        self._log_quote(market, bid, ask, p_fair, market_best_bid, market_best_ask, fills)
        return fills

    def resolve_market(self, market_id: str, outcome: str) -> float:
        """
        Resolve a market with known outcome.

        For BTC Up/Down: outcome = "UP" if BTC price higher at end, else "DOWN".
        In paper mode without real resolution data, we can use the fair price
        at expiry as a proxy: if p_fair > 0.5 at expiry, assume "UP".

        Returns total PnL for this market.
        """
        if market_id not in self.states:
            return 0.0
        state = self.states[market_id]
        if state.resolved:
            return state.realized_pnl + (state.resolution_pnl or 0.0)

        pnl = state.resolve(outcome)
        self.total_realized_pnl += state.realized_pnl
        self.total_resolution_pnl += (state.resolution_pnl or 0.0)
        self._log_resolution(state)
        return pnl

    def get_total_pnl(self) -> float:
        """Sum of all realized + resolution PnL across all markets."""
        total = 0.0
        for s in self.states.values():
            total += s.realized_pnl
            if s.resolution_pnl is not None:
                total += s.resolution_pnl
        return total

    def get_open_pnl(self, market_id: str, yes_mid: float) -> float:
        """Unrealized PnL for an open market at current mid."""
        state = self.states.get(market_id)
        return state.unrealized_pnl(yes_mid) if state else 0.0

    def print_summary(self) -> None:
        """Print full session summary."""
        total_quotes = sum(s.quote_count for s in self.states.values())
        total_fills = sum(len(s.fills) for s in self.states.values())
        total_pnl = self.get_total_pnl()
        resolved = [s for s in self.states.values() if s.resolved]
        open_markets = [s for s in self.states.values() if not s.resolved]

        print("\n" + "=" * 65)
        print("📊 PAPER MM SUMMARY")
        print("=" * 65)
        print(f"Markets traded:    {len(self.states)}")
        print(f"  Resolved:        {len(resolved)}")
        print(f"  Open:            {len(open_markets)}")
        print(f"Total quotes:      {total_quotes}")
        print(f"Total fills:       {total_fills}")
        print(f"Fill rate:         {total_fills/total_quotes*100:.1f}%" if total_quotes else "Fill rate: N/A")
        print(f"Realized P&L:      ${self.total_realized_pnl:+.4f}")
        print(f"Resolution P&L:    ${self.total_resolution_pnl:+.4f}")
        print(f"Total P&L:         ${total_pnl:+.4f}")
        print("=" * 65)

        if self.states:
            print("\nPer-market breakdown:")
            for s in sorted(self.states.values(), key=lambda x: x.start_ts):
                total_m = s.realized_pnl + (s.resolution_pnl or 0.0)
                status = f"[{s.outcome}]" if s.resolved else "[OPEN]"
                print(
                    f"  {status} {s.market_question[:45]}\n"
                    f"    quotes={s.quote_count} fills={len(s.fills)} "
                    f"realized={s.realized_pnl:+.4f} total={total_m:+.4f}"
                )

    def _log_quote(self, market, bid, ask, p_fair, best_bid, best_ask, fills) -> None:
        """Append quote cycle to Drive log."""
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            record = {
                "event": "quote",
                "ts": datetime.now(timezone.utc).isoformat(),
                "market_id": market.id,
                "market_question": market.question[:60],
                "our_bid": round(bid, 4),
                "our_ask": round(ask, 4),
                "p_fair": round(p_fair, 4),
                "market_best_bid": round(best_bid, 4),
                "market_best_ask": round(best_ask, 4),
                "spread": round(ask - bid, 4),
                "fills": len(fills),
            }
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.debug(f"Log write error: {e}")

    def _log_resolution(self, state: PaperMMState) -> None:
        """Log market resolution."""
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            record = {
                "event": "resolution",
                "ts": datetime.now(timezone.utc).isoformat(),
                **state.summary_dict(),
            }
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.debug(f"Log write error: {e}")
