"""
Market scanner: discovers BTC/ETH 5min/15min markets and scans for neg-risk arbitrage.
Read-only — no wallet needed.

Neg-risk arb: if YES_ask + NO_ask < 1.0 - fees, buying both guarantees profit
at resolution regardless of outcome.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from .models import ArbOpportunity, Market, Orderbook

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Fee on Polymarket CLOB (2% taker)
TAKER_FEE = 0.02

# Keywords to identify BTC/ETH 5/15-min up-or-down markets
CRYPTO_KEYWORDS = [
    "btc up or down",
    "bitcoin up or down",
    "eth up or down",
    "ethereum up or down",
    "5-minute",
    "15-minute",
    "5 minute",
    "15 minute",
    "5min",
    "15min",
]

logger = logging.getLogger(__name__)


def _is_crypto_market(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in CRYPTO_KEYWORDS)


def _parse_market(raw: dict) -> Optional[Market]:
    """Parse a raw Gamma API market entry into a Market object."""
    try:
        token_ids = raw.get("clobTokenIds") or raw.get("clob_token_ids")
        if isinstance(token_ids, str):
            import json
            token_ids = json.loads(token_ids)
        if not token_ids or len(token_ids) < 2:
            return None

        end_raw = raw.get("endDate") or raw.get("end_date") or ""
        try:
            end_date = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except Exception:
            end_date = datetime.now(timezone.utc)

        return Market(
            id=str(raw["id"]),
            question=raw.get("question", ""),
            slug=raw.get("slug", ""),
            yes_token_id=token_ids[0],
            no_token_id=token_ids[1],
            end_date=end_date,
            active=raw.get("active", True),
        )
    except Exception as e:
        logger.debug(f"Failed to parse market {raw.get('id')}: {e}")
        return None


async def discover_markets(
    client: httpx.AsyncClient,
    limit: int = 200,
) -> list[Market]:
    """
    Fetch active markets from Gamma API and filter for BTC/ETH 5/15-min markets.
    """
    try:
        resp = await client.get(
            f"{GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        raw_markets = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch markets: {e}")
        return []

    markets = []
    for raw in raw_markets:
        if not _is_crypto_market(raw.get("question", "")):
            continue
        m = _parse_market(raw)
        if m:
            markets.append(m)

    logger.info(f"Discovered {len(markets)} crypto 5/15-min markets")
    return markets


async def get_orderbook(
    client: httpx.AsyncClient,
    token_id: str,
) -> Optional[Orderbook]:
    """
    Fetch the orderbook for a single CLOB token from Polymarket.
    Returns None on failure.
    """
    try:
        resp = await client.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug(f"Orderbook fetch failed for {token_id[:16]}…: {e}")
        return None

    def parse_side(entries: list) -> list:
        result = []
        for entry in entries:
            try:
                result.append((float(entry["price"]), float(entry["size"])))
            except (KeyError, ValueError):
                pass
        return result

    return Orderbook(
        token_id=token_id,
        bids=parse_side(data.get("bids", [])),
        asks=parse_side(data.get("asks", [])),
    )


async def scan_neg_risk(
    client: httpx.AsyncClient,
    markets: list[Market],
    threshold: float = 0.97,
    min_depth_usdc: float = 5.0,
    max_position_usdc: float = 50.0,
) -> list[ArbOpportunity]:
    """
    Scan markets for neg-risk arbitrage opportunities.

    threshold: buy both if YES_ask + NO_ask < threshold
               default 0.97 means ≥3% profit after fees
    """
    opportunities = []

    async def check_market(market: Market):
        ob_yes, ob_no = await asyncio.gather(
            get_orderbook(client, market.yes_token_id),
            get_orderbook(client, market.no_token_id),
        )
        if ob_yes is None or ob_no is None:
            return

        yes_ask = ob_yes.best_ask()
        no_ask = ob_no.best_ask()

        if yes_ask <= 0 or yes_ask >= 1 or no_ask <= 0 or no_ask >= 1:
            return

        total_cost = yes_ask + no_ask

        # Account for fees (buying both sides = 2 taker fills)
        effective_cost = total_cost * (1 + TAKER_FEE)

        if effective_cost >= threshold:
            return

        profit_pct = (1.0 - effective_cost) * 100

        # Depth check: available size at the best ask
        yes_depth = ob_yes.depth_at(yes_ask, "ask") * yes_ask
        no_depth = ob_no.depth_at(no_ask, "ask") * no_ask

        if yes_depth < min_depth_usdc or no_depth < min_depth_usdc:
            return

        max_size = min(yes_depth, no_depth, max_position_usdc)

        opp = ArbOpportunity(
            market=market,
            strategy="neg_risk",
            yes_ask=yes_ask,
            no_ask=no_ask,
            total_cost=effective_cost,
            profit_pct=profit_pct,
            yes_depth=yes_depth,
            no_depth=no_depth,
            max_size_usdc=max_size,
        )
        opportunities.append(opp)
        logger.info(f"ARB FOUND: {opp}")

    await asyncio.gather(*[check_market(m) for m in markets])
    return sorted(opportunities, key=lambda o: o.profit_pct, reverse=True)
