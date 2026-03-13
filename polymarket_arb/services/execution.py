"""
ExecutionService — Phase 2.

Wraps py-clob-client to place and manage orders.
In simulation mode, logs trades without actually executing them.
In live mode, places real FOK orders via py-clob-client.
"""
from __future__ import annotations
import asyncio
import logging
import os
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass, field
import time

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    order_id: str
    token_id: str
    side: str          # "BUY"
    price: float
    size: float
    status: str        # "simulated" | "filled" | "open" | "failed"
    timestamp: float = field(default_factory=time.time)
    error: Optional[str] = None


@dataclass  
class TradeResult:
    """Result of executing both legs of a neg-risk trade."""
    up_order: OrderResult
    down_order: OrderResult
    simulated: bool = True
    
    @property
    def success(self) -> bool:
        return self.up_order.status != "failed" and self.down_order.status != "failed"
    
    @property
    def total_cost(self) -> float:
        return self.up_order.price * self.up_order.size + self.down_order.price * self.down_order.size


class ExecutionService:
    """
    Handles order placement for negative-risk arbitrage.
    
    Modes:
    - simulation=True (default): logs trades, no real orders
    - simulation=False: uses py-clob-client to place real FOK orders
    """
    
    def __init__(self, simulation: bool = True):
        self.simulation = simulation
        self._client = None
        self._trades_executed = 0
        self._total_profit_simulated = 0.0
        self._last_latency_ms: float = 0.0
        self._latency_samples: list[float] = []

        if not simulation:
            self._init_clob_client()
    
    def _init_clob_client(self):
        """Initialize py-clob-client with credentials from env."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            
            key = os.getenv("POLYMARKET_PRIVATE_KEY")
            if not key:
                raise ValueError("POLYMARKET_PRIVATE_KEY env var not set")
            
            # Polymarket uses Polygon (chain_id=137)
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=key,
                chain_id=137,
            )
            logger.info("CLOB client initialized (LIVE MODE)")
        except ImportError:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            raise
        except Exception as e:
            logger.error(f"Failed to init CLOB client: {e}")
            raise
    
    async def execute_neg_risk(
        self,
        market_slug: str,
        up_token_id: str,
        down_token_id: str,
        up_ask: Decimal,
        down_ask: Decimal,
        shares: Decimal,
    ) -> TradeResult:
        """
        Execute both legs of a negative-risk trade.
        
        Places FOK (Fill-Or-Kill) market orders on both sides simultaneously.
        Uses equal-shares sizing (same number of shares on UP and DOWN).
        """
        if self.simulation:
            return self._simulate_trade(
                market_slug, up_token_id, down_token_id,
                up_ask, down_ask, shares
            )
        else:
            return await self._execute_live(
                market_slug, up_token_id, down_token_id,
                up_ask, down_ask, shares
            )
    
    def _simulate_trade(
        self,
        market_slug: str,
        up_token_id: str,
        down_token_id: str,
        up_ask: Decimal,
        down_ask: Decimal,
        shares: Decimal,
    ) -> TradeResult:
        """Simulate a trade — log it, update counters, return fake result."""
        self._trades_executed += 1
        
        gross_edge = float(1 - up_ask - down_ask)
        fee = float(up_ask * Decimal("0.02") + down_ask * Decimal("0.02"))
        net_profit = (gross_edge - float(Decimal("0.04"))) * float(shares)
        self._total_profit_simulated += net_profit
        
        up_result = OrderResult(
            order_id=f"sim-{self._trades_executed}-up",
            token_id=up_token_id,
            side="BUY",
            price=float(up_ask),
            size=float(shares),
            status="simulated",
        )
        down_result = OrderResult(
            order_id=f"sim-{self._trades_executed}-down",
            token_id=down_token_id,
            side="BUY",
            price=float(down_ask),
            size=float(shares),
            status="simulated",
        )
        
        logger.info(
            f"[SIM] Trade #{self._trades_executed} | {market_slug} | "
            f"UP@{float(up_ask):.4f} + DOWN@{float(down_ask):.4f} × {float(shares):.1f}sh | "
            f"net=${net_profit:.4f} | cumulative=${self._total_profit_simulated:.4f}"
        )
        
        return TradeResult(up_order=up_result, down_order=down_result, simulated=True)
    
    async def _execute_live(
        self,
        market_slug: str,
        up_token_id: str,
        down_token_id: str,
        up_ask: Decimal,
        down_ask: Decimal,
        shares: Decimal,
    ) -> TradeResult:
        """Place real FOK orders via py-clob-client (single batch round-trip)."""
        from py_clob_client.clob_types import OrderArgs, OrderType

        loop = asyncio.get_event_loop()
        _client = self._client
        t_start = time.perf_counter()

        up_price = float(up_ask)
        down_price = float(down_ask)
        size = float(shares)

        def _build_and_post_batch() -> list:
            # Build both orders
            up_args   = OrderArgs(token_id=up_token_id,   price=up_price,   size=size, side="BUY", fee_rate_bps=200)
            down_args = OrderArgs(token_id=down_token_id, price=down_price, size=size, side="BUY", fee_rate_bps=200)
            up_order   = _client.create_order(up_args)
            down_order = _client.create_order(down_args)
            # Single network round-trip for both legs
            return _client.post_orders([up_order, down_order], orderType=OrderType.FOK)

        def _parse(resp, token_id, price, leg) -> OrderResult:
            """Parse one element from the batch response list."""
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id", "")
                status_raw = resp.get("status", "")
                error_msg  = resp.get("errorMsg") or resp.get("error") or None
                resolved   = "filled" if (status_raw == "matched" or order_id) else "failed"
            else:
                order_id, resolved, error_msg = "", "failed", str(resp)
            return OrderResult(
                order_id=str(order_id) if order_id else f"live-{leg}-no-id",
                token_id=token_id,
                side="BUY",
                price=price,
                size=size,
                status=resolved,
                error=error_msg,
            )

        try:
            resps = await loop.run_in_executor(None, _build_and_post_batch)
            latency_ms = (time.perf_counter() - t_start) * 1000
            logger.info(f"[LIVE] {market_slug} batch response ({latency_ms:.1f}ms): {resps}")
            self._last_latency_ms = latency_ms
            self._latency_samples.append(latency_ms)

            # resps is a list of 2 dicts: [up_resp, down_resp]
            if not isinstance(resps, (list, tuple)) or len(resps) < 2:
                raise ValueError(f"Unexpected batch response shape: {resps}")

            up_result   = _parse(resps[0], up_token_id,   up_price,   "up")
            down_result = _parse(resps[1], down_token_id, down_price, "down")

        except Exception as e:
            latency_ms = (time.perf_counter() - t_start) * 1000
            logger.error(f"[LIVE] {market_slug} batch failed ({latency_ms:.1f}ms): {e}")
            self._last_latency_ms = latency_ms
            self._latency_samples.append(latency_ms)
            failed = OrderResult(order_id="batch-error", token_id="", side="BUY",
                                 price=0, size=size, status="failed", error=str(e))
            return TradeResult(up_order=failed, down_order=failed, simulated=False)

        if not (up_result.status == "filled" and down_result.status == "filled"):
            logger.warning(
                f"[LIVE] {market_slug} partial/full failure — "
                f"UP={up_result.status}, DOWN={down_result.status}"
            )

        return TradeResult(up_order=up_result, down_order=down_result, simulated=False)
    
    @property
    def stats(self) -> dict:
        samples = self._latency_samples
        if samples:
            avg_latency = sum(samples) / len(samples)
            sorted_s = sorted(samples)
            p95_idx = int(len(sorted_s) * 0.95)
            p95_latency = sorted_s[min(p95_idx, len(sorted_s) - 1)]
        else:
            avg_latency = 0.0
            p95_latency = 0.0
        return {
            "trades": self._trades_executed,
            "simulated_profit": round(self._total_profit_simulated, 4),
            "mode": "simulation" if self.simulation else "live",
            "last_latency_ms": round(self._last_latency_ms, 3),
            "avg_latency_ms": round(avg_latency, 3),
            "p95_latency_ms": round(p95_latency, 3),
        }
