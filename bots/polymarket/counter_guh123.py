"""
Counter-guh123 Market Maker Bot
================================
Strategy: Act as liquidity provider (maker) targeting the exact markets
where guh123 operates as a taker.

Reverse-engineered profile:
  guh123 buys BOTH Up and Down on BTC/ETH 5m/15m markets.
  It's a taker — sweeps existing limit orders.
  Average fill prices: Up @ 0.30-0.66, Down @ 0.33-0.70
  Entry window: first 5 minutes after market opens.

Our edge:
  Post limit orders on both sides at a slight premium.
  guh123 will buy from us, paying our ask.
  We capture the bid-ask spread without directional risk.

Spread target:
  Post Up @ market_fair + 0.02 (we sell Up to guh123)
  Post Down @ market_fair + 0.02 (we sell Down to guh123)
  Total capital at risk: 0 (both sides cancel out for neg-risk portion)
"""

import asyncio
import aiohttp
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# --- Config ---
TARGET_ADDRESS = "0xa45fe11dd1420fca906ceac2c067844379a42429"  # guh123
POLL_INTERVAL = 3.0       # seconds between guh123 activity checks
SPREAD_BPS = 200          # 2% spread above fair price (200 basis points)
MAX_ORDER_SIZE = 20.0     # USDC per side per market
ARB_THRESHOLD = 0.99      # only post when sum < 0.99 (arb exists)
ENTRY_WINDOW = 300        # seconds: only post in first 5min of market
PAPER_MODE = True         # always start in paper mode


@dataclass
class MarketState:
    condition_id: str
    slug: str
    opens_at: int           # unix timestamp
    up_price: float = 0.5
    down_price: float = 0.5
    up_posted: bool = False
    down_posted: bool = False
    total_pnl: float = 0.0
    fills_up: list = field(default_factory=list)
    fills_down: list = field(default_factory=list)


class CounterGuh123Bot:
    """
    Detects guh123's activity → posts limit orders ahead of its sweeps.
    
    In paper mode: simulates fills using next observed trade prices.
    In live mode: requires CLOB API credentials.
    """

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self.markets: dict[str, MarketState] = {}
        self.guh123_recent: deque = deque(maxlen=50)
        self.pnl_total = 0.0
        self.fills_count = 0
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        async with aiohttp.ClientSession() as session:
            self.session = session
            logger.info("CounterGuh123 bot started (paper=%s)", self.paper_mode)
            await asyncio.gather(
                self._watch_guh123_loop(),
                self._market_maker_loop(),
                self._report_loop(),
            )

    # ------------------------------------------------------------------ #
    #  Guh123 Watcher                                                      #
    # ------------------------------------------------------------------ #

    async def _watch_guh123_loop(self):
        """Poll guh123's recent activity. When a new trade appears, record
        which market it entered — that's where we want to post orders."""
        last_ts = int(time.time()) - 60
        url = f"https://data-api.polymarket.com/activity"
        params = {"user": TARGET_ADDRESS, "limit": 50}

        while True:
            try:
                async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
                if isinstance(data, list):
                    new_trades = [t for t in data if t.get("timestamp", 0) > last_ts]
                    if new_trades:
                        logger.info("guh123: %d new trades detected", len(new_trades))
                        for trade in new_trades:
                            await self._on_guh123_trade(trade)
                        last_ts = max(t["timestamp"] for t in new_trades)
            except Exception as e:
                logger.warning("Watch loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    async def _on_guh123_trade(self, trade: dict):
        """Called every time guh123 makes a new trade."""
        cid = trade.get("conditionId", "")
        slug = trade.get("slug", cid[:20])
        ts = trade.get("timestamp", int(time.time()))

        self.guh123_recent.append(trade)

        if cid not in self.markets:
            # New market guh123 entered — register it
            self.markets[cid] = MarketState(
                condition_id=cid,
                slug=slug,
                opens_at=ts,
            )
            logger.info("New guh123 market: %s", slug)

        # Update price estimate from observed trade
        state = self.markets[cid]
        outcome = trade.get("outcome", "")
        price = float(trade.get("price", 0.5))
        if outcome == "Up":
            state.up_price = price
        elif outcome == "Down":
            state.down_price = price

    # ------------------------------------------------------------------ #
    #  Market Maker Loop                                                   #
    # ------------------------------------------------------------------ #

    async def _market_maker_loop(self):
        """Post limit orders on both sides of markets where guh123 is active."""
        while True:
            for cid, state in list(self.markets.items()):
                await self._post_orders_for_market(state)
            await asyncio.sleep(POLL_INTERVAL)

    async def _post_orders_for_market(self, state: MarketState):
        now = int(time.time())
        age = now - state.opens_at

        # Only operate within the entry window
        if age > ENTRY_WINDOW:
            return

        price_sum = state.up_price + state.down_price
        if price_sum >= ARB_THRESHOLD:
            logger.debug("Market %s: sum=%.4f >= threshold, skip", state.slug, price_sum)
            return

        # Our ask prices: slightly above current market
        spread = SPREAD_BPS / 10000.0  # 0.02
        ask_up = min(state.up_price + spread, 0.99)
        ask_down = min(state.down_price + spread, 0.99)

        if state.up_price + state.down_price + 2 * spread >= 1.0:
            # Our asks would eliminate the arb — reduce spread
            spread = (1.0 - price_sum) / 4  # half the available edge
            ask_up = state.up_price + spread
            ask_down = state.down_price + spread

        our_sum = ask_up + ask_down

        logger.info(
            "Posting: %s | Up@%.3f Down@%.3f | sum=%.4f | edge=%.2f%%",
            state.slug, ask_up, ask_down, our_sum, 100 * (1 - price_sum)
        )

        if self.paper_mode:
            await self._simulate_fill(state, ask_up, ask_down)
        else:
            await self._place_live_orders(state, ask_up, ask_down)

    # ------------------------------------------------------------------ #
    #  Paper Simulation                                                    #
    # ------------------------------------------------------------------ #

    async def _simulate_fill(self, state: MarketState, ask_up: float, ask_down: float):
        """
        In paper mode: assume guh123 will hit our asks on the next sweep.
        We model the fill as: we sold Up@ask_up and Down@ask_down.
        
        P&L per pair of shares sold:
          Revenue = ask_up + ask_down
          Cost = 1.0 (we must deliver 1 share, one side wins)
          Net = (ask_up + ask_down) - 1.0  [per share pair]
        
        But we need to balance our exposure:
          If Up wins: we owe 1 Up share → we need to buy it at market
          If Down wins: we owe 1 Down share → we need to buy it at market
          
        For neg-risk: sell Up + sell Down → when Up wins, receive 1.0, pay 0.
          After selling both sides at ask, our portfolio is:
          - Received: ask_up + ask_down (cash)
          - Will owe: max(1, 0) = 1 when outcome resolves
          
        Simplified: paper pnl = (ask_up + ask_down) - 1.0 per "round trip"
        """
        order_size = min(MAX_ORDER_SIZE, 10.0)  # cap at $10 per side
        shares_up = order_size / ask_up
        shares_down = order_size / ask_down

        # Assume guh123 fills a fraction of our order each sweep
        # (it buys available liquidity; we assume 50% fill rate in paper mode)
        fill_rate = 0.5
        filled_up = shares_up * fill_rate
        filled_down = shares_down * fill_rate
        locked = min(filled_up, filled_down)

        # Guaranteed return on locked shares
        revenue = locked * 1.0
        cost = locked * (ask_up + ask_down)
        pnl = revenue - cost

        state.total_pnl += pnl
        self.pnl_total += pnl
        self.fills_count += 1

        logger.info(
            "[PAPER] Fill %s: locked=%.2f shares, pnl=$%.4f (cumulative: $%.4f)",
            state.slug[:30], locked, pnl, self.pnl_total
        )

    # ------------------------------------------------------------------ #
    #  Live Order Placement (stub)                                         #
    # ------------------------------------------------------------------ #

    async def _place_live_orders(self, state: MarketState, ask_up: float, ask_down: float):
        """
        Live mode: place limit orders via Polymarket CLOB API.
        Requires: API key, private key, proxy credentials.
        See: bots/polymarket/clob_client.py
        """
        raise NotImplementedError(
            "Live mode not yet implemented. "
            "Integrate with CLOBClient from clob_client.py. "
            "Required: POST /order with side=SELL, price=ask_price, size=order_size"
        )

    # ------------------------------------------------------------------ #
    #  Reporting                                                           #
    # ------------------------------------------------------------------ #

    async def _report_loop(self):
        while True:
            await asyncio.sleep(60)
            logger.info(
                "=== STATUS === | Markets tracked: %d | Fills: %d | Total P&L: $%.4f",
                len(self.markets), self.fills_count, self.pnl_total
            )
            for cid, state in self.markets.items():
                logger.info(
                    "  %s | pnl=$%.4f | up=%.3f dn=%.3f",
                    state.slug[:35], state.total_pnl, state.up_price, state.down_price
                )


# ------------------------------------------------------------------ #
#  Entry Point                                                         #
# ------------------------------------------------------------------ #

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    bot = CounterGuh123Bot(paper_mode=PAPER_MODE)
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
