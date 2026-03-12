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

    # Position tracking (NO tokens)
    no_long_qty: float = 0.0
    no_long_cost: float = 0.0
    no_short_qty: float = 0.0
    no_short_proceeds: float = 0.0

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
    def net_no_qty(self) -> float:
        """Net long NO position."""
        return self.no_long_qty - self.no_short_qty

    @property
    def total_buy_cost(self) -> float:
        return self.yes_long_cost

    @property
    def total_sell_proceeds(self) -> float:
        return self.yes_short_proceeds

    def unrealized_pnl(self, yes_mid: float, no_mid: float = 0.5) -> float:
        """Unrealized PnL at current mid prices."""
        unrealized = 0.0
        if self.yes_long_qty > 0:
            avg_cost = self.yes_long_cost / self.yes_long_qty
            unrealized += self.yes_long_qty * (yes_mid - avg_cost)
        if self.no_long_qty > 0:
            avg_cost = self.no_long_cost / self.no_long_qty
            unrealized += self.no_long_qty * (no_mid - avg_cost)
        return unrealized

    def record_fill(self, fill: MMFill) -> None:
        """Record a fill and update position."""
        self.fills.append(fill)
        if fill.side == "YES":
            if fill.direction == "BUY":
                self.yes_long_qty += fill.qty
                self.yes_long_cost += fill.size_usdc * (1 + TAKER_FEE)
            else:  # SELL
                self.yes_short_qty += fill.qty
                self.yes_short_proceeds += fill.size_usdc * (1 - TAKER_FEE)
                if self.yes_long_qty > 0:
                    avg_buy = self.yes_long_cost / self.yes_long_qty
                    closed_qty = min(fill.qty, self.yes_long_qty)
                    self.realized_pnl += closed_qty * (fill.price - avg_buy) * (1 - TAKER_FEE)
        elif fill.side == "NO":
            if fill.direction == "BUY":
                self.no_long_qty += fill.qty
                self.no_long_cost += fill.size_usdc * (1 + TAKER_FEE)
            else:  # SELL
                self.no_short_qty += fill.qty
                self.no_short_proceeds += fill.size_usdc * (1 - TAKER_FEE)
                if self.no_long_qty > 0:
                    avg_buy = self.no_long_cost / self.no_long_qty
                    closed_qty = min(fill.qty, self.no_long_qty)
                    self.realized_pnl += closed_qty * (fill.price - avg_buy) * (1 - TAKER_FEE)

    def resolve(self, outcome: str) -> float:
        """
        Compute final PnL at market resolution.

        Args:
            outcome: "UP"   → YES tokens worth $1.00, NO tokens worth $0.00
                     "DOWN" → YES tokens worth $0.00, NO tokens worth $1.00

        Returns realized + resolution PnL.
        """
        self.resolved = True
        self.outcome = outcome
        yes_value = 1.0 if outcome == "UP" else 0.0
        no_value = 1.0 if outcome == "DOWN" else 0.0

        res_yes = 0.0
        if self.yes_long_qty > 0:
            avg_cost = self.yes_long_cost / self.yes_long_qty
            res_yes = self.yes_long_qty * (yes_value - avg_cost)

        res_no = 0.0
        if self.no_long_qty > 0:
            avg_cost = self.no_long_cost / self.no_long_qty
            res_no = self.no_long_qty * (no_value - avg_cost)

        self.resolution_pnl = res_yes + res_no
        total = self.realized_pnl + self.resolution_pnl
        logger.info(
            f"Market resolved [{outcome}]: {self.market_question[:50]}\n"
            f"  realized={self.realized_pnl:+.4f} resolution={self.resolution_pnl:+.4f} "
            f"(yes={res_yes:+.4f} no={res_no:+.4f}) total={total:+.4f} | fills={len(self.fills)}"
        )
        return total

    def summary_dict(self) -> dict:
        yes_fills = sum(1 for f in self.fills if f.side == "YES")
        no_fills = sum(1 for f in self.fills if f.side == "NO")
        return {
            "market_id": self.market_id,
            "market_question": self.market_question,
            "quote_count": self.quote_count,
            "fill_count": len(self.fills),
            "yes_fills": yes_fills,
            "no_fills": no_fills,
            "yes_long_qty": round(self.yes_long_qty, 4),
            "yes_long_cost": round(self.yes_long_cost, 4),
            "no_long_qty": round(self.no_long_qty, 4),
            "no_long_cost": round(self.no_long_cost, 4),
            "net_yes_qty": round(self.net_yes_qty, 4),
            "net_no_qty": round(self.net_no_qty, 4),
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
        quotes,            # BothSidesQuotes from strategy_btc_mm
        ob_yes,            # Orderbook for YES token
        ob_no,             # Orderbook for NO token
        only_reduce: bool = False,
    ) -> list[MMFill]:
        """
        Simulate fills against both YES and NO real orderbooks.

        Fill logic:
        - YES BID FILL: our yes_bid >= market YES best_ask → we BUY YES
        - YES ASK FILL: our yes_ask <= market YES best_bid → we SELL YES
        - NO BID FILL:  our no_bid >= market NO best_ask  → we BUY NO
        - NO ASK FILL:  our no_ask <= market NO best_bid  → we SELL NO

        In only_reduce mode: only allow fills that REDUCE existing inventory.
        """
        state = self._get_or_create(market)
        state.quote_count += 1
        state.last_bid = quotes.yes_bid
        state.last_ask = quotes.yes_ask
        state.last_p_fair = quotes.p_fair

        fills = []

        def _allows_buy(side: str) -> bool:
            if not only_reduce:
                return True
            if side == "YES":
                return state.net_yes_qty < 0
            else:
                return state.net_no_qty < 0

        def _allows_sell(side: str) -> bool:
            if not only_reduce:
                return True
            if side == "YES":
                return state.yes_long_qty > 0
            else:
                return state.no_long_qty > 0

        # ── YES side ──────────────────────────────────────────────────────────
        yes_best_ask = ob_yes.best_ask()
        yes_best_bid = ob_yes.best_bid()

        # YES BID FILL: our bid >= market ask → we BUY YES
        if quotes.yes_bid >= yes_best_ask and yes_best_ask > 0 and _allows_buy("YES"):
            depth = ob_yes.depth_at(yes_best_ask, "ask")
            size_usdc = min(self.order_size_usdc, depth)
            if size_usdc >= 0.5:
                qty = size_usdc / yes_best_ask
                fill = MMFill(
                    ts=time.time(), side="YES", direction="BUY",
                    price=yes_best_ask, size_usdc=size_usdc, qty=qty,
                    our_quote=quotes.yes_bid, market_price=yes_best_ask,
                    market_question=market.question, market_id=market.id,
                )
                state.record_fill(fill)
                fills.append(fill)
                logger.info(
                    f"[PAPER BID FILL] BUY YES @ {yes_best_ask:.4f} "
                    f"(our_bid={quotes.yes_bid:.4f}) size=${size_usdc:.2f}"
                )

        # YES ASK FILL: our ask <= market bid → we SELL YES
        if quotes.yes_ask <= yes_best_bid and yes_best_bid > 0 and _allows_sell("YES"):
            depth = ob_yes.depth_at(yes_best_bid, "bid")
            size_usdc = min(self.order_size_usdc, depth)
            if size_usdc >= 0.5:
                qty = size_usdc / quotes.yes_ask
                fill = MMFill(
                    ts=time.time(), side="YES", direction="SELL",
                    price=quotes.yes_ask, size_usdc=size_usdc, qty=qty,
                    our_quote=quotes.yes_ask, market_price=yes_best_bid,
                    market_question=market.question, market_id=market.id,
                )
                state.record_fill(fill)
                fills.append(fill)
                logger.info(
                    f"[PAPER ASK FILL] SELL YES @ {quotes.yes_ask:.4f} "
                    f"(market_bid={yes_best_bid:.4f}) size=${size_usdc:.2f}"
                )

        # ── NO side ───────────────────────────────────────────────────────────
        no_best_ask = ob_no.best_ask()
        no_best_bid = ob_no.best_bid()

        # NO BID FILL: our no_bid >= market NO ask → we BUY NO
        if quotes.no_bid >= no_best_ask and no_best_ask > 0 and _allows_buy("NO"):
            depth = ob_no.depth_at(no_best_ask, "ask")
            size_usdc = min(self.order_size_usdc, depth)
            if size_usdc >= 0.5:
                qty = size_usdc / no_best_ask
                fill = MMFill(
                    ts=time.time(), side="NO", direction="BUY",
                    price=no_best_ask, size_usdc=size_usdc, qty=qty,
                    our_quote=quotes.no_bid, market_price=no_best_ask,
                    market_question=market.question, market_id=market.id,
                )
                state.record_fill(fill)
                fills.append(fill)
                logger.info(
                    f"[PAPER BID FILL] BUY NO  @ {no_best_ask:.4f} "
                    f"(our_bid={quotes.no_bid:.4f}) size=${size_usdc:.2f}"
                )

        # NO ASK FILL: our no_ask <= market NO bid → we SELL NO
        if quotes.no_ask <= no_best_bid and no_best_bid > 0 and _allows_sell("NO"):
            depth = ob_no.depth_at(no_best_bid, "bid")
            size_usdc = min(self.order_size_usdc, depth)
            if size_usdc >= 0.5:
                qty = size_usdc / quotes.no_ask
                fill = MMFill(
                    ts=time.time(), side="NO", direction="SELL",
                    price=quotes.no_ask, size_usdc=size_usdc, qty=qty,
                    our_quote=quotes.no_ask, market_price=no_best_bid,
                    market_question=market.question, market_id=market.id,
                )
                state.record_fill(fill)
                fills.append(fill)
                logger.info(
                    f"[PAPER ASK FILL] SELL NO  @ {quotes.no_ask:.4f} "
                    f"(market_bid={no_best_bid:.4f}) size=${size_usdc:.2f}"
                )

        self._log_quote(market, quotes, yes_best_bid, yes_best_ask, no_best_bid, no_best_ask, fills)
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
                yes_f = sum(1 for f in s.fills if f.side == "YES")
                no_f = sum(1 for f in s.fills if f.side == "NO")
                print(
                    f"  {status} {s.market_question[:45]}\n"
                    f"    quotes={s.quote_count} fills={len(s.fills)} (YES:{yes_f} NO:{no_f}) "
                    f"realized={s.realized_pnl:+.4f} total={total_m:+.4f}"
                )

    def _log_quote(self, market, quotes, yes_best_bid, yes_best_ask, no_best_bid, no_best_ask, fills) -> None:
        """Append quote cycle to Drive log."""
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            record = {
                "event": "quote",
                "ts": datetime.now(timezone.utc).isoformat(),
                "market_id": market.id,
                "market_question": market.question[:60],
                "yes_bid": round(quotes.yes_bid, 4),
                "yes_ask": round(quotes.yes_ask, 4),
                "no_bid": round(quotes.no_bid, 4),
                "no_ask": round(quotes.no_ask, 4),
                "p_fair": round(quotes.p_fair, 4),
                "inventory_skew": round(quotes.inventory_skew, 4),
                "only_reduce": quotes.only_reduce,
                "yes_best_bid": round(yes_best_bid, 4),
                "yes_best_ask": round(yes_best_ask, 4),
                "no_best_bid": round(no_best_bid, 4),
                "no_best_ask": round(no_best_ask, 4),
                "spread": round(quotes.yes_ask - quotes.yes_bid, 4),
                "fills": len(fills),
                "yes_fills": sum(1 for f in fills if f.side == "YES"),
                "no_fills": sum(1 for f in fills if f.side == "NO"),
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
