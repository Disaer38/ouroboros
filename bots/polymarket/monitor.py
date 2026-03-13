"""
monitor.py — Vague-Sourdough Live Monitor (Paper Mode)
=======================================================
Runs indefinitely, scanning 5-minute BTC/ETH/XRP/SOL/BNB markets on Polymarket.
Logs every signal, open, and close event to logs/polymarket_monitor.jsonl.

Strategy (reverse-engineered from @vague-sourdough / 0x70ec...):
  - Watch 5-min binary markets (up-or-down-5m) for each asset.
  - When one side's probability >= THRESHOLD (default 0.60), that side is "dominant".
  - Paper-buy dominant side at window open (first 4 minutes only).
  - Track P&L via bid price at expiry.
  - Log every event as a JSONL line + print to console.

Usage:
  python bots/polymarket/monitor.py              # runs forever
  python bots/polymarket/monitor.py --runtime 3600  # runs for 1h
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── dependency bootstrap ────────────────────────────────────────────────────
try:
    import aiohttp
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "aiohttp"])
    import aiohttp  # type: ignore

# ── paths ───────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent              # bots/polymarket -> bots -> repo root
LOG_PATH = _REPO_ROOT / "logs" / "polymarket_monitor.jsonl"

# ── strategy params ─────────────────────────────────────────────────────────
ASSETS    = ["btc", "eth", "xrp", "sol", "bnb"]
THRESHOLD = 0.60          # min probability to consider side "dominant"
SIZE_USD  = 20.0          # paper-trade size per signal (USDC)
MAX_OPEN  = 4             # max simultaneous open positions
INTERVAL  = 30            # scan interval in seconds
BUY_WINDOW_SECS = 240     # only enter in first 4 min of 5-min window

# ── API endpoints ────────────────────────────────────────────────────────────
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"


# ────────────────────────────────────────────────────────────────────────────
# Data models
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    asset:       str
    outcome:     str        # "Up" or "Down"
    token_id:    str
    entry_price: float
    size:        float      # USDC spent
    window_ts:   int        # start of the 5-min window (unix)
    opened_at:   str = field(default_factory=lambda: ts_str())


# ────────────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────────────

def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def cur_window() -> int:
    """Floor now to nearest 5-minute boundary."""
    return (now_ts() // 300) * 300

def ts_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def window_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC")


class JSONLLogger:
    """Append-only JSONL logger with graceful error handling."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, obj: dict) -> None:
        obj.setdefault("ts", ts_str())
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, default=str) + "\n")
        except Exception as exc:
            print(f"[LOG ERROR] {exc}", file=sys.stderr)


# ────────────────────────────────────────────────────────────────────────────
# Monitor core
# ────────────────────────────────────────────────────────────────────────────

class SourdoughMonitor:
    def __init__(self, runtime: Optional[int] = None):
        self.runtime   = runtime              # seconds; None = run forever
        self.balance   = 1_000.0             # paper balance
        self.realized  = 0.0
        self.wins = self.losses = self.trades = 0
        self.positions: list[Position] = []
        self.entered:   set[int] = set()      # (asset, window) already traded
        self.session:   Optional[aiohttp.ClientSession] = None
        self.logger     = JSONLLogger(LOG_PATH)
        self._stop      = False

    # ── HTTP helpers ────────────────────────────────────────────────────────

    async def _get(self, url: str, params: dict | None = None) -> dict | list | None:
        """GET with timeout + error swallow. Returns parsed JSON or None."""
        try:
            async with self.session.get(                         # type: ignore
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                # Non-200: log quietly and return None
                text = await resp.text()
                print(f"  [HTTP {resp.status}] {url} → {text[:120]}")
        except asyncio.TimeoutError:
            print(f"  [TIMEOUT] {url}")
        except aiohttp.ClientError as exc:
            print(f"  [NET ERROR] {url}: {exc}")
        except Exception as exc:
            print(f"  [ERROR] {url}: {exc}")
        return None

    # ── Market data ─────────────────────────────────────────────────────────

    async def _fetch_market(self, asset: str, window: int) -> Optional[dict]:
        """
        Fetch market data for one asset/window from Gamma.
        Slug format: {asset}-updown-5m-{window_ts}
        Returns dict with price_up, price_dn, token_up, token_dn, resolved.
        """
        slug = f"{asset}-updown-5m-{window}"
        data = await self._get(f"{GAMMA}/events", {"slug": slug})
        if not data or not isinstance(data, list) or not data:
            # Try without the timestamp (search by partial slug)
            data = await self._get(f"{GAMMA}/markets", {
                "active": "true",
                "closed": "false",
                "slug": slug,
                "limit": 1,
            })
            if isinstance(data, dict):
                data = data.get("data", [])
            if not data:
                return None

        try:
            # Gamma events structure: data[0]["markets"][0]
            entry = data[0]
            if "markets" in entry:
                mkt = entry["markets"][0]
            else:
                mkt = entry

            prices  = json.loads(mkt.get("outcomePrices") or '["0.5","0.5"]')
            tok_ids = json.loads(mkt.get("clobTokenIds")  or "[]")

            return {
                "market_id":  mkt.get("id"),
                "question":   mkt.get("question", ""),
                "price_up":   float(prices[0]) if len(prices) > 0 else 0.5,
                "price_dn":   float(prices[1]) if len(prices) > 1 else 0.5,
                "token_up":   tok_ids[0] if len(tok_ids) > 0 else None,
                "token_dn":   tok_ids[1] if len(tok_ids) > 1 else None,
                "resolved":   mkt.get("resolved", False),
                "active":     mkt.get("active", True),
            }
        except (IndexError, KeyError, json.JSONDecodeError, TypeError):
            return None

    async def _fetch_bid(self, token_id: Optional[str]) -> float:
        """Best bid price from CLOB, 0.0 if unavailable."""
        if not token_id:
            return 0.0
        data = await self._get(f"{CLOB}/book", {"token_id": token_id})
        if data and data.get("bids"):
            try:
                return float(data["bids"][0]["price"])
            except (KeyError, TypeError, ValueError):
                pass
        return 0.0

    # ── Position lifecycle ──────────────────────────────────────────────────

    async def _settle_expired(self) -> None:
        """Check all open positions whose window has ended and settle them."""
        window = cur_window()
        still_open: list[Position] = []

        for pos in self.positions:
            if pos.window_ts >= window:
                still_open.append(pos)
                continue

            # Window has expired — try to get closing bid
            bid = await self._fetch_bid(pos.token_id)

            # Classify result
            if bid >= 0.90:
                pnl    = (0.99 - pos.entry_price) * pos.size
                result = "WIN"
                self.wins += 1
            elif bid <= 0.10:
                pnl    = (0.01 - pos.entry_price) * pos.size
                result = "LOSS"
                self.losses += 1
            elif bid > 0:
                pnl    = (bid - pos.entry_price) * pos.size
                result = f"CLOSED@{bid:.3f}"
                if pnl >= 0:
                    self.wins += 1
                else:
                    self.losses += 1
            else:
                # Bid unavailable — try Gamma resolved price
                mkt = await self._fetch_market(pos.asset.lower(), pos.window_ts)
                if mkt and mkt.get("resolved"):
                    cur_p = mkt["price_up"] if pos.outcome == "Up" else mkt["price_dn"]
                    pnl   = (cur_p - pos.entry_price) * pos.size
                    result = f"SETTLED@{cur_p:.3f}"
                    if pnl >= 0:
                        self.wins += 1
                    else:
                        self.losses += 1
                else:
                    # Unknown — mark as loss for conservatism
                    pnl    = -pos.entry_price * pos.size
                    result = "UNKNOWN(assumed LOSS)"
                    self.losses += 1

            self.balance  += pos.size + pnl   # return stake + P&L
            self.realized += pnl
            self.trades   += 1

            label = "✅ WIN " if result == "WIN" else ("❌ LOSS" if result == "LOSS" else f"⚪ {result}")
            print(
                f"  [{label}] {pos.asset}-{pos.outcome} | "
                f"entry={pos.entry_price:.3f} close_bid={bid:.3f} "
                f"PnL=${pnl:+.2f} | total realized=${self.realized:+.2f}"
            )

            self.logger.log({
                "event":       "close",
                "asset":       pos.asset,
                "outcome":     pos.outcome,
                "entry_price": pos.entry_price,
                "close_bid":   bid,
                "pnl":         round(pnl, 4),
                "result":      result,
                "window":      pos.window_ts,
                "size":        pos.size,
            })

        self.positions = still_open

    def _already_entered(self, asset: str, window: int) -> bool:
        return (asset.upper(), window) in self.entered

    def _mark_entered(self, asset: str, window: int) -> None:
        self.entered.add((asset.upper(), window))

    # ── Main scan loop ──────────────────────────────────────────────────────

    async def _scan_tick(self) -> None:
        window   = cur_window()
        secs_in  = now_ts() - window
        can_buy  = (secs_in <= BUY_WINDOW_SECS) and (len(self.positions) < MAX_OPEN)

        print(
            f"\n{'='*64}\n"
            f"  {ts_str()} | Window: {window_str(window)} "
            f"({secs_in}s in, {300-secs_in}s left)\n"
            f"  Balance: ${self.balance:.2f} | PnL: ${self.realized:+.2f} | "
            f"W/L: {self.wins}/{self.losses} | Open: {len(self.positions)}"
        )

        for asset in ASSETS:
            mkt = await self._fetch_market(asset, window)
            if not mkt:
                print(f"  {asset.upper():4s} — no market data")
                continue

            pu, pd = mkt["price_up"], mkt["price_dn"]

            # Determine dominant side
            dominant_outcome: Optional[str] = None
            dominant_price:   float         = 0.0
            dominant_token:   Optional[str] = None

            if pu >= THRESHOLD and pu >= pd:
                dominant_outcome = "Up"
                dominant_price   = pu
                dominant_token   = mkt["token_up"]
            elif pd >= THRESHOLD:
                dominant_outcome = "Down"
                dominant_price   = pd
                dominant_token   = mkt["token_dn"]

            sig_label = (
                f"  🎯 SIGNAL: {dominant_outcome} @ {dominant_price:.3f}"
                if dominant_outcome else ""
            )
            print(f"  {asset.upper():4s}  Up={pu:.3f}  Down={pd:.3f}{sig_label}")

            # Log signal regardless of whether we enter
            if dominant_outcome:
                self.logger.log({
                    "event":           "signal",
                    "asset":           asset.upper(),
                    "outcome":         dominant_outcome,
                    "dominant_price":  dominant_price,
                    "other_price":     pd if dominant_outcome == "Up" else pu,
                    "window":          window,
                    "secs_in_window":  secs_in,
                })

            # Paper-enter if conditions met
            if (
                can_buy
                and dominant_outcome
                and dominant_token
                and not self._already_entered(asset, window)
                and len(self.positions) < MAX_OPEN
            ):
                pos = Position(
                    asset       = asset.upper(),
                    outcome     = dominant_outcome,
                    token_id    = dominant_token,
                    entry_price = dominant_price,
                    size        = SIZE_USD,
                    window_ts   = window,
                )
                self.positions.append(pos)
                self.balance -= SIZE_USD
                self._mark_entered(asset, window)
                can_buy = len(self.positions) < MAX_OPEN  # re-evaluate cap

                print(
                    f"  📥 OPEN  {asset.upper()}-{dominant_outcome} @ {dominant_price:.3f}  "
                    f"size=${SIZE_USD}  balance=${self.balance:.2f}"
                )
                self.logger.log({
                    "event":       "open",
                    "asset":       asset.upper(),
                    "outcome":     dominant_outcome,
                    "entry_price": dominant_price,
                    "size":        SIZE_USD,
                    "window":      window,
                    "balance":     round(self.balance, 2),
                })

        # Show live unrealized P&L for open positions
        if self.positions:
            print("  Open positions:")
            for pos in self.positions:
                bid  = await self._fetch_bid(pos.token_id)
                upnl = (bid - pos.entry_price) * pos.size if bid > 0 else 0.0
                ttl  = max(0, 300 - (now_ts() - pos.window_ts))
                print(
                    f"    {pos.asset}-{pos.outcome} | "
                    f"entry={pos.entry_price:.3f}  bid={bid:.3f}  "
                    f"uPnL=${upnl:+.2f}  TTL={ttl}s"
                )

    # ── Entry point ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        end_ts = (time.time() + self.runtime) if self.runtime else float("inf")

        print("=" * 64)
        print("  SOURDOUGH LIVE MONITOR  (paper mode)")
        print(f"  Assets   : {', '.join(a.upper() for a in ASSETS)}")
        print(f"  Threshold: {THRESHOLD:.0%}  Size: ${SIZE_USD}  Max open: {MAX_OPEN}")
        print(f"  Log      : {LOG_PATH}")
        print(f"  Runtime  : {'∞' if self.runtime is None else f'{self.runtime}s'}")
        print("=" * 64)

        self.logger.log({
            "event":     "start",
            "assets":    ASSETS,
            "threshold": THRESHOLD,
            "size_usd":  SIZE_USD,
            "max_open":  MAX_OPEN,
        })

        async with aiohttp.ClientSession() as session:
            self.session = session

            while not self._stop and time.time() < end_ts:
                try:
                    await self._settle_expired()
                    await self._scan_tick()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    print(f"\n[CYCLE ERROR] {exc}")
                    self.logger.log({"event": "error", "error": str(exc)})

                await asyncio.sleep(INTERVAL)

            # Final settle before exit
            try:
                await asyncio.sleep(2)
                await self._settle_expired()
            except Exception:
                pass

        self._print_summary()

    def _print_summary(self) -> None:
        wr = f"{self.wins / self.trades * 100:.1f}%" if self.trades else "N/A"
        print("\n" + "=" * 64)
        print("  FINAL SUMMARY")
        print(f"  Start balance : $1,000.00")
        print(f"  End balance   : ${self.balance:.2f}")
        print(f"  Realized PnL  : ${self.realized:+.2f}")
        print(f"  Trades        : {self.trades}  ({self.wins}W / {self.losses}L)  WR={wr}")
        print(f"  Log           : {LOG_PATH}")
        print("=" * 64)

        self.logger.log({
            "event":    "stop",
            "balance":  round(self.balance, 2),
            "realized": round(self.realized, 4),
            "trades":   self.trades,
            "wins":     self.wins,
            "losses":   self.losses,
            "win_rate": wr,
        })


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sourdough Live Monitor")
    p.add_argument(
        "--runtime", type=int, default=None,
        help="Run for N seconds then stop (default: run forever)"
    )
    p.add_argument(
        "--threshold", type=float, default=THRESHOLD,
        help=f"Dominant-side threshold (default: {THRESHOLD})"
    )
    p.add_argument(
        "--size", type=float, default=SIZE_USD,
        help=f"Paper-trade size in USDC (default: {SIZE_USD})"
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    # Allow CLI overrides
    global THRESHOLD, SIZE_USD
    THRESHOLD = args.threshold
    SIZE_USD  = args.size

    monitor = SourdoughMonitor(runtime=args.runtime)

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(*_):
        print("\n[STOP] Signal received, finishing current cycle…")
        monitor._stop = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, _shutdown)

    try:
        loop.run_until_complete(monitor.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
