"""
Strategy: "Dominant Side + Insurance" (reverse-engineered from vague-sourdough).

Logic:
  1. Scan 5-minute crypto binary markets (BTC/ETH up-or-down).
  2. Wait for price asymmetry: one side drifts above 0.60.
  3. Buy dominant side (price >= MIN_DOMINANT_PRICE) with BASE_SIZE USDC.
  4. Buy insurance on opposite side (price <= MAX_INSURANCE_PRICE) with INSURANCE_FRACTION * BASE_SIZE.
  5. Hold to resolution (5-minute rounds resolve automatically).
  6. Expected PnL: if dominant side wins, net = shares_dominant * 1.0 - cost_dominant - cost_insurance.
                  if insurance wins, net = shares_insurance * 1.0 - cost_dominant - cost_insurance.

Risk management:
  - Max open positions: MAX_OPEN_POSITIONS
  - Daily loss limit: MAX_DAILY_LOSS_USDC
  - Min liquidity required: MIN_LIQUIDITY_USDC on dominant side
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------

MIN_DOMINANT_PRICE: float = 0.60   # Buy dominant side only if price >= this
MAX_INSURANCE_PRICE: float = 0.15  # Buy insurance only if price <= this
BASE_SIZE_USDC: float = 15.0       # USDC to spend on dominant side per trade
INSURANCE_FRACTION: float = 0.20   # Insurance bet = 20% of base size
MIN_LIQUIDITY_USDC: float = 50.0   # Minimum depth on dominant side
MAX_OPEN_POSITIONS: int = 5        # Max concurrent open positions
MAX_DAILY_LOSS_USDC: float = 50.0  # Stop trading if daily loss exceeds this

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# 5-min crypto market slug patterns
CRYPTO_5MIN_SLUGS = ["btc-up-or-down-5m"]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SourdoughOpportunity:
    """A detected Dominant Side + Insurance opportunity."""
    condition_id: str
    question: str
    dominant_token_id: str
    dominant_side: str          # "YES" or "NO"
    dominant_price: float
    insurance_token_id: str
    insurance_side: str         # opposite of dominant
    insurance_price: float
    liquidity_dominant: float   # USDC depth on dominant side
    round_end_ts: int           # Unix timestamp when round closes
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def insurance_size_usdc(self) -> float:
        return BASE_SIZE_USDC * INSURANCE_FRACTION

    def dominant_shares(self) -> float:
        return BASE_SIZE_USDC / self.dominant_price if self.dominant_price > 0 else 0.0

    def insurance_shares(self) -> float:
        ins_size = self.insurance_size_usdc()
        return ins_size / self.insurance_price if self.insurance_price > 0 else 0.0

    def expected_pnl_if_dominant_wins(self) -> float:
        return self.dominant_shares() - BASE_SIZE_USDC - self.insurance_size_usdc()

    def expected_pnl_if_insurance_wins(self) -> float:
        return self.insurance_shares() - BASE_SIZE_USDC - self.insurance_size_usdc()

    def worst_case_pnl(self) -> float:
        return min(self.expected_pnl_if_dominant_wins(), self.expected_pnl_if_insurance_wins())

    def __str__(self) -> str:
        return (
            f"[SOURDOUGH] {self.question[:50]} | "
            f"DOM={self.dominant_side}@{self.dominant_price:.3f} "
            f"INS={self.insurance_side}@{self.insurance_price:.3f} | "
            f"worst_pnl=${self.worst_case_pnl():+.2f}"
        )


@dataclass
class OpenPosition:
    opportunity: SourdoughOpportunity
    dominant_order_id: Optional[str]
    insurance_order_id: Optional[str]
    dominant_cost: float
    insurance_cost: float
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    pnl: Optional[float] = None


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def fetch_active_5min_markets() -> list[dict]:
    """Fetch active 5-minute crypto markets from Gamma."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "tag_slug": "crypto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("data", [])
        # Filter for 5-minute rounds
        result = []
        for m in markets:
            slug = str(m.get("slug", "") or m.get("marketSlug", ""))
            q = str(m.get("question", "")).lower()
            if ("5m" in slug or "5-min" in slug or "5 min" in q) and ("btc" in q or "bitcoin" in q):
                result.append(m)
        return result
    except Exception as e:
        logger.error(f"Failed to fetch 5-min markets: {e}")
        return []


def fetch_orderbook(token_id: str) -> tuple[float, float]:
    """
    Fetch best ask price and available liquidity for a token.
    Returns (best_ask_price, depth_usdc).
    """
    try:
        resp = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        resp.raise_for_status()
        book = resp.json()
        asks = book.get("asks", [])
        if not asks:
            return 1.0, 0.0
        # best ask = lowest price
        best = min(asks, key=lambda x: float(x.get("price", 1.0)))
        price = float(best.get("price", 1.0))
        # depth at best ask in USDC
        depth = sum(float(e.get("price", 0)) * float(e.get("size", 0))
                    for e in asks if float(e.get("price", 1.0)) <= price + 0.05)
        return price, depth
    except Exception as e:
        logger.debug(f"Orderbook fetch failed for {token_id[:16]}...: {e}")
        return 1.0, 0.0


# ---------------------------------------------------------------------------
# Opportunity scanner
# ---------------------------------------------------------------------------

def scan_for_opportunities(markets: list[dict]) -> list[SourdoughOpportunity]:
    """Scan a list of markets for Dominant Side + Insurance setups."""
    opps = []
    for m in markets:
        condition_id = m.get("conditionId") or m.get("id", "")
        question = m.get("question", "")
        yes_token = m.get("clobTokenIds", [None, None])[0] if m.get("clobTokenIds") else m.get("yes_token_id")
        no_token = m.get("clobTokenIds", [None, None])[1] if m.get("clobTokenIds") else m.get("no_token_id")
        end_ts = int(m.get("endDateIso") and
                     datetime.fromisoformat(str(m["endDateIso"]).replace("Z", "+00:00")).timestamp()
                     or time.time() + 300)

        if not yes_token or not no_token:
            continue

        yes_price, yes_depth = fetch_orderbook(yes_token)
        no_price, no_depth = fetch_orderbook(no_token)

        # Check YES dominant
        if yes_price >= MIN_DOMINANT_PRICE and no_price <= MAX_INSURANCE_PRICE:
            if yes_depth >= MIN_LIQUIDITY_USDC:
                opps.append(SourdoughOpportunity(
                    condition_id=condition_id,
                    question=question,
                    dominant_token_id=yes_token,
                    dominant_side="YES",
                    dominant_price=yes_price,
                    insurance_token_id=no_token,
                    insurance_side="NO",
                    insurance_price=no_price,
                    liquidity_dominant=yes_depth,
                    round_end_ts=end_ts,
                ))
        # Check NO dominant
        elif no_price >= MIN_DOMINANT_PRICE and yes_price <= MAX_INSURANCE_PRICE:
            if no_depth >= MIN_LIQUIDITY_USDC:
                opps.append(SourdoughOpportunity(
                    condition_id=condition_id,
                    question=question,
                    dominant_token_id=no_token,
                    dominant_side="NO",
                    dominant_price=no_price,
                    insurance_token_id=yes_token,
                    insurance_side="YES",
                    insurance_price=yes_price,
                    liquidity_dominant=no_depth,
                    round_end_ts=end_ts,
                ))

    return opps


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------

class PositionTracker:
    def __init__(self):
        self.open: list[OpenPosition] = []
        self.closed: list[OpenPosition] = []
        self.daily_pnl: float = 0.0

    def add(self, pos: OpenPosition) -> None:
        self.open.append(pos)

    def count_open(self) -> int:
        return len([p for p in self.open if not p.resolved])

    def resolve_expired(self) -> None:
        """Mark positions whose round has ended as resolved (paper mode)."""
        now_ts = time.time()
        for pos in self.open:
            if not pos.resolved and now_ts >= pos.opportunity.round_end_ts + 30:
                pos.resolved = True
                # In paper mode, we don't know the actual outcome — log as pending
                logger.info(f"Position expired (pending resolution): {pos.opportunity.question[:40]}")
                self.closed.append(pos)
        self.open = [p for p in self.open if not p.resolved]

    def total_pnl(self) -> float:
        return sum(p.pnl or 0.0 for p in self.closed)

    def summary(self) -> str:
        return (
            f"Open: {self.count_open()} | "
            f"Closed: {len(self.closed)} | "
            f"Total PnL: ${self.total_pnl():+.2f}"
        )
