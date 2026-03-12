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
MIN_SPREAD      = 0.04   # 4¢ minimum spread (covers fees + slippage)
MAX_SPREAD      = 0.30   # 30¢ max spread (avoid being uncompetitive)
MIN_ORDER_SIZE  = 1.0    # Minimum order in USDC
CANCEL_BEFORE_EXPIRY = 30.0   # Cancel orders this many seconds before expiry
DEFAULT_REQUOTE  = 15.0  # Re-quote every N seconds
SIGMA_ANNUAL_DEFAULT = 0.80   # fallback if no Binance data


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
    # Match: "Month Day, H:MMam/pm-H:MMam/pm ET"
    pattern = (
        r"(\w+ \d+),\s*"           # Month Day,
        r"(\d+:\d+(?:AM|PM))"      # start time
        r"\s*[-–]\s*"              # separator
        r"(\d+:\d+(?:AM|PM))"      # end time
        r"\s*ET"                   # timezone
    )
    m = re.search(pattern, question, re.IGNORECASE)
    if not m:
        return None

    date_str, start_str, end_str = m.group(1), m.group(2).upper(), m.group(3).upper()

    # Parse current year
    year = datetime.now(timezone.utc).year

    def parse_dt(date_s: str, time_s: str) -> Optional[float]:
        try:
            dt = datetime.strptime(f"{date_s} {year} {time_s}", "%B %d %Y %I:%M%p")
            # ET = UTC-5 (EST). Adjust for EDT if needed — use -5 as safe default
            et_offset = 5 * 3600
            return dt.timestamp() + et_offset
        except ValueError:
            return None

    start_ts = parse_dt(date_str, start_str)
    end_ts   = parse_dt(date_str, end_str)

    if start_ts is None or end_ts is None:
        return None

    # Handle midnight rollover: if end < start, end is next day
    if end_ts <= start_ts:
        end_ts += 86400

    return (start_ts, end_ts)


def is_updown_market(question: str) -> bool:
    """Return True if this is an Up/Down directional market."""
    return "up or down" in question.lower()


# ── Inventory tracker ────────────────────────────────────────────────────────

@dataclass
class Inventory:
    """Tracks YES/NO position in a single market."""
    yes_qty: float = 0.0
    no_qty:  float = 0.0

    @property
    def net_yes(self) -> float:
        """Net YES exposure (positive = long YES = long Up)."""
        return self.yes_qty - self.no_qty

    def update(self, side: str, qty: float) -> None:
        if side == "YES":
            self.yes_qty += qty
        elif side == "NO":
            self.no_qty += qty


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

    Bid = r - δ/2
    Ask = r + δ/2

    Args:
        p_fair:        Fair probability of YES outcome.
        inventory_net: Net YES position (positive = long YES).
        t_remaining:   Time remaining in years (use window_seconds / 31536000).
        sigma:         Annualized volatility of the underlying (BTC).
        gamma:         Risk aversion coefficient (0.1 = low, 1.0 = high).
        kappa:         Fill intensity (higher = more fills per unit spread).

    Returns:
        ASQuotes with bid, ask, and diagnostics.
    """
    # Reservation price: skew away from current inventory
    reservation = p_fair - gamma * sigma**2 * inventory_net * t_remaining

    # Optimal spread
    spread_as = gamma * sigma**2 * t_remaining
    if kappa > 0 and gamma > 0:
        spread_as += (2.0 / gamma) * math.log(1.0 + gamma / kappa)
    spread = max(MIN_SPREAD, min(MAX_SPREAD, spread_as))

    bid = reservation - spread / 2.0
    ask = reservation + spread / 2.0

    # Clamp to valid probability range
    bid = max(0.01, min(0.99, bid))
    ask = max(0.01, min(0.99, ask))
    # Ensure positive spread after clamp
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
            # Fallback: use end_date from market, assume 5-minute window
            self.end_ts = market.end_date.timestamp()
            self.start_ts = self.end_ts - 300.0

        self.window_seconds = max(60.0, self.end_ts - self.start_ts)

        # Components
        self.inventory = Inventory()
        self.kappa_est = KappaEstimator()

        # State
        self.last_quotes: Optional[ASQuotes] = None
        self.last_p_fair: float = 0.5
        self.quote_count: int = 0

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

    def compute_quotes(self) -> tuple[float, float]:
        """Compute current A-S bid/ask. Returns (bid, ask)."""
        # Ensure Binance data
        state = get_btc_state()
        if len(state.prices) < 3:
            bootstrap_from_klines()

        # Update BTC price
        fetch_btc_price()

        p_fair = compute_updown_fair_price(
            window_seconds=self.window_seconds,
            momentum_lookback=min(60.0, self.window_seconds),
        )
        self.last_p_fair = p_fair

        sigma = state.sigma_annualized()
        kappa = self.kappa_est.estimate_kappa()
        t_remaining = max(1.0 / 3600, self.time_remaining_years)  # at least 1s

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

        # Live: submit to CLOB
        try:
            size = self.order_size_usdc
            # Cancel existing orders first
            await self._cancel_orders()
            # Place new bid (buy YES)
            await self.clob.create_order(
                token_id=self.market.yes_token_id,
                side="BUY",
                price=round(bid, 4),
                size=size,
            )
            # Place new ask (sell YES)
            await self.clob.create_order(
                token_id=self.market.yes_token_id,
                side="SELL",
                price=round(ask, 4),
                size=size,
            )
            self.quote_count += 1
            logger.info(f"Quoted: bid={bid:.4f} ask={ask:.4f} [{self.market.question[:40]}]")
        except Exception as e:
            logger.error(f"Order placement failed: {e}")

    async def _cancel_orders(self) -> None:
        """Cancel all open orders for this market."""
        if self.clob is None:
            return
        try:
            await self.clob.cancel_orders(market=self.market.condition_id)
        except Exception as e:
            logger.warning(f"Cancel failed: {e}")

    async def run(self) -> None:
        """Main MM loop: quote until expiry."""
        logger.info(f"Starting MM: {self.market.question[:60]}")

        # Bootstrap price data once
        state = get_btc_state()
        if len(state.prices) < 3:
            bootstrap_from_klines()

        while self.time_remaining > CANCEL_BEFORE_EXPIRY:
            try:
                bid, ask = self.compute_quotes()
                await self._place_orders(bid, ask)
            except Exception as e:
                logger.error(f"Quote cycle error: {e}")

            await asyncio.sleep(self.requote_interval)

        # Final cancel
        logger.info(f"Market expiring in {self.time_remaining:.0f}s — cancelling orders.")
        await self._cancel_orders()
        logger.info(
            f"MM done: {self.market.question[:55]} | "
            f"quotes_placed={self.quote_count} | "
            f"inv_yes={self.inventory.yes_qty:.1f} inv_no={self.inventory.no_qty:.1f}"
        )
