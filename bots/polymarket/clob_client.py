"""
Polymarket CLOB client — HTTP + WebSocket.

Responsibilities:
  - Orderbook snapshots (REST)
  - Live orderbook via WebSocket subscription
  - Order placement / cancellation (authenticated)

Does NOT contain trading strategy logic.

Usage (read-only, no auth):
    client = ClobClient()
    ob = await client.get_orderbook(token_id)
    print(ob.mid_price)

Usage (authenticated):
    client = ClobClient(private_key="0x...", api_key="...", ...)
    resp = await client.place_limit_order(token_id, side="BUY", price=0.55, size=10.0)
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

CLOB_HTTP = "https://clob.polymarket.com"
CLOB_WS   = "wss://clob.polymarket.com/ws"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Level:
    price: float
    size: float   # in USDC (for asks) or shares * price


@dataclass
class Orderbook:
    token_id: str
    bids: list[Level]
    asks: list[Level]
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        """Arithmetic midpoint between best bid and ask."""
        b, a = self.best_bid, self.best_ask
        if b == 0.0 or a == 1.0:
            return 0.5
        return (b + a) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    def depth_usdc(self, side: str, levels: int = 3) -> float:
        """Total USDC depth across top N price levels on one side."""
        book = self.bids if side == "bid" else self.asks
        return sum(lv.price * lv.size for lv in book[:levels])


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class ClobClient:
    """
    Thin async HTTP wrapper around the Polymarket CLOB API.

    Can be used as an async context manager:
        async with ClobClient() as c:
            ob = await c.get_orderbook(token_id)

    Or manually:
        c = ClobClient()
        await c.open()
        ob = await c.get_orderbook(token_id)
        await c.close()
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        proxy_url: Optional[str] = None,
    ):
        self._timeout = timeout
        self._proxy_url = proxy_url
        self._http: Optional[httpx.AsyncClient] = None

    async def open(self) -> None:
        kwargs: dict = {"timeout": self._timeout, "follow_redirects": True}
        if self._proxy_url:
            kwargs["proxies"] = self._proxy_url
        self._http = httpx.AsyncClient(**kwargs)

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "ClobClient":
        await self.open()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("ClobClient not opened. Use 'async with ClobClient()' or call open().")
        return self._http

    # ------------------------------------------------------------------
    # Public read-only endpoints
    # ------------------------------------------------------------------

    async def get_orderbook(self, token_id: str) -> Orderbook:
        """Fetch current orderbook snapshot for a single token."""
        resp = await self._client().get(
            f"{CLOB_HTTP}/book",
            params={"token_id": token_id},
        )
        resp.raise_for_status()
        data = resp.json()

        def _parse(raw: list) -> list[Level]:
            return [Level(float(x["price"]), float(x["size"])) for x in raw]

        return Orderbook(
            token_id=token_id,
            bids=_parse(data.get("bids", [])),
            asks=_parse(data.get("asks", [])),
        )

    async def get_mid_price(self, token_id: str) -> float:
        """Convenience: return mid-price for a token."""
        ob = await self.get_orderbook(token_id)
        return ob.mid_price

    async def get_both_books(
        self, yes_token_id: str, no_token_id: str
    ) -> tuple[Orderbook, Orderbook]:
        """Fetch YES and NO orderbooks concurrently."""
        return await asyncio.gather(
            self.get_orderbook(yes_token_id),
            self.get_orderbook(no_token_id),
        )

    async def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Fetch last trade price for a token (from CLOB trade history)."""
        try:
            resp = await self._client().get(
                f"{CLOB_HTTP}/last-trade-price",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()
            price_str = data.get("price")
            return float(price_str) if price_str else None
        except Exception as e:
            logger.debug(f"last-trade-price failed for {token_id}: {e}")
            return None


# ---------------------------------------------------------------------------
# WebSocket live book
# ---------------------------------------------------------------------------

class LiveOrderbook:
    """
    Maintains a live local copy of an orderbook via CLOB WebSocket.

    Usage:
        live = LiveOrderbook(token_id)
        await live.connect()
        # ... later ...
        ob = live.snapshot()
        await live.disconnect()

    Or with callback:
        async def on_update(ob: Orderbook):
            print(ob.mid_price)

        live = LiveOrderbook(token_id, on_update=on_update)
        await live.connect()
    """

    def __init__(
        self,
        token_id: str,
        on_update: Optional[Callable[[Orderbook], None]] = None,
    ):
        self.token_id = token_id
        self._on_update = on_update
        self._bids: dict[float, float] = {}   # price → size
        self._asks: dict[float, float] = {}
        self._task: Optional[asyncio.Task] = None
        self._connected = False

    def snapshot(self) -> Orderbook:
        """Return current local book state as an Orderbook."""
        bids = sorted(
            [Level(p, s) for p, s in self._bids.items() if s > 0],
            key=lambda l: -l.price,
        )
        asks = sorted(
            [Level(p, s) for p, s in self._asks.items() if s > 0],
            key=lambda l: l.price,
        )
        return Orderbook(token_id=self.token_id, bids=bids, asks=asks)

    async def connect(self) -> None:
        """Start background WebSocket listener."""
        self._task = asyncio.create_task(self._listen())
        self._connected = True

    async def disconnect(self) -> None:
        """Stop listener."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._connected = False

    async def _listen(self) -> None:
        """Internal WebSocket loop with reconnection."""
        import websockets  # optional dep — only needed for live mode

        subscribe_msg = json.dumps({
            "auth": {},
            "markets": [],
            "assets_ids": [self.token_id],
            "type": "Market",
        })

        while True:
            try:
                logger.info(f"WS connecting for token {self.token_id[:16]}...")
                async with websockets.connect(CLOB_WS, ping_interval=30) as ws:
                    await ws.send(subscribe_msg)
                    logger.info("WS subscribed")

                    async for raw in ws:
                        try:
                            self._handle_message(json.loads(raw))
                        except Exception as e:
                            logger.debug(f"WS message parse error: {e}")

            except asyncio.CancelledError:
                logger.info("WS listener cancelled")
                break
            except Exception as e:
                logger.warning(f"WS error: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)

    def _handle_message(self, msg: dict) -> None:
        """Process a single WS message and update local book."""
        event_type = msg.get("event_type")

        if event_type == "book":
            # Full snapshot
            self._bids.clear()
            self._asks.clear()
            for lv in msg.get("bids", []):
                self._bids[float(lv["price"])] = float(lv["size"])
            for lv in msg.get("asks", []):
                self._asks[float(lv["price"])] = float(lv["size"])

        elif event_type in ("price_change", "tick_size_change"):
            # Incremental update
            for lv in msg.get("changes", []):
                price = float(lv["price"])
                size = float(lv["size"])
                side = lv.get("side", "").lower()
                if side == "buy":
                    if size == 0:
                        self._bids.pop(price, None)
                    else:
                        self._bids[price] = size
                elif side == "sell":
                    if size == 0:
                        self._asks.pop(price, None)
                    else:
                        self._asks[price] = size

        else:
            return  # ignore other event types

        if self._on_update:
            self._on_update(self.snapshot())
