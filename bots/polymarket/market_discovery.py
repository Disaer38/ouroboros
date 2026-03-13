"""
Dynamic market discovery for Polymarket 5-minute Up/Down markets.

Key insight: 5m markets rotate every 5 minutes. Token IDs change with each round.
This module provides:
  - get_active_market(asset)  — fetch current active market for an asset
  - MarketWatcher             — background loop that detects market rotations

Assets supported: "btc", "eth", "sol", "xrp"
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# Series slugs for 5-minute markets (confirmed via Gamma API)
SERIES_SLUGS = {
    "btc": "btc-up-or-down-5m",
    "eth": "eth-up-or-down-5m",
    "sol": "sol-up-or-down-5m",
    "xrp": "xrp-up-or-down-5m",
}


@dataclass
class ActiveMarket:
    """Represents a currently active 5-minute Up/Down market."""
    asset: str                 # "btc", "eth", etc.
    market_id: str             # Gamma market ID (numeric string)
    condition_id: str          # 0x... hex ID
    question: str              # e.g. "Will BTC be higher at 1:05PM than 1:00PM?"
    yes_token_id: str          # CLOB token for YES
    no_token_id: str           # CLOB token for NO
    end_date: datetime         # When this round expires
    series_slug: str           # "btc-up-or-down-5m"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def seconds_to_expiry(self) -> float:
        return max(0.0, (self.end_date - datetime.now(timezone.utc)).total_seconds())

    @property
    def is_expired(self) -> bool:
        return self.seconds_to_expiry == 0.0

    def __str__(self) -> str:
        ttl = self.seconds_to_expiry
        return f"[{self.asset.upper()} 5m] {self.question[:60]} | TTL={ttl:.0f}s"


def _parse_active_market(asset: str, data: dict) -> Optional[ActiveMarket]:
    """Parse a single market dict from Gamma API into ActiveMarket."""
    import json as _json

    try:
        # Token IDs
        tids_raw = data.get("clobTokenIds") or data.get("clob_token_ids")
        if isinstance(tids_raw, str):
            tids = _json.loads(tids_raw)
        elif isinstance(tids_raw, list):
            tids = tids_raw
        else:
            return None

        if not tids or len(tids) < 2:
            return None

        end_str = data.get("endDate") or data.get("end_date_iso") or ""
        if not end_str:
            return None
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

        # Skip expired
        if end_dt < datetime.now(timezone.utc):
            return None

        question = data.get("question") or data.get("title") or ""
        if not question:
            return None

        condition_id = data.get("conditionId") or data.get("condition_id") or ""
        market_id = str(data.get("id") or data.get("market_id") or "")

        return ActiveMarket(
            asset=asset,
            market_id=market_id,
            condition_id=condition_id,
            question=question,
            yes_token_id=str(tids[0]),
            no_token_id=str(tids[1]),
            end_date=end_dt,
            series_slug=SERIES_SLUGS.get(asset, ""),
        )

    except Exception as e:
        logger.debug(f"Failed to parse market for {asset}: {e}")
        return None


async def get_active_market(
    asset: str = "btc",
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[ActiveMarket]:
    """
    Fetch the currently active 5-minute market for a given asset.

    Args:
        asset: "btc", "eth", "sol", or "xrp"
        client: Optional httpx.AsyncClient (creates one if not provided)

    Returns:
        ActiveMarket if found, None if no active market (outside trading hours)
    """
    asset = asset.lower()
    series_slug = SERIES_SLUGS.get(asset)
    if not series_slug:
        raise ValueError(f"Unknown asset '{asset}'. Supported: {list(SERIES_SLUGS)}")

    async def _fetch(c: httpx.AsyncClient) -> Optional[ActiveMarket]:
        # Strategy: query the events API for the active series
        # The series slug for events is different from market slug
        # We search for active markets with the right slug pattern
        resp = await c.get(
            f"{GAMMA_API}/markets",
            params={
                "slug": series_slug,   # exact series slug match
                "active": "true",
                "closed": "false",
                "limit": 5,
                "order": "endDate",
                "ascending": "true",   # earliest expiry first = most current round
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        markets = resp.json()

        if not markets:
            # Fallback: search by question text
            logger.debug(f"Slug search returned nothing for {asset}, trying question search")
            resp2 = await c.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 50,
                    "order": "volume24hr",
                },
                timeout=10.0,
            )
            resp2.raise_for_status()
            all_markets = resp2.json()
            asset_upper = asset.upper()
            markets = [
                m for m in all_markets
                if asset_upper in (m.get("question") or "").upper()
                and "UP OR DOWN" in (m.get("question") or "").upper()
                and "5" in (m.get("question") or "")
            ]

        if not markets:
            logger.info(f"No active 5m market found for {asset.upper()}")
            return None

        # Pick the soonest-expiring (= current round)
        for market_data in markets:
            m = _parse_active_market(asset, market_data)
            if m and not m.is_expired:
                logger.info(f"Found active market: {m}")
                return m

        return None

    if client is not None:
        return await _fetch(client)

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as c:
        return await _fetch(c)


async def get_active_markets(
    assets: list[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Optional[ActiveMarket]]:
    """
    Fetch active 5m markets for multiple assets concurrently.

    Returns:
        dict mapping asset → ActiveMarket (or None if not active)
    """
    if assets is None:
        assets = ["btc", "eth", "sol", "xrp"]

    async def _fetch_one(asset: str) -> tuple[str, Optional[ActiveMarket]]:
        m = await get_active_market(asset, client)
        return asset, m

    if client is not None:
        results = await asyncio.gather(*[_fetch_one(a) for a in assets])
    else:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as c:
            results = await asyncio.gather(*[
                get_active_market(a, c) for a in assets
            ])
            results = list(zip(assets, results))

    return dict(results)


class MarketWatcher:
    """
    Background task that monitors for market rotations.

    Polls every `poll_interval` seconds and calls `on_rotation` when
    a new market becomes active (i.e. the token_id changes).

    Usage:
        async def handle_new_market(market: ActiveMarket):
            print(f"New market: {market}")

        watcher = MarketWatcher("btc", on_rotation=handle_new_market)
        await watcher.start()
        # ... run your bot ...
        await watcher.stop()
    """

    def __init__(
        self,
        asset: str = "btc",
        on_rotation: Optional[Callable[[ActiveMarket], None]] = None,
        poll_interval: float = 15.0,   # seconds between checks
    ):
        self.asset = asset
        self.on_rotation = on_rotation
        self.poll_interval = poll_interval

        self._current: Optional[ActiveMarket] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def current(self) -> Optional[ActiveMarket]:
        return self._current

    async def start(self) -> None:
        """Start background polling."""
        # Fetch initial market immediately
        self._current = await get_active_market(self.asset)
        if self._current:
            logger.info(f"MarketWatcher started: {self._current}")
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop background polling."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                new_market = await get_active_market(self.asset)
                if new_market and self._is_rotation(new_market):
                    logger.info(
                        f"Market rotation detected for {self.asset.upper()}: "
                        f"{self._current.market_id if self._current else 'None'} "
                        f"→ {new_market.market_id}"
                    )
                    self._current = new_market
                    if self.on_rotation:
                        # Support both sync and async callbacks
                        if asyncio.iscoroutinefunction(self.on_rotation):
                            await self.on_rotation(new_market)
                        else:
                            self.on_rotation(new_market)
            except Exception as e:
                logger.warning(f"MarketWatcher poll error for {self.asset}: {e}")

    def _is_rotation(self, new_market: ActiveMarket) -> bool:
        """Check if this is genuinely a new market round."""
        if self._current is None:
            return True
        return new_market.market_id != self._current.market_id


# ---------------------------------------------------------------------------
# Quick test / CLI
# ---------------------------------------------------------------------------

async def _demo() -> None:
    """Quick smoke test — print active markets for all assets."""
    print("Fetching active 5m markets...\n")
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        for asset in ["btc", "eth", "sol", "xrp"]:
            m = await get_active_market(asset, client)
            if m:
                print(f"✅ {asset.upper()}: {m.question}")
                print(f"   YES token: {m.yes_token_id}")
                print(f"   NO  token: {m.no_token_id}")
                print(f"   Expires in: {m.seconds_to_expiry:.0f}s")
            else:
                print(f"❌ {asset.upper()}: no active market (outside trading hours?)")
            print()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_demo())
