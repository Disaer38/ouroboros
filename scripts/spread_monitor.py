#!/usr/bin/env python3
"""
Polymarket Live Spread Monitor
Monitors 5-min BTC/ETH/SOL markets and logs spread data to CSV.

Usage:
    python scripts/spread_monitor.py

Environment variables:
    POLL_INTERVAL_SECONDS  — seconds between polls (default: 5)
    POLYMARKET_PROXY_URL   — optional proxy for geo-blocked regions
"""
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
GEO_CHECK_URL    = "https://ipinfo.io/json"
POLYMARKET_GEO_URL = "https://polymarket.com/api/geoblock"

ASSETS = ["btc", "eth", "sol"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))

# ANSI colour helpers
RED_BOLD = "\033[1;31m"
RESET    = "\033[0m"

# Paths — logs/ sits at repo root, one level above scripts/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT  = os.path.dirname(_SCRIPT_DIR)
LOGS_DIR    = os.path.join(_REPO_ROOT, "logs")
CSV_PATH    = os.path.join(LOGS_DIR, "spread_log.csv")

CSV_HEADERS = [
    "timestamp", "asset", "market_id",
    "up_token_id", "down_token_id",
    "up_best_bid", "up_best_ask",
    "down_best_bid", "down_best_ask",
    "neg_risk_spread", "up_mid", "down_mid",
    "minutes_left",
]

# ---------------------------------------------------------------------------
# Session-level stats
# ---------------------------------------------------------------------------

_stats: dict = {
    "total_polls": 0,
    "arb_found":   0,
    "max_neg_risk_spread": 0.0,
}

# ---------------------------------------------------------------------------
# Geo-block check
# ---------------------------------------------------------------------------

def geo_check(session: requests.Session) -> None:
    """Print IP / geo info and Polymarket block status at startup."""
    print("Checking geo-location …")
    proxy_url = os.environ.get("POLYMARKET_PROXY_URL")
    if proxy_url:
        print(f"  Proxy configured: {_mask_url(proxy_url)}")

    try:
        resp = session.get(GEO_CHECK_URL, timeout=10)
        resp.raise_for_status()
        geo = resp.json()
        print(f"  IP: {geo.get('ip', '?')}  |  Country: {geo.get('country', '?')}  |  City: {geo.get('city', '?')}")
    except Exception as exc:
        print(f"  IP lookup failed: {exc}")

    try:
        pm = session.get(POLYMARKET_GEO_URL, timeout=10)
        pm.raise_for_status()
        pm_data = pm.json()
        blocked = pm_data.get("blocked", False)
        country = pm_data.get("countryCode", "?")
        if blocked:
            print(f"  {RED_BOLD}WARNING: Polymarket geo-BLOCKED (country={country}){RESET}")
            print("  Set POLYMARKET_PROXY_URL env var to bypass geo-block.")
        else:
            print(f"  Polymarket: ALLOWED (country={country})")
    except Exception as exc:
        print(f"  Polymarket geoblock check failed: {exc}")

    print()


def _mask_url(url: str) -> str:
    """Hide password in a proxy URL before printing."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url)
        if p.password:
            masked = p._replace(netloc=f"{p.username}:***@{p.hostname}:{p.port}")
            return urlunparse(masked)
    except Exception:
        pass
    return url


# ---------------------------------------------------------------------------
# Market discovery (Gamma API)
# ---------------------------------------------------------------------------

def fetch_active_market(asset: str, session: requests.Session) -> Optional[dict]:
    """
    Discover the current active 5m Up/Down market for *asset* via Gamma API.

    Returns a dict with keys:
        market_id, up_token_id, down_token_id, end_date (datetime), question
    or None if no active market is found.
    """
    slug = f"{asset.lower()}-up-or-down-5m"
    now  = datetime.now(timezone.utc)

    try:
        resp = session.get(
            f"{GAMMA_API}/markets",
            params={
                "slug":      slug,
                "active":    "true",
                "closed":    "false",
                "limit":     5,
                "order":     "endDate",
                "ascending": "true",
            },
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()

        # Fallback: broad search if slug lookup returns nothing
        if not markets:
            logging.debug(f"[{asset.upper()}] slug search empty, trying broad search")
            resp2 = session.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": 50},
                timeout=10,
            )
            resp2.raise_for_status()
            asset_upper = asset.upper()
            markets = [
                m for m in resp2.json()
                if asset_upper in (m.get("question") or "").upper()
                and "UP OR DOWN" in (m.get("question") or "").upper()
                and "5" in (m.get("question") or "")
            ]

        for m in markets:
            tids_raw = m.get("clobTokenIds") or m.get("clob_token_ids")
            if isinstance(tids_raw, str):
                try:
                    tids = json.loads(tids_raw)
                except Exception:
                    continue
            elif isinstance(tids_raw, list):
                tids = tids_raw
            else:
                continue

            if not tids or len(tids) < 2:
                continue

            end_str = m.get("endDate") or m.get("end_date_iso") or ""
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except Exception:
                continue

            if end_dt <= now:
                continue  # already expired

            return {
                "market_id":    str(m.get("id") or m.get("market_id") or ""),
                "up_token_id":  str(tids[0]),
                "down_token_id": str(tids[1]),
                "end_date":     end_dt,
                "question":     (m.get("question") or ""),
            }

    except Exception as exc:
        logging.warning(f"[{asset.upper()}] Market discovery error: {exc}")

    return None


# ---------------------------------------------------------------------------
# Orderbook fetching (CLOB API)
# ---------------------------------------------------------------------------

def fetch_orderbook(token_id: str, session: requests.Session) -> Optional[dict]:
    """
    Fetch current orderbook snapshot for *token_id*.

    Returns dict with keys: best_bid, best_ask, mid, spread
    or None on error.
    """
    try:
        resp = session.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        bids = sorted(
            [float(x["price"]) for x in data.get("bids", []) if float(x.get("size", 0)) > 0],
            reverse=True,
        )
        asks = sorted(
            [float(x["price"]) for x in data.get("asks", []) if float(x.get("size", 0)) > 0],
        )

        best_bid = bids[0] if bids else 0.0
        best_ask = asks[0] if asks else 1.0
        mid      = (best_bid + best_ask) / 2.0

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid":      mid,
            "spread":   best_ask - best_bid,
        }

    except Exception as exc:
        logging.warning(f"Orderbook fetch failed for token {token_id[:16]}…: {exc}")
        return None


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------

def init_csv() -> None:
    """Create logs/ directory and write CSV header if the file is new."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as fh:
            csv.writer(fh).writerow(CSV_HEADERS)


def append_csv_row(row: dict) -> None:
    with open(CSV_PATH, "a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=CSV_HEADERS).writerow(row)


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def poll_cycle(session: requests.Session) -> None:
    """Fetch data for all assets, print table, and append to CSV."""
    ts        = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wall_time = datetime.now().strftime("%H:%M:%S")
    _stats["total_polls"] += 1

    for asset in ASSETS:
        market = fetch_active_market(asset, session)
        if market is None:
            print(f"[{wall_time}] {asset.upper()} 5m | No active market — skipping")
            continue

        up_book   = fetch_orderbook(market["up_token_id"],   session)
        down_book = fetch_orderbook(market["down_token_id"], session)

        if up_book is None or down_book is None:
            print(f"[{wall_time}] {asset.upper()} 5m | Orderbook fetch failed — skipping")
            continue

        neg_risk_spread = 1.0 - up_book["best_ask"] - down_book["best_ask"]
        minutes_left    = max(
            0.0,
            (market["end_date"] - datetime.now(timezone.utc)).total_seconds() / 60.0,
        )

        # Update session stats
        if neg_risk_spread > _stats["max_neg_risk_spread"]:
            _stats["max_neg_risk_spread"] = neg_risk_spread
        if neg_risk_spread > 0.01:
            _stats["arb_found"] += 1

        # Console line
        sign = "+" if neg_risk_spread >= 0 else ""
        print(
            f"[{wall_time}] {asset.upper()} 5m"
            f" | UP: bid={up_book['best_bid']:.2f} ask={up_book['best_ask']:.2f}"
            f" | DOWN: bid={down_book['best_bid']:.2f} ask={down_book['best_ask']:.2f}"
            f" | NEG-RISK: {sign}{neg_risk_spread:.2f}"
            f" | Mid: {up_book['mid']:.2f}"
            f" | \u23f1 {minutes_left:.1f}min left"
        )

        if neg_risk_spread > 0.01:
            print(f"{RED_BOLD}\U0001f6a8 ARB OPPORTUNITY: +{neg_risk_spread:.3f}{RESET}")

        # CSV row
        append_csv_row({
            "timestamp":       ts,
            "asset":           asset.upper(),
            "market_id":       market["market_id"],
            "up_token_id":     market["up_token_id"],
            "down_token_id":   market["down_token_id"],
            "up_best_bid":     up_book["best_bid"],
            "up_best_ask":     up_book["best_ask"],
            "down_best_bid":   down_book["best_bid"],
            "down_best_ask":   down_book["best_ask"],
            "neg_risk_spread": round(neg_risk_spread, 6),
            "up_mid":          round(up_book["mid"], 6),
            "down_mid":        round(down_book["mid"], 6),
            "minutes_left":    round(minutes_left, 2),
        })


# ---------------------------------------------------------------------------
# Summary + main
# ---------------------------------------------------------------------------

def print_summary() -> None:
    print("\n" + "=" * 60)
    print("  Session summary")
    print("=" * 60)
    print(f"  Total poll cycles   : {_stats['total_polls']}")
    print(f"  Arb opportunities   : {_stats['arb_found']}  (neg_risk_spread > 0.01)")
    print(f"  Max neg_risk_spread : {_stats['max_neg_risk_spread']:.4f}")
    print(f"  CSV log             : {CSV_PATH}")
    print("=" * 60)


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print("=" * 60)
    print("  Polymarket Live Spread Monitor")
    print(f"  Assets        : {', '.join(a.upper() for a in ASSETS)}")
    print(f"  Poll interval : {POLL_INTERVAL}s")
    print("=" * 60)

    # Build requests session (with optional proxy)
    session = requests.Session()
    session.headers.update({"User-Agent": "ouroboros-spread-monitor/1.0"})
    proxy_url = os.environ.get("POLYMARKET_PROXY_URL")
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})

    geo_check(session)

    init_csv()
    print(f"Logging to: {CSV_PATH}")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            poll_cycle(session)
            print()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print_summary()
    finally:
        session.close()


if __name__ == "__main__":
    main()
