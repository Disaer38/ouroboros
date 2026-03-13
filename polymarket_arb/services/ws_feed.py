"""
WebSocket Feed — real-time orderbook via Polymarket CLOB market channel.

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
Protocol:
  1. Connect
  2. Send subscription message: {"type": "market", "assets_ids": [token_ids...], "custom_feature_enabled": true}
  3. Receive events: book (snapshot) | price_change | last_trade_price | tick_size_change
  4. PING every 10s, expect PONG

Best ask for each token is tracked and updated on every event.
When any ask changes, check all active neg-risk pairs for opportunity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from core.models import MarketMeta, Opportunity
from strategy.neg_risk import NegativeRiskStrategy

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10  # seconds


class TopOfBook:
    """Tracks best bid/ask for a single token."""
    __slots__ = ("best_bid", "best_ask", "last_update")

    def __init__(self):
        self.best_bid: Optional[Decimal] = None
        self.best_ask: Optional[Decimal] = None
        self.last_update: float = 0.0

    def update_from_book(self, data: dict):
        """Process full book snapshot."""
        asks = data.get("asks", [])
        bids = data.get("bids", [])
        if asks:
            # asks sorted ascending by price — first is best ask
            self.best_ask = Decimal(str(min(float(a["price"]) for a in asks)))
        if bids:
            self.best_bid = Decimal(str(max(float(b["price"]) for b in bids)))
        self.last_update = time.time()

    def update_from_price_change(self, data: dict):
        """Process incremental price_change event."""
        # best_bid / best_ask fields provided directly in price_change
        ba = data.get("best_ask")
        bb = data.get("best_bid")
        if ba:
            self.best_ask = Decimal(str(ba))
        if bb:
            self.best_bid = Decimal(str(bb))
        # Also process price_changes array for order removals
        for change in data.get("price_changes", []):
            price = Decimal(str(change["price"]))
            size = Decimal(str(change["size"]))
            side = change.get("side", "").upper()
            if size == 0:
                # Order removed — if it was best, recalculate needed (but best_ask above covers it)
                pass
        self.last_update = time.time()


class WSFeed:
    """
    WebSocket-based real-time feed for multiple token pairs.

    Usage:
        feed = WSFeed(markets, on_opportunity=callback)
        await feed.run()
    """

    def __init__(
        self,
        markets: list[MarketMeta],
        on_opportunity: Callable[[Opportunity], None],
        strategy: Optional[NegativeRiskStrategy] = None,
    ):
        self.markets = markets
        self.on_opportunity = on_opportunity
        self.strategy = strategy or NegativeRiskStrategy()

        # token_id -> TopOfBook
        self._books: dict[str, TopOfBook] = {}
        # token_id -> MarketMeta (both up and down map to same market)
        self._token_to_market: dict[str, MarketMeta] = {}

        self._opportunities_seen = 0
        self._events_processed = 0
        self._running = False

        # Pre-populate
        for m in markets:
            self._books[m.up_token_id] = TopOfBook()
            self._books[m.down_token_id] = TopOfBook()
            self._token_to_market[m.up_token_id] = m
            self._token_to_market[m.down_token_id] = m

    async def run(self):
        """Main loop — connects and reconnects on failure."""
        self._running = True
        reconnect_delay = 1.0

        while self._running:
            try:
                await self._connect_and_listen()
                reconnect_delay = 1.0  # reset on clean disconnect
            except ConnectionClosed as e:
                logger.warning(f"WS disconnected: {e}. Reconnecting in {reconnect_delay}s...")
            except Exception as e:
                logger.error(f"WS error: {e}. Reconnecting in {reconnect_delay}s...")

            if self._running:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)  # exponential backoff

    async def stop(self):
        self._running = False

    async def _connect_and_listen(self):
        """Single connection lifecycle."""
        all_token_ids = list(self._books.keys())
        logger.info(f"Connecting to WS: {len(all_token_ids)} tokens")

        async with websockets.connect(
            WS_URL,
            ping_interval=None,  # manual ping
            max_size=10 * 1024 * 1024,  # 10MB
        ) as ws:
            logger.info("WS connected")

            # Subscribe to all tokens, split into chunks of 100
            chunk_size = 100
            for i in range(0, len(all_token_ids), chunk_size):
                chunk = all_token_ids[i:i + chunk_size]
                sub_msg = json.dumps({
                    "type": "market",
                    "assets_ids": chunk,
                    "custom_feature_enabled": True,
                })
                await ws.send(sub_msg)
                logger.debug(f"Subscribed to {len(chunk)} tokens (chunk {i//chunk_size + 1})")

            # Concurrent: ping loop + message loop
            await asyncio.gather(
                self._ping_loop(ws),
                self._message_loop(ws),
            )

    async def _ping_loop(self, ws):
        """Send PING every 10 seconds."""
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.send("PING")
                logger.debug("Sent PING")
            except Exception:
                break

    async def _message_loop(self, ws):
        """Process incoming messages."""
        async for raw in ws:
            if raw == "PONG":
                logger.debug("Received PONG")
                continue

            try:
                msgs = json.loads(raw)
                # API can send a single object or a list
                if isinstance(msgs, dict):
                    msgs = [msgs]
                for msg in msgs:
                    self._handle_message(msg)
            except json.JSONDecodeError:
                logger.warning(f"Non-JSON message: {raw[:100]}")
            except Exception as e:
                logger.error(f"Error handling message: {e}", exc_info=True)

    def _handle_message(self, msg: dict):
        """Route message to appropriate handler."""
        event_type = msg.get("event_type", "")
        asset_id = msg.get("asset_id", "")

        if not asset_id or asset_id not in self._books:
            return

        book = self._books[asset_id]

        if event_type == "book":
            book.update_from_book(msg)
            self._check_pair(asset_id)
        elif event_type == "price_change":
            book.update_from_price_change(msg)
            self._check_pair(asset_id)
        elif event_type == "best_bid_ask":
            ba = msg.get("best_ask")
            bb = msg.get("best_bid")
            if ba:
                book.best_ask = Decimal(str(ba))
            if bb:
                book.best_bid = Decimal(str(bb))
            book.last_update = time.time()
            self._check_pair(asset_id)

        self._events_processed += 1

    def _check_pair(self, changed_token_id: str):
        """Check if a neg-risk opportunity exists for the market containing this token."""
        market = self._token_to_market.get(changed_token_id)
        if not market:
            return

        up_book = self._books.get(market.up_token_id)
        down_book = self._books.get(market.down_token_id)

        if not up_book or not down_book:
            return
        if up_book.best_ask is None or down_book.best_ask is None:
            return

        total = up_book.best_ask + down_book.best_ask
        if total < self.strategy.threshold:
            self._opportunities_seen += 1
            opp = self._build_opportunity(market, up_book, down_book)
            if opp:
                self.on_opportunity(opp)

    def _build_opportunity(
        self,
        market: MarketMeta,
        up_book: TopOfBook,
        down_book: TopOfBook,
    ) -> Optional[Opportunity]:
        """Build an Opportunity object from TopOfBook data."""
        try:
            # Use large size placeholder — WS doesn't give size at best_ask level
            placeholder_size = Decimal("100")
            return Opportunity(
                market=market,
                up_ask=up_book.best_ask,
                down_ask=down_book.best_ask,
                up_ask_size=placeholder_size,
                down_ask_size=placeholder_size,
            )
        except Exception as e:
            logger.error(f"Failed to build opportunity: {e}")
            return None

    @property
    def stats(self) -> dict:
        return {
            "events": self._events_processed,
            "opportunities": self._opportunities_seen,
            "tokens_tracked": len(self._books),
        }
