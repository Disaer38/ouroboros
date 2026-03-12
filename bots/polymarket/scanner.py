"""
Market discovery and neg-risk opportunity scanner.

Discovery strategy:
  1. Gamma /events endpoint → 5min/15min intraday markets (primary target)
  2. Gamma /markets endpoint → daily/hourly markets (fallback arb targets)
  3. Filter to active, unclosed crypto "Up or Down" markets
  4. Fetch CLOB orderbooks and check neg-risk condition

Neg-risk condition: YES_ask + NO_ask < profit_threshold
  (default 0.97 → 3% gross margin before 2% taker fee)
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
import json

import httpx

from .models import ArbOpportunity, Market, Orderbook

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Matches intraday time ranges like "3:00PM-3:05PM", "3:00PM-3:15PM"
TIME_RANGE_RE = re.compile(r"\d+:\d+[AP]M[-–]\d+:\d+[AP]M", re.IGNORECASE)

CRYPTO_KEYWORDS = ["bitcoin", "btc"]
BTC_ONLY = True


def _is_target_market(question: str) -> bool:
    """Return True if this is a BTC Up/Down market."""
    q = question.lower()
    is_btc = "bitcoin" in q or "btc" in q
    has_updown = "up or down" in q
    return is_btc and has_updown


def _is_intraday(question: str) -> bool:
    """Return True if this is a 5min/15min intraday market (has time range in title)."""
    return bool(TIME_RANGE_RE.search(question))


def _parse_market_from_gamma(m: dict) -> Optional[Market]:
    """Parse a single market object from the Gamma API response."""
    try:
        # Token IDs come as JSON string or list
        tids_raw = m.get("clobTokenIds") or m.get("clob_token_ids")
        if isinstance(tids_raw, str):
            tids = json.loads(tids_raw)
        elif isinstance(tids_raw, list):
            tids = tids_raw
        else:
            return None

        if not tids or len(tids) < 2:
            return None

        end_str = m.get("endDate") or m.get("end_date_iso") or ""
        if not end_str:
            return None
        # Parse ISO datetime
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

        # Skip already-expired markets
        if end_dt < datetime.now(timezone.utc):
            return None

        question = m.get("question") or m.get("title") or ""
        if not question:
            return None

        return Market(
            id=str(m.get("id") or m.get("conditionId") or ""),
            question=question,
            slug=m.get("slug") or "",
            yes_token_id=str(tids[0]),
            no_token_id=str(tids[1]),
            end_date=end_dt,
            active=bool(m.get("active", True)),
        )
    except Exception as e:
        logger.debug(f"Failed to parse market: {e} | data={m}")
        return None


async def _fetch_events_markets(client: httpx.AsyncClient) -> list[Market]:
    """
    Fetch intraday markets from the Gamma /events endpoint.

    The events endpoint nests markets inside event objects. This is where
    5min/15min BTC/ETH Up-or-Down markets live.
    """
    markets: list[Market] = []
    try:
        resp = await client.get(
            f"{GAMMA_API}/events",
            params={
                "limit": 500,
                "order": "startDate",
                "ascending": "false",
                "active": "true",
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        events = resp.json()
        logger.debug(f"Events API returned {len(events)} events")

        for event in events:
            title = event.get("title") or ""
            if not _is_target_market(title):
                continue

            for m in event.get("markets", []):
                # Skip closed/inactive
                if m.get("closed") or not m.get("active", True):
                    continue
                market = _parse_market_from_gamma(m)
                if market:
                    markets.append(market)

    except Exception as e:
        logger.warning(f"Events API error: {e}")

    return markets


async def _fetch_direct_markets(client: httpx.AsyncClient) -> list[Market]:
    """
    Fetch markets directly from the Gamma /markets endpoint.

    Returns daily/hourly crypto markets as backup arb targets.
    """
    markets: list[Market] = []
    try:
        resp = await client.get(
            f"{GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 500,
                "order": "volume24hr",
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.debug(f"Markets API returned {len(data)} markets")

        for m in data:
            question = m.get("question") or m.get("title") or ""
            if not _is_target_market(question):
                continue
            if m.get("closed") or not m.get("active", True):
                continue
            market = _parse_market_from_gamma(m)
            if market:
                markets.append(market)

    except Exception as e:
        logger.warning(f"Markets API error: {e}")

    return markets


async def discover_markets(client: httpx.AsyncClient) -> list[Market]:
    """
    Discover all active crypto Up/Down markets.

    Combines results from /events (intraday 5min/15min) and
    /markets (daily/hourly). Deduplicates by market ID.
    Intraday markets are sorted first.
    """
    events_markets, direct_markets = await asyncio.gather(
        _fetch_events_markets(client),
        _fetch_direct_markets(client),
    )

    # Deduplicate by ID
    seen: set[str] = set()
    result: list[Market] = []

    # Intraday first (primary targets), then daily
    for m in events_markets + direct_markets:
        if m.id and m.id not in seen:
            seen.add(m.id)
            result.append(m)

    # Sort: intraday first, then by end_date ascending
    result.sort(key=lambda m: (not _is_intraday(m.question), m.end_date))

    logger.info(
        f"Discovered {len(result)} markets "
        f"({sum(1 for m in result if _is_intraday(m.question))} intraday, "
        f"{sum(1 for m in result if not _is_intraday(m.question))} daily)"
    )
    return result


async def get_orderbook(client: httpx.AsyncClient, token_id: str) -> Orderbook:
    """Fetch and parse CLOB orderbook for a single token."""
    resp = await client.get(
        f"{CLOB_API}/book",
        params={"token_id": token_id},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()

    def parse_levels(raw: list) -> list[tuple[float, float]]:
        return [(float(x["price"]), float(x["size"])) for x in raw]

    return Orderbook(
        token_id=token_id,
        bids=parse_levels(data.get("bids", [])),
        asks=parse_levels(data.get("asks", [])),
    )


async def check_neg_risk(
    client: httpx.AsyncClient,
    market: Market,
    profit_threshold: float = 0.97,
    min_depth_usdc: float = 2.0,
) -> Optional[ArbOpportunity]:
    """
    Check if a market has a neg-risk (buy both sides) arbitrage opportunity.

    Args:
        profit_threshold: Enter trade if YES_ask + NO_ask < this value.
                          0.97 = 3% gross margin (before 2% taker fee on each side).
        min_depth_usdc:   Minimum USDC available on each side's best ask.

    Returns ArbOpportunity if opportunity exists, None otherwise.
    """
    try:
        ob_yes, ob_no = await asyncio.gather(
            get_orderbook(client, market.yes_token_id),
            get_orderbook(client, market.no_token_id),
        )
    except Exception as e:
        logger.debug(f"Orderbook fetch failed for {market.question[:40]}: {e}")
        return None

    yes_ask = ob_yes.best_ask()
    no_ask = ob_no.best_ask()

    # Sanity check: prices must be in (0, 1)
    if not (0 < yes_ask < 1 and 0 < no_ask < 1):
        return None

    total_cost = yes_ask + no_ask

    if total_cost >= profit_threshold:
        return None  # No arb

    # Check there's enough depth on both sides
    yes_depth = ob_yes.depth_at(yes_ask, "ask")
    no_depth = ob_no.depth_at(no_ask, "ask")

    if yes_depth < min_depth_usdc or no_depth < min_depth_usdc:
        logger.debug(
            f"Depth too thin: YES={yes_depth:.1f} NO={no_depth:.1f} USDC "
            f"(min={min_depth_usdc})"
        )
        return None

    profit_pct = (1.0 - total_cost) * 100
    # Available size limited by shallowest side, capped at $50
    max_size = min(yes_depth, no_depth, 50.0)

    return ArbOpportunity(
        market=market,
        strategy="neg_risk",
        yes_ask=yes_ask,
        no_ask=no_ask,
        total_cost=total_cost,
        profit_pct=profit_pct,
        yes_depth=yes_depth,
        no_depth=no_depth,
        max_size_usdc=max_size,
    )


async def scan_all(
    client: httpx.AsyncClient,
    markets: list[Market],
    profit_threshold: float = 0.97,
    min_depth_usdc: float = 2.0,
    concurrency: int = 10,
) -> list[ArbOpportunity]:
    """
    Scan a list of markets for neg-risk opportunities in parallel.

    Args:
        concurrency: Max parallel orderbook requests.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _check(m: Market) -> Optional[ArbOpportunity]:
        async with sem:
            return await check_neg_risk(client, m, profit_threshold, min_depth_usdc)

    results = await asyncio.gather(*[_check(m) for m in markets])
    opps = [r for r in results if r is not None]

    if opps:
        logger.info(f"Found {len(opps)} arb opportunities out of {len(markets)} markets")
    else:
        logger.info(f"No arb opportunities in {len(markets)} markets (all above threshold)")

    return sorted(opps, key=lambda o: o.total_cost)  # best first
