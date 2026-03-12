"""
Analyze trader vague-sourdough (0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d).

Fetches last N trades via Polymarket Gamma API, clusters by market,
computes per-market PnL and entry timing, then prints a strategy report.

Usage:
    python -m bots.polymarket.analyze_trader
    python -m bots.polymarket.analyze_trader --address 0xABC... --limit 500
"""
import argparse
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TARGET_ADDRESS = "0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_trades(address: str, limit: int = 500) -> list[dict]:
    """Fetch trades from Gamma API (trades endpoint)."""
    url = f"{GAMMA_API}/trades"
    params = {
        "maker": address,
        "limit": min(limit, 500),
        "offset": 0,
    }
    all_trades: list[dict] = []
    while len(all_trades) < limit:
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Gamma trades fetch failed: {e}")
            break

        batch = data if isinstance(data, list) else data.get("data", [])
        if not batch:
            break
        all_trades.extend(batch)
        if len(batch) < params["limit"]:
            break
        params["offset"] += len(batch)

    logger.info(f"Fetched {len(all_trades)} trades for {address}")
    return all_trades[:limit]


def fetch_user_positions(address: str) -> list[dict]:
    """Fetch open/closed positions from Gamma API."""
    url = f"{GAMMA_API}/positions"
    try:
        resp = requests.get(url, params={"user": address, "limit": 500}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        logger.error(f"Positions fetch failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def parse_ts(ts_val) -> datetime | None:
    """Parse a timestamp field (int epoch or ISO string)."""
    if ts_val is None:
        return None
    try:
        if isinstance(ts_val, (int, float)):
            return datetime.fromtimestamp(float(ts_val), tz=timezone.utc)
        return datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
    except Exception:
        return None


def cluster_by_market(trades: list[dict]) -> dict[str, list[dict]]:
    """Group trades by market slug/conditionId."""
    clusters: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        key = (
            t.get("market")
            or t.get("conditionId")
            or t.get("marketSlug")
            or t.get("asset_id", "unknown")
        )
        clusters[key].append(t)
    return dict(clusters)


def analyze_market(market_key: str, trades: list[dict]) -> dict[str, Any]:
    """Compute stats for a single market's trades."""
    # Sort by time
    sorted_trades = sorted(trades, key=lambda t: parse_ts(t.get("timestamp") or t.get("createdAt")) or datetime.min)

    total_spent = 0.0
    total_received = 0.0
    side_buckets: dict[str, float] = defaultdict(float)  # outcome -> total USDC
    prices_by_side: dict[str, list[float]] = defaultdict(list)
    timestamps = []

    for t in sorted_trades:
        price = float(t.get("price", 0) or 0)
        size = float(t.get("size", 0) or t.get("shares", 0) or 0)
        side = str(t.get("side", "") or t.get("outcome", "")).upper()
        ts = parse_ts(t.get("timestamp") or t.get("createdAt"))
        if ts:
            timestamps.append(ts)

        usdc_value = price * size
        if side in ("BUY", "YES", "NO"):
            total_spent += usdc_value
            side_buckets[side] += usdc_value
            prices_by_side[side].append(price)
        elif side == "SELL":
            total_received += usdc_value

    net_pnl = total_received - total_spent
    duration_secs = None
    if len(timestamps) >= 2:
        duration_secs = (timestamps[-1] - timestamps[0]).total_seconds()

    # Identify dominant/insurance pattern
    # YES + NO with sum < 1.0 → neg-risk arb
    # YES dominant (>0.60), NO insurance (<0.15) → "Dominant Side + Insurance"
    yes_usdc = side_buckets.get("YES", 0.0)
    no_usdc = side_buckets.get("NO", 0.0)
    buy_usdc = side_buckets.get("BUY", 0.0)

    yes_avg = (sum(prices_by_side["YES"]) / len(prices_by_side["YES"])) if prices_by_side.get("YES") else None
    no_avg = (sum(prices_by_side["NO"]) / len(prices_by_side["NO"])) if prices_by_side.get("NO") else None
    buy_avg = (sum(prices_by_side["BUY"]) / len(prices_by_side["BUY"])) if prices_by_side.get("BUY") else None

    pattern = "UNKNOWN"
    dominant_avg = yes_avg or buy_avg
    insurance_avg = no_avg
    if dominant_avg is not None and insurance_avg is not None:
        if dominant_avg >= 0.55 and insurance_avg <= 0.20:
            pattern = "DOMINANT_SIDE_PLUS_INSURANCE"
        elif (dominant_avg + insurance_avg) < 0.98:
            pattern = "NEG_RISK_ARB"
        else:
            pattern = "DIRECTIONAL"
    elif dominant_avg is not None:
        pattern = "DIRECTIONAL"

    return {
        "market": market_key,
        "trade_count": len(trades),
        "total_spent_usdc": round(total_spent, 4),
        "total_received_usdc": round(total_received, 4),
        "net_pnl_usdc": round(net_pnl, 4),
        "yes_usdc": round(yes_usdc, 4),
        "no_usdc": round(no_usdc, 4),
        "buy_usdc": round(buy_usdc, 4),
        "yes_avg_price": round(yes_avg, 4) if yes_avg else None,
        "no_avg_price": round(no_avg, 4) if no_avg else None,
        "buy_avg_price": round(buy_avg, 4) if buy_avg else None,
        "duration_secs": duration_secs,
        "first_trade": timestamps[0].isoformat() if timestamps else None,
        "last_trade": timestamps[-1].isoformat() if timestamps else None,
        "detected_pattern": pattern,
    }


def run_analysis(address: str, limit: int = 500) -> None:
    print(f"\n{'='*70}")
    print(f"  Trader Analysis: {address}")
    print(f"{'='*70}\n")

    trades = fetch_trades(address, limit=limit)
    if not trades:
        print("No trades found. Check address or API availability.")
        return

    clusters = cluster_by_market(trades)
    results = [analyze_market(k, v) for k, v in clusters.items()]
    results.sort(key=lambda r: abs(r["net_pnl_usdc"]), reverse=True)

    # Aggregate stats
    total_pnl = sum(r["net_pnl_usdc"] for r in results)
    total_spent = sum(r["total_spent_usdc"] for r in results)
    pattern_counts: dict[str, int] = defaultdict(int)
    for r in results:
        pattern_counts[r["detected_pattern"]] += 1

    print(f"Total trades fetched : {len(trades)}")
    print(f"Unique markets       : {len(results)}")
    print(f"Total USDC spent     : ${total_spent:,.2f}")
    print(f"Net PnL              : ${total_pnl:+,.2f}")
    print(f"\nDetected patterns:")
    for pat, cnt in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        print(f"  {pat:<35} : {cnt} markets")

    print(f"\n{'─'*70}")
    print(f"  Top 20 markets by |PnL|:")
    print(f"{'─'*70}")
    header = f"{'Market':<40} {'PnL':>8} {'Spent':>8} {'Pattern':<30} {'Trades':>6}"
    print(header)
    print("─" * len(header))
    for r in results[:20]:
        mk = r["market"][:39]
        print(
            f"{mk:<40} {r['net_pnl_usdc']:>+8.2f} {r['total_spent_usdc']:>8.2f}"
            f" {r['detected_pattern']:<30} {r['trade_count']:>6}"
        )

    print(f"\n{'─'*70}")
    print("  Entry timing analysis (avg seconds into round for DOMINANT_SIDE_PLUS_INSURANCE):")
    print(f"{'─'*70}")
    ds_markets = [r for r in results if r["detected_pattern"] == "DOMINANT_SIDE_PLUS_INSURANCE" and r["duration_secs"]]
    if ds_markets:
        avg_dur = sum(r["duration_secs"] for r in ds_markets) / len(ds_markets)
        print(f"  Markets using strategy : {len(ds_markets)}")
        print(f"  Avg trade window (secs): {avg_dur:.1f}")
        # Show entry price distribution
        yes_prices = [r["yes_avg_price"] for r in ds_markets if r.get("yes_avg_price")]
        no_prices = [r["no_avg_price"] for r in ds_markets if r.get("no_avg_price")]
        if yes_prices:
            print(f"  Dominant side avg price: {sum(yes_prices)/len(yes_prices):.3f} "
                  f"(min={min(yes_prices):.3f}, max={max(yes_prices):.3f})")
        if no_prices:
            print(f"  Insurance side avg price: {sum(no_prices)/len(no_prices):.3f} "
                  f"(min={min(no_prices):.3f}, max={max(no_prices):.3f})")
    else:
        print("  No DOMINANT_SIDE_PLUS_INSURANCE trades found (may need more data).")

    print(f"\n{'='*70}\n")

    # Save JSON report to Drive if available
    try:
        import os
        drive_dir = "/content/drive/MyDrive/Ouroboros/logs"
        if os.path.exists(drive_dir):
            out_path = f"{drive_dir}/trader_analysis_{address[:10]}.json"
            with open(out_path, "w") as f:
                json.dump({"address": address, "markets": results}, f, indent=2)
            print(f"Full report saved to: {out_path}")
    except Exception as e:
        logger.debug(f"Drive save skipped: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze a Polymarket trader's strategy")
    parser.add_argument("--address", default=TARGET_ADDRESS, help="Trader wallet address")
    parser.add_argument("--limit", type=int, default=500, help="Max trades to fetch")
    args = parser.parse_args()
    run_analysis(args.address, args.limit)

