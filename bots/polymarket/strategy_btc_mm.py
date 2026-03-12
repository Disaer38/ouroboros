"""
BTC Up/Down Market Maker — Avellaneda-Stoikov for directional binary markets.

Polymarket "Bitcoin Up or Down" markets:
  YES token: BTC price HIGHER at window end than window start.
  NO  token: BTC price LOWER  at window end than window start.

Strategy:
  1. Compute P_fair = P(Up) via GBM + momentum (fair_price.py).
  2. Compute reservation price r = P_fair - γ * σ² * q * T
     where q = current YES inventory, T = time remaining.
  3. Set bid = r - δ/2, ask = r + δ/2
     where δ = γ * σ² * T + (2/γ) * ln(1 + γ/κ)
  4. Place/update orders every requote_interval seconds.
  5. Cancel if market expires in < 30s.
  6. Track fills, update inventory, compute PnL.

References:
  Avellaneda & Stoikov (2008) "High-frequency trading in a limit order book"
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import re

import httpx

from .fair_price import (
    bootstrap_from_klines,
    compute_updown_fair_price,
    fetch_btc_price,
    get_btc_state,
)
from .kappa_estimator import KappaEstimator
from .models import Market

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
MIN_SPREAD           = 0.04   # 4¢ minimum spread (covers fees + slippage)
MAX_SPREAD           = 0.30   # 30¢ max spread (avoid being uncompetitive)
MIN_ORDER_SIZE       = 1.0    # Minimum order in USDC
CANCEL_BEFORE_EXPIRY = 30.0   # Cancel orders this many seconds before expiry
DEFAULT_REQUOTE      = 15.0   # Re-quote every N seconds
SIGMA_ANNUAL_DEFAULT = 0.80   # fallback if no Binance data
MAX_INVENTORY_YES    = 50.0   # Max net YES inventory (USDC worth)
MAX_INVENTORY_NO     = 50.0   # Max net NO inventory (USDC worth)
CLOB_API             = "https://clob.polymarket.com"
POLYMARKET_FEE       = 0.02   # 2% taker fee per side


# ── Market window parser ─────────────────────────────────────────────────────

def parse_window_from_question(question: str) -> Optional[tuple[float, float]]:
    """
    Parse start and end UTC timestamps from an Up/Down market question.

    Handles formats like:
      "Bitcoin Up or Down - March 12, 5:25PM-5:30PM ET"
      "Bitcoin Up or Down - March 13, 11:15AM-11:30AM ET"

    Returns (start_ts, end_ts) as Unix timestamps in UTC, or None if unparseable.
    ET = UTC-5 (EST) or UTC-4 (EDT). We use UTC-5 as conservative default.
    """
    pattern = (
        r"(\w+ \d+),\s*"
        r"(\d+:\d+(?:AM|PM))"
        r"\s*[-–]\s*"
        r"(\d+:\d+(?:AM|PM))"
        r"\s*ET"
    )
    m = re.search(pattern, question, re.IGNORECASE)
    if not m:
        return None

    date_str, start_str, end_str = m.group(1), m.group(2).upper(), m.group(3).upper()
    year = datetime.now(timezone.utc).year

    def parse_dt(date_s: str, time_s: str) -> Optional[float]:
        try:
            dt = datetime.strptime(f"{date_s} {year} {time_s}", "%B %d %Y %I:%M%p")
            et_offset = 5 * 3600  # ET = UTC-5 (EST)
            return dt.timestamp() + et_offset
        except ValueError:
            return None

    start_ts = parse_dt(date_str, start_str)
    end_ts   = parse_dt(date_str, end_str)

    if start_ts is None or end_ts is None:
        return None

    if end_ts <= start_ts:
        end_ts += 86400

    return (start_ts, end_ts)


def is_updown_market(question: str) -> bool:
    """Return True if this is an Up/Down directional market."""
    return "up or down" in question.lower()


# ── Inventory & PnL tracker ─────────────────────────────────────────────────

@dataclass
class Inventory:
    """
    Tracks YES/NO position with cost basis and realized PnL.

    Convention: positive = long (bought), negative = short (sold).
    For binary markets we only go long (buy YES or buy NO).
    """
    yes_qty: float   = 0.0   # shares held
    yes_cost: float  = 0.0   # total USDC spent on YES
    no_qty: float    = 0.0   # shares held
    no_cost: float   = 0.0   # total USDC spent on NO
    realized_pnl: float = 0.0  # from closed positions

    @property
    def net_yes(self) -> float:
        """Net YES exposure (positive = long YES = long Up)."""
        return self.yes_qty - self.no_qty

    @property
    def total_cost(self) -> float:
        return self.yes_cost + self.no_cost

    def update_buy(self, side: str, qty: float, price: float) -> None:
        """Record a buy fill."""
        if side == "YES":
            self.yes_qty += qty
            self.yes_cost += qty * price
        elif side == "NO":
            self.no_qty += qty
            self.no_cost += qty * price

    def update_sell(self, side: str, qty: float, price: float) -> None:
        """Record a sell fill (maker sold to incoming taker)."""
        if side == "YES":
            if self.yes_qty > 0:
                avg_cost = self.yes_cost / self.yes_qty if self.yes_qty > 0 else price
                filled_qty = min(qty, self.yes_qty)
                self.realized_pnl += filled_qty * (price - avg_cost)
                self.yes_qty -= filled_qty
                self.yes_cost -= filled_qty * avg_cost
        elif side == "NO":
            if self.no_qty > 0:
                avg_cost = self.no_cost / self.no_qty if self.no_qty > 0 else price
                filled_qty = min(qty, self.no_qty)
                self.realized_pnl += filled_qty * (price - avg_cost)
                self.no_qty -= filled_qty
                self.no_cost -= filled_qty * avg_cost

    def unrealized_pnl(self, yes_mark: float, no_mark: float) -> float:
        """Unrealized PnL at current mark prices."""
        return (
            self.yes_qty * yes_mark - self.yes_cost +
            self.no_qty * no_mark - self.no_cost
        )

    def is_within_limits(self) -> bool:
        """Check inventory against limits."""
        return self.yes_cost <= MAX_INVENTORY_YES and self.no_cost <= MAX_INVENTORY_NO


# ── Open order tracker ───────────────────────────────────────────────────────

@dataclass
class OpenOrder:
    """Tracks a single open limit order."""
    order_id: str
    side: str         # "YES" or "NO"
    direction: str    # "BUY" or "SELL"
    price: float
    size: float
    placed_at: float = field(default_factory=time.time)


# ── Avellaneda-Stoikov quotes ────────────────────────────────────────────────

@dataclass
class ASQuotes:
    """Computed A-S bid/ask pair with metadata."""
    p_fair: float
    reservation: float
    bid: float
    ask: float
    spread: float
    kappa: float
    sigma: float
    gamma: float
    t_remaining: float


def compute_as_quotes(
    p_fair: float,
    inventory_net: float,
    t_remaining: float,
    sigma: float,
    gamma: float,
    kappa: float,
) -> ASQuotes:
    """
    Avellaneda-Stoikov optimal bid/ask for a binary prediction market.

    Reservation price:
        r = p_fair - γ * σ² * q * T

    Optimal spread:
        δ = γ * σ² * T + (2/γ) * ln(1 + γ/κ)
    """
    reservation = p_fair - gamma * sigma**2 * inventory_net * t_remaining

    spread_as = gamma * sigma**2 * t_remaining
    if kappa > 0 and gamma > 0:
        spread_as += (2.0 / gamma) * math.log(1.0 + gamma / kappa)
    spread = max(MIN_SPREAD, min(MAX_SPREAD, spread_as))

    bid = reservation - spread / 2.0
    ask = reservation + spread / 2.0

    bid = max(0.01, min(0.99, bid))
    ask = max(0.01, min(0.99, ask))
    if ask - bid < MIN_SPREAD:
        mid = (bid + ask) / 2.0
        bid = mid - MIN_SPREAD / 2.0
        ask = mid + MIN_SPREAD / 2.0

    return ASQuotes(
        p_fair=p_fair,
        reservation=reservation,
        bid=bid,
        ask=ask,
        spread=ask - bid,
        kappa=kappa,
        sigma=sigma,
        gamma=gamma,
        t_remaining=t_remaining,
    )


# ── Market Maker ─────────────────────────────────────────────────────────────

class BTCUpDownMM:
    """
    Market maker for a single "Bitcoin Up or Down" Polymarket binary.

    Lifecycle:
      1. __init__: parse market, bootstrap Binance data.
      2. run():    loop until expiry, re-quoting every requote_interval.
      3. _quote(): compute A-S quotes, submit orders (or log in paper mode).
      4. _sync_fills(): poll CLOB for fills, update inventory.
    """

    def __init__(
        self,
        market: Market,
        clob_client=None,
        gamma: float = 0.5,
        order_size_usdc: float = 10.0,
        requote_interval: float = DEFAULT_REQUOTE,
        paper: bool = True,
    ) -> None:
        self.market = market
        self.clob = clob_client
        self.gamma = gamma
        self.order_size_usdc = order_size_usdc
        self.requote_interval = requote_interval
        self.paper = paper

        # Parse window
        window = parse_window_from_question(market.question)
        if window:
            self.start_ts, self.end_ts = window
        else:
            self.end_ts = market.end_date.timestamp()
            self.start_ts = self.end_ts - 300.0

        self.window_seconds = max(60.0, self.end_ts - self.start_ts)

        # Components
        self.inventory = Inventory()
        self.kappa_est = KappaEstimator()
        self.open_orders: dict[str, OpenOrder] = {}  # order_id → OpenOrder

        # State
        self.last_quotes: Optional[ASQuotes] = None
        self.last_p_fair: float = 0.5
        self.quote_count: int = 0
        self.fill_count: int = 0

        logger.info(
            f"MM init: {market.question[:60]} | "
            f"window={self.window_seconds:.0f}s | "
            f"expiry={datetime.fromtimestamp(self.end_ts, tz=timezone.utc).isoformat()}"
        )

    @property
    def time_remaining(self) -> float:
        """Seconds until window end."""
        return max(0.0, self.end_ts - time.time())

    @property
    def time_remaining_years(self) -> float:
        return self.time_remaining / (365 * 24 * 3600)

    def mid_price(self) -> float:
        """Best estimate of mid price (= P_fair when no orderbook data)."""
        return self.last_p_fair

    def compute_quotes(self) -> tuple[float, float]:
        """Compute current A-S bid/ask. Returns (bid, ask)."""
        state = get_btc_state()
        if len(state.prices) < 3:
            bootstrap_from_klines()

        fetch_btc_price()

        p_fair = compute_updown_fair_price(
            window_seconds=self.window_seconds,
            momentum_lookback=min(60.0, self.window_seconds),
        )
        self.last_p_fair = p_fair

        sigma = state.sigma_annualized()
        kappa = self.kappa_est.estimate_kappa()
        t_remaining = max(1.0 / 3600, self.time_remaining_years)

        quotes = compute_as_quotes(
            p_fair=p_fair,
            inventory_net=self.inventory.net_yes,
            t_remaining=t_remaining,
            sigma=sigma,
            gamma=self.gamma,
            kappa=kappa,
        )
        self.last_quotes = quotes
        return quotes.bid, quotes.ask

    async def _fetch_open_orders(self) -> list[dict]:
        """Fetch open orders for this market from CLOB."""
        if self.clob is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            orders = await loop.run_in_executor(None, lambda: self.clob.get_orders(market=self.market.id))
            return orders if isinstance(orders, list) else []
        except Exception as e:
            logger.warning(f"Failed to fetch open orders: {e}")
            return []

    async def _sync_fills(self) -> None:
        """
        Poll CLOB for filled/partially-filled orders and update inventory.

        Compares tracked open orders vs current CLOB state.
        """
        if self.clob is None or not self.open_orders:
            return
        try:
            current_open = await self._fetch_open_orders()
            current_ids = {o.get("id") or o.get("order_id") for o in current_open}

            for order_id, order in list(self.open_orders.items()):
                if order_id not in current_ids:
                    # Order no longer open → it was filled or cancelled
                    # Assume filled (conservative; could check trade history)
                    self.inventory.update_buy(
                        side=order.side,
                        qty=order.size / order.price,  # shares
                        price=order.price,
                    )
                    # Update kappa: this spread got filled
                    self.kappa_est.record_order_fill(
                        quote_price=order.price,
                        mid_price=self.mid_price(),
                        filled=True,
                    )
                    self.fill_count += 1
                    logger.info(
                        f"Fill detected: {order.direction} {order.side} "
                        f"@ {order.price:.4f} x {order.size:.2f} USDC | "
                        f"inv_yes={self.inventory.yes_qty:.2f} inv_no={self.inventory.no_qty:.2f}"
                    )
                    del self.open_orders[order_id]
        except Exception as e:
            logger.warning(f"Fill sync error: {e}")

    async def _place_orders(self, bid: float, ask: float) -> None:
        """Place or update orders on CLOB. No-op in paper mode."""
        if self.paper:
            q = self.last_quotes
            logger.info(
                f"[PAPER] {self.market.question[:55]}\n"
                f"        P_fair={q.p_fair:.4f} r={q.reservation:.4f} "
                f"bid={bid:.4f} ask={ask:.4f} spread={q.spread:.4f} "
                f"κ={q.kappa:.2f} σ={q.sigma:.2%} inv={self.inventory.net_yes:+.2f}"
            )
            return

        # Live: cancel existing, place new
        await self._cancel_orders()

        size = self.order_size_usdc

        # Check inventory limits before placing
        if not self.inventory.is_within_limits():
            logger.warning(
                f"Inventory limit reached — skipping quote. "
                f"YES=${self.inventory.yes_cost:.1f} NO=${self.inventory.no_cost:.1f}"
            )
            return

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            loop = asyncio.get_event_loop()

            # Place bid: buy YES at bid price
            bid_args = OrderArgs(token_id=self.market.yes_token_id, price=round(bid, 4), size=size, side="BUY")
            bid_order = self.clob.create_order(bid_args)
            bid_resp = await loop.run_in_executor(None, lambda: self.clob.post_order(bid_order, OrderType.GTC))
            if bid_resp and isinstance(bid_resp, dict) and bid_resp.get("orderID"):
                oid = bid_resp["orderID"]
                self.open_orders[oid] = OpenOrder(
                    order_id=oid,
                    side="YES",
                    direction="BUY",
                    price=bid,
                    size=size,
                )

            # Place ask: sell YES (if we have inventory) or buy NO
            if self.inventory.yes_qty * ask >= MIN_ORDER_SIZE:
                # We have YES to sell
                ask_args = OrderArgs(token_id=self.market.yes_token_id, price=round(ask, 4), size=min(size, self.inventory.yes_qty * ask), side="SELL")
                ask_order = self.clob.create_order(ask_args)
                ask_resp = await loop.run_in_executor(None, lambda: self.clob.post_order(ask_order, OrderType.GTC))
            else:
                # No YES inventory to sell — buy NO as hedge
                no_bid = 1.0 - ask  # complement price
                no_bid = max(0.01, min(0.99, no_bid))
                ask_args = OrderArgs(token_id=self.market.no_token_id, price=round(no_bid, 4), size=size, side="BUY")
                ask_order = self.clob.create_order(ask_args)
                ask_resp = await loop.run_in_executor(None, lambda: self.clob.post_order(ask_order, OrderType.GTC))

            if ask_resp and isinstance(ask_resp, dict) and ask_resp.get("orderID"):
                oid = ask_resp["orderID"]
                side = "YES" if self.inventory.yes_qty * ask >= MIN_ORDER_SIZE else "NO"
                direction = "SELL" if side == "YES" else "BUY"
                self.open_orders[oid] = OpenOrder(
                    order_id=oid,
                    side=side,
                    direction=direction,
                    price=ask,
                    size=size,
                )

            self.quote_count += 1
            logger.info(
                f"Quoted: bid={bid:.4f} ask={ask:.4f} "
                f"[{self.market.question[:40]}] "
                f"inv_yes={self.inventory.net_yes:+.2f}"
            )
        except Exception as e:
            logger.error(f"Order placement failed: {e}")

    async def _cancel_orders(self) -> None:
        """Cancel all tracked open orders for this market."""
        if self.clob is None:
            return
        loop = asyncio.get_event_loop()
        for order_id, order in list(self.open_orders.items()):
            try:
                await loop.run_in_executor(None, lambda oid=order_id: self.clob.cancel(order_id=oid))
            except Exception as e:
                logger.warning(f"Cancel order {order_id} failed: {e}")
            self.kappa_est.record_order_fill(
                quote_price=order.price,
                mid_price=self.mid_price(),
                filled=False,
            )
        self.open_orders.clear()

    def log_status(self) -> None:
        """Log current strategy status."""
        q = self.last_quotes
        if q is None:
            return
        inv = self.inventory
        logger.info(
            f"Status [{self.market.question[:45]}]\n"
            f"  P_fair={q.p_fair:.4f} | bid={q.bid:.4f} ask={q.ask:.4f} "
            f"spread={q.spread:.4f}\n"
            f"  κ={q.kappa:.2f} σ={q.sigma:.2%} γ={q.gamma} "
            f"T={q.t_remaining*365*24*60:.1f}min\n"
            f"  inv_yes={inv.yes_qty:.2f}({inv.yes_cost:.2f}$) "
            f"inv_no={inv.no_qty:.2f}({inv.no_cost:.2f}$) "
            f"net={inv.net_yes:+.3f}\n"
            f"  realized_pnl={inv.realized_pnl:+.4f} | "
            f"quotes={self.quote_count} fills={self.fill_count}"
        )

    async def run(self) -> None:
        """Main MM loop: quote until expiry."""
        logger.info(f"Starting MM: {self.market.question[:60]}")

        # Bootstrap price data once
        state = get_btc_state()
        if len(state.prices) < 3:
            bootstrap_from_klines()

        # Bootstrap kappa from market trade history
        self.kappa_est.bootstrap_from_polymarket(self.market.yes_token_id)

        cycle = 0
        while self.time_remaining > CANCEL_BEFORE_EXPIRY:
            try:
                # Sync fills from previous cycle
                if not self.paper:
                    await self._sync_fills()

                bid, ask = self.compute_quotes()
                await self._place_orders(bid, ask)

                # Log status every 4 cycles
                cycle += 1
                if cycle % 4 == 0:
                    self.log_status()

            except Exception as e:
                logger.error(f"Quote cycle error: {e}", exc_info=True)

            await asyncio.sleep(self.requote_interval)

        # Final cancel + status
        logger.info(f"Market expiring in {self.time_remaining:.0f}s — cancelling orders.")
        await self._cancel_orders()

        inv = self.inventory
        logger.info(
            f"MM done: {self.market.question[:55]}\n"
            f"  quotes={self.quote_count} fills={self.fill_count}\n"
            f"  inv_yes={inv.yes_qty:.2f} inv_no={inv.no_qty:.2f}\n"
            f"  realized_pnl={inv.realized_pnl:+.4f} USDC"
        )
