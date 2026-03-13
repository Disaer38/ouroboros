"""
MarketDataService — Phase 1 (REST polling).

Fetches active BTC/ETH 5m/15m markets from Gamma API,
then polls CLOB for orderbook snapshots.
"""
from __future__ import annotations
import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional
import httpx

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import GAMMA_API, CLOB_API, TARGET_SLUGS
from core.models import MarketMeta, OrderBook, PriceLevel

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._markets: list[MarketMeta] = []

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    # ── Market discovery ────────────────────────────────────────────

    async def fetch_active_markets(self) -> list[MarketMeta]:
        """Fetch currently active BTC/ETH 5m/15m markets from Gamma events API."""
        markets: list[MarketMeta] = []

        # Map: (tag_slug, slug_keyword) pairs — verified working 2026-03-13
        search_targets = [
            ("bitcoin",  "updown-5m"),
            ("ethereum", "updown-5m"),
            ("bitcoin",  "updown-15m"),
            ("ethereum", "updown-15m"),
        ]

        for tag_slug, keyword in search_targets:
            try:
                params = {
                    "tag_slug": tag_slug,
                    "limit": 20,
                    "order": "startDate",
                    "ascending": "false",
                }
                resp = await self._client.get(f"{GAMMA_API}/events", params=params)
                resp.raise_for_status()
                events = resp.json()

                for event in events:
                    event_slug = event.get("slug", "")
                    if keyword not in event_slug:
                        continue

                    # Each event has a list of markets (UP market + DOWN market)
                    for m in event.get("markets", []):
                        meta = self._parse_market(m)
                        if meta:
                            markets.append(meta)
                            logger.debug(
                                f"Found: {meta.slug} | "
                                f"UP={meta.up_token_id[:8]}... "
                                f"DOWN={meta.down_token_id[:8]}..."
                            )
                            break  # one MarketMeta per event (UP+DOWN pair)

            except Exception as e:
                logger.warning(f"Failed to fetch markets for tag={tag_slug} kw={keyword}: {e}")

        logger.info(f"Fetched {len(markets)} active markets")
        self._markets = markets
        return markets

    def _parse_market(self, m: dict) -> Optional[MarketMeta]:
        """Parse a Gamma API market object into MarketMeta."""
        try:
            # Skip inactive or closed markets
            if m.get("closed") == True or m.get("active") == False:
                return None

            # Gamma API returns clobTokenIds as a JSON string like '["tokenA","tokenB"]'
            import json

            tokens_raw = m.get("clobTokenIds")
            if isinstance(tokens_raw, str):
                tokens = json.loads(tokens_raw)
            elif isinstance(tokens_raw, list):
                tokens = tokens_raw
            else:
                logger.debug(f"No clobTokenIds for market {m.get('slug')}")
                return None

            if len(tokens) < 2:
                return None

            # Convention: tokens[0] = UP (YES), tokens[1] = DOWN (NO)
            # Verify by checking outcome labels if available
            outcomes_raw = m.get("outcomes")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except Exception:
                    outcomes = ["Up", "Down"]
            elif isinstance(outcomes_raw, list):
                outcomes = outcomes_raw
            else:
                outcomes = ["Up", "Down"]

            # Determine UP/DOWN token index
            up_idx, down_idx = 0, 1
            if len(outcomes) >= 2:
                up_label = str(outcomes[0]).lower()
                if "down" in up_label or "no" in up_label:
                    up_idx, down_idx = 1, 0

            return MarketMeta(
                condition_id=m.get("conditionId", ""),
                slug=m.get("slug", ""),
                question=m.get("question", ""),
                end_date_iso=m.get("endDate", ""),
                up_token_id=tokens[up_idx],
                down_token_id=tokens[down_idx],
                active=True,
            )
        except Exception as e:
            logger.warning(f"Failed to parse market {m.get('slug')}: {e}")
            return None

    # ── Orderbook fetching ───────────────────────────────────────────

    async def fetch_orderbook(self, token_id: str) -> Optional[OrderBook]:
        """Fetch current orderbook snapshot from CLOB REST API."""
        try:
            resp = await self._client.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse_orderbook(token_id, data)
        except Exception as e:
            logger.warning(f"Failed to fetch orderbook for {token_id[:12]}...: {e}")
            return None

    def _parse_orderbook(self, token_id: str, data: dict) -> OrderBook:
        def parse_levels(raw: list[dict]) -> list[PriceLevel]:
            levels = []
            for item in raw:
                try:
                    price = Decimal(str(item.get("price", "0")))
                    size  = Decimal(str(item.get("size", "0")))
                    if price > 0 and size > 0:
                        levels.append(PriceLevel(price=price, size=size))
                except InvalidOperation:
                    pass
            return levels

        bids = parse_levels(data.get("bids", []))
        asks = parse_levels(data.get("asks", []))
        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    async def fetch_both_books(self, market: MarketMeta) -> tuple[Optional[OrderBook], Optional[OrderBook]]:
        """Fetch UP and DOWN orderbooks concurrently."""
        up_book, down_book = await asyncio.gather(
            self.fetch_orderbook(market.up_token_id),
            self.fetch_orderbook(market.down_token_id),
        )
        return up_book, down_book
