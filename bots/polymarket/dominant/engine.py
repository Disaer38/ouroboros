"""
TradingEngine — main async loop for the Dominant Side paper trader.

Orchestrates:
  - MarketWatcher: detects new 5-minute rounds
  - ClobClient: fetches live orderbooks
  - SignalGenerator: computes BUY/NO_SIGNAL
  - PaperExchange: executes virtual trades
  - Resolution: polls Gamma API to determine winners

Loop:
  1. For each configured asset, get the current active market.
  2. Fetch YES + NO orderbooks concurrently.
  3. Generate signal.
  4. If actionable → exchange.buy().
  5. Check for markets that have expired and poll for resolution.
  6. Sleep scan_interval_sec.
  7. Repeat.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..clob_client import ClobClient
from ..market_discovery import ActiveMarket, get_active_market
from .config import DominantConfig
from .exchange import DominantPosition, PaperExchange
from .signal import SignalGenerator

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


class TradingEngine:
    """
    Main engine for Dominant Side paper trading.

    Usage:
        config = DominantConfig()
        exchange = PaperExchange(config)
        engine = TradingEngine(config, exchange)
        await engine.run()
    """

    def __init__(
        self,
        config: DominantConfig,
        exchange: PaperExchange,
        stop_event: Optional[asyncio.Event] = None,
    ):
        self.config = config
        self.exchange = exchange
        self.signal_gen = SignalGenerator(config)
        self.stop_event = stop_event or asyncio.Event()

        # Track current active market per asset
        self._markets: dict[str, Optional[ActiveMarket]] = {a: None for a in config.assets}

        # Positions waiting for resolution: market_id → position
        self._pending_resolution: dict[str, DominantPosition] = {}

        # Stats
        self.scan_count: int = 0
        self.signal_count: int = 0

    async def run(self) -> None:
        """Main engine loop. Runs until stop_event is set."""
        logger.info(
            f"TradingEngine starting | assets={self.config.assets} "
            f"threshold={self.config.min_dominant_prob} "
            f"bet=${self.config.bet_size_usdc}"
        )

        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as http:
            async with ClobClient() as clob:
                while not self.stop_event.is_set():
                    scan_start = time.monotonic()
                    self.scan_count += 1

                    # 1. Process each asset
                    await asyncio.gather(*[
                        self._process_asset(asset, http, clob)
                        for asset in self.config.assets
                    ])

                    # 2. Check for expired positions ready to resolve
                    await self._check_resolutions(http)

                    # 3. Sleep until next scan
                    elapsed = time.monotonic() - scan_start
                    sleep_sec = max(0.0, self.config.scan_interval_sec - elapsed)
                    try:
                        await asyncio.wait_for(
                            self.stop_event.wait(),
                            timeout=sleep_sec,
                        )
                    except asyncio.TimeoutError:
                        pass

        logger.info("TradingEngine stopped.")

    async def _process_asset(
        self,
        asset: str,
        http: httpx.AsyncClient,
        clob: ClobClient,
    ) -> None:
        """Fetch market data for one asset and generate/execute signals."""
        try:
            # Refresh active market
            market = await get_active_market(asset, http)
            self._markets[asset] = market

            if market is None:
                logger.debug(f"No active market for {asset.upper()}")
                return

            # Skip if already traded this market
            if market.market_id in self.exchange._traded_market_ids:
                logger.debug(f"Already traded {asset.upper()} market {market.market_id}")
                return

            # Fetch both orderbooks concurrently
            ob_yes, ob_no = await clob.get_both_books(market.yes_token_id, market.no_token_id)

            # Generate signal
            signal = self.signal_gen.generate(market, ob_yes, ob_no)

            if not signal.is_actionable:
                logger.debug(f"{signal}")
                return

            self.signal_count += 1

            # Execute trade
            trade = self.exchange.buy(signal, market, asset)
            if trade is not None:
                # Track for resolution
                pos = self.exchange.positions.get(signal.market_id)
                if pos:
                    self._pending_resolution[signal.market_id] = pos

        except Exception as e:
            logger.warning(f"Error processing {asset}: {e}")

    async def _check_resolutions(self, http: httpx.AsyncClient) -> None:
        """Check if any pending positions have expired and can be resolved."""
        now = datetime.now(timezone.utc)
        to_resolve = []

        for market_id, pos in list(self._pending_resolution.items()):
            if pos.expiry is None:
                continue
            # Wait for expiry + buffer before resolving
            time_since_expiry = (now - pos.expiry).total_seconds()
            if time_since_expiry >= self.config.resolution_poll_delay_sec:
                to_resolve.append((market_id, pos))

        for market_id, pos in to_resolve:
            await self._resolve_position(market_id, pos, http)

    async def _resolve_position(
        self,
        market_id: str,
        pos: DominantPosition,
        http: httpx.AsyncClient,
    ) -> None:
        """
        Attempt to resolve a position using Gamma API.

        Strategy:
        1. Query Gamma for market resolution.
        2. If resolved: call exchange.resolve(market_id, won=...).
        3. If not yet resolved: retry up to max_retries times.
        4. Fallback: assume based on last known price direction.
        """
        for attempt in range(self.config.resolution_max_retries):
            try:
                won = await self._fetch_resolution(http, market_id, pos)
                if won is not None:
                    self.exchange.resolve(market_id, won)
                    self._pending_resolution.pop(market_id, None)
                    return
                else:
                    # Not yet resolved — wait and retry
                    logger.info(
                        f"Market {market_id[:12]} not yet resolved "
                        f"(attempt {attempt+1}/{self.config.resolution_max_retries})"
                    )
                    await asyncio.sleep(30.0)
            except Exception as e:
                logger.warning(f"Resolution fetch error for {market_id}: {e}")
                await asyncio.sleep(10.0)

        # Fallback: market not resolved after retries
        # Use a conservative assumption: we lost (worst case)
        logger.warning(
            f"Could not resolve market {market_id[:12]} after "
            f"{self.config.resolution_max_retries} retries. Marking as loss."
        )
        self.exchange.resolve(market_id, won=False)
        self._pending_resolution.pop(market_id, None)

    async def _fetch_resolution(
        self,
        http: httpx.AsyncClient,
        market_id: str,
        pos: DominantPosition,
    ) -> Optional[bool]:
        """
        Fetch market resolution from Gamma API.

        Returns:
            True  = our side won
            False = our side lost
            None  = market not yet resolved
        """
        try:
            resp = await http.get(
                f"{GAMMA_API}/markets/{market_id}",
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

            # Check if resolved
            resolved = data.get("resolved") or data.get("isResolved") or False
            if not resolved:
                return None

            # Get outcome
            # Gamma returns: outcome = "Yes" | "No" | null
            outcome = data.get("outcome") or data.get("resolution") or ""
            if not outcome:
                return None

            outcome = outcome.strip().upper()

            # Determine if WE won
            if pos.side.value == "YES":
                won = outcome in ("YES", "TRUE", "1")
            else:  # pos.side == NO
                won = outcome in ("NO", "FALSE", "0")

            logger.info(
                f"Market resolved: {pos.market_question[:50]} "
                f"outcome={outcome} our_side={pos.side.value} won={won}"
            )
            return won

        except Exception as e:
            logger.debug(f"Gamma resolution fetch failed: {e}")
            return None

    def status(self) -> dict:
        """Return current engine status."""
        return {
            "scan_count": self.scan_count,
            "signal_count": self.signal_count,
            "pending_resolution": len(self._pending_resolution),
            "active_markets": {
                asset: str(m) if m else None
                for asset, m in self._markets.items()
            },
            **self.exchange.summary(),
        }