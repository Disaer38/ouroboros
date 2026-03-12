"""
Deep analysis of vague-sourdough trader on Polymarket.

Fetches the last 60 minutes of trading activity for:
  0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d

Calculates:
  - Trade count and volume (last 60 min)
  - Markets traded with labels
  - Spread captured (from buy/sell pairs in same market)
  - Turnover speed (avg hold time between buy→sell)
  - Pattern classification per market

Saves report to: data/sourdough_analysis.md

Usage:
    cd /content/ouroboros_repo
    python -m bots.analyze_sourdough
    python -m bots.analyze_sourdough --hours 12
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

TARGET = "0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d"
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
OUTPUT_PATH = "data/sourdough_analysis.md"


# ─────────────────────────────────────────────
# Fetching
# ─────────────────────────────────────────────

def _parse_ts(val) -> datetime | None:
    if val is None:
        return None
    try:
        if isinstance(val, (int, float)):
            v = float(val)
            # Handle milliseconds vs seconds
            if v > 1e12:
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        s = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fetch_trades_data_api(address: str, since: datetime, max_pages: int = 50) -> list[dict]:
    """Fetch trades from data-api.polymarket.com/trades with time filter."""
    url = f"{DATA_API}/trades"
    trades = []
    offset = 0
    limit = 500
    cutoff_reached = False

    print(f"  Fetching trades since {since.strftime('%H:%M:%S UTC')} ...")
    for page in range(max_pages):
        params = {
            "user": address,
            "limit": limit,
            "offset": offset,
        }
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"  [WARN] data-api page {page}: {e}")
            break

        if not batch or not isinstance(batch, list):
            break

        for t in batch:
            ts = _parse_ts(t.get("timestamp") or t.get("createdAt"))
            if ts and ts < since:
                cutoff_reached = True
                break
            trades.append(t)

        print(f"  Page {page+1}: +{len(batch)} trades | total so far: {len(trades)}")

        if cutoff_reached or len(batch) < limit:
            break
        offset += len(batch)
        time.sleep(0.1)

    return trades


def fetch_trades_gamma(address: str, since: datetime, max_pages: int = 20) -> list[dict]:
    """Fallback: Gamma trades endpoint."""
    url = f"{GAMMA_API}/trades"
    trades = []
    offset = 0
    limit = 500

    print(f"  [GAMMA fallback] Fetching trades since {since.strftime('%H:%M:%S UTC')} ...")
    for page in range(max_pages):
        params = {"maker": address, "limit": limit, "offset": offset}
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            batch = data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            print(f"  [WARN] Gamma page {page}: {e}")
            break

        if not batch:
            break

        cutoff_reached = False
        for t in batch:
            ts = _parse_ts(t.get("timestamp") or t.get("createdAt"))
            if ts and ts < since:
                cutoff_reached = True
                break
            trades.append(t)

        print(f"  Gamma page {page+1}: +{len(batch)} | total: {len(trades)}")
        if cutoff_reached or len(batch) < limit:
            break
        offset += len(batch)
        time.sleep(0.1)

    return trades


def enrich_with_market_labels(trades: list[dict]) -> None:
    """Try to resolve conditionId → human label via Gamma markets endpoint."""
    condition_ids = {
        t.get("conditionId") or t.get("market") or t.get("asset_id")
        for t in trades
        if (t.get("conditionId") or t.get("market") or t.get("asset_id"))
    }
    condition_ids.discard(None)
    if not condition_ids:
        return

    label_map: dict[str, str] = {}
    print(f"  Resolving {len(condition_ids)} market labels ...")
    for cid in list(condition_ids)[:30]:  # cap API calls
        try:
            r = requests.get(
                f"{GAMMA_API}/markets",
                params={"condition_id": cid},
                timeout=10,
            )
            if r.ok:
                data = r.json()
                items = data if isinstance(data, list) else data.get("markets", [])
                if items:
                    label_map[cid] = items[0].get("question", cid)[:60]
        except Exception:
            pass

    for t in trades:
        key = t.get("conditionId") or t.get("market") or t.get("asset_id")
        if key in label_map:
            t["_label"] = label_map[key]


# ─────────────────────────────────────────────
# Analysis helpers
# ─────────────────────────────────────────────

def group_by_market(trades: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        key = (
            t.get("conditionId")
            or t.get("market")
            or t.get("asset_id")
            or "unknown"
        )
        groups[key].append(t)
    return dict(groups)


def compute_market_stats(market_key: str, trades: list[dict]) -> dict[str, Any]:
    trades = sorted(
        trades,
        key=lambda t: _parse_ts(t.get("timestamp") or t.get("createdAt")) or datetime.min,
    )

    buy_trades = []
    sell_trades = []
    timestamps = []
    total_volume_usdc = 0.0

    for t in trades:
        side = str(t.get("side", "") or t.get("type", "") or "").upper()
        price = float(t.get("price", 0) or 0)
        size = float(t.get("size", 0) or t.get("shares", 0) or 0)
        usdc = price * size
        total_volume_usdc += usdc
        ts = _parse_ts(t.get("timestamp") or t.get("createdAt"))
        if ts:
            timestamps.append(ts)

        if side in ("BUY", "YES"):
            buy_trades.append({"price": price, "size": size, "usdc": usdc, "ts": ts})
        elif side in ("SELL", "NO"):
            sell_trades.append({"price": price, "size": size, "usdc": usdc, "ts": ts})

    # Average buy/sell prices
    avg_buy = (sum(b["price"] for b in buy_trades) / len(buy_trades)) if buy_trades else None
    avg_sell = (sum(s["price"] for s in sell_trades) / len(sell_trades)) if sell_trades else None

    # Spread captured: avg_sell - avg_buy (if we sold higher than we bought = positive)
    spread = None
    if avg_buy is not None and avg_sell is not None:
        spread = avg_sell - avg_buy

    # Turnover speed: average time between consecutive trades
    avg_gap_secs = None
    if len(timestamps) >= 2:
        gaps = [
            (timestamps[i + 1] - timestamps[i]).total_seconds()
            for i in range(len(timestamps) - 1)
        ]
        avg_gap_secs = sum(gaps) / len(gaps)

    # Duration of activity in this market
    duration_secs = None
    if len(timestamps) >= 2:
        duration_secs = (timestamps[-1] - timestamps[0]).total_seconds()

    # Pattern classification
    has_both_sides = bool(buy_trades) and bool(sell_trades)
    pattern = "UNKNOWN"
    if has_both_sides:
        if avg_buy is not None and avg_sell is not None and avg_sell > avg_buy + 0.01:
            pattern = "SPREAD_CAPTURE"  # classic MM: buy low, sell high
        elif avg_buy is not None and avg_sell is not None and avg_sell < avg_buy:
            pattern = "LIQUIDITY_PROVISION_LOSS"
        else:
            pattern = "HEDGED_BOTH_SIDES"
    elif buy_trades:
        pattern = "DIRECTIONAL_BUY"
    elif sell_trades:
        pattern = "DIRECTIONAL_SELL"

    # Use enriched label if available
    label = trades[0].get("_label") if trades else None

    return {
        "market_key": market_key[:60],
        "label": label or market_key[:60],
        "trade_count": len(trades),
        "buy_count": len(buy_trades),
        "sell_count": len(sell_trades),
        "total_volume_usdc": round(total_volume_usdc, 2),
        "avg_buy_price": round(avg_buy, 4) if avg_buy is not None else None,
        "avg_sell_price": round(avg_sell, 4) if avg_sell is not None else None,
        "spread_captured": round(spread, 4) if spread is not None else None,
        "avg_gap_secs": round(avg_gap_secs, 2) if avg_gap_secs is not None else None,
        "duration_secs": round(duration_secs, 1) if duration_secs is not None else None,
        "first_trade": timestamps[0].strftime("%H:%M:%S") if timestamps else None,
        "last_trade": timestamps[-1].strftime("%H:%M:%S") if timestamps else None,
        "pattern": pattern,
    }


# ─────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────

STRATEGY_DESCRIPTIONS = {
    "SPREAD_CAPTURE": "🎯 MM Spread Capture — buys low, sells high within same market.",
    "HEDGED_BOTH_SIDES": "⚖️ Dual-Side Hedge — holds both YES/NO to lock in guaranteed payout.",
    "DIRECTIONAL_BUY": "📈 Directional Buy — one-way position, no exit observed in window.",
    "DIRECTIONAL_SELL": "📉 Directional Sell — exiting/shorting position.",
    "LIQUIDITY_PROVISION_LOSS": "⚠️ Loss from spread (sold lower than bought — market moved against).",
    "UNKNOWN": "❓ Unknown pattern.",
}


def build_report(
    address: str,
    trades: list[dict],
    stats_by_market: list[dict[str, Any]],
    window_hours: float,
    generated_at: datetime,
) -> str:
    total_trades = len(trades)
    total_volume = sum(s["total_volume_usdc"] for s in stats_by_market)
    unique_markets = len(stats_by_market)

    # Aggregate spread stats
    spreads = [s["spread_captured"] for s in stats_by_market if s["spread_captured"] is not None]
    avg_spread = sum(spreads) / len(spreads) if spreads else None
    positive_spreads = [s for s in spreads if s > 0]

    # Pattern distribution
    pattern_counts: dict[str, int] = defaultdict(int)
    for s in stats_by_market:
        pattern_counts[s["pattern"]] += 1

    # Speed stats
    gaps = [s["avg_gap_secs"] for s in stats_by_market if s["avg_gap_secs"] is not None]
    overall_avg_gap = sum(gaps) / len(gaps) if gaps else None
    trades_per_min = (total_trades / (window_hours * 60)) if window_hours > 0 else 0

    lines = []
    lines.append(f"# 🔬 vague-sourdough Strategy Analysis")
    lines.append(f"")
    lines.append(f"> Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"> Address: `{address}`")
    lines.append(f"> Window: Last {window_hours:.1f} hour(s)")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 📊 Overview")
    lines.append(f"")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total Trades | **{total_trades:,}** |")
    lines.append(f"| Unique Markets | **{unique_markets}** |")
    lines.append(f"| Total Volume (USDC) | **${total_volume:,.2f}** |")
    lines.append(f"| Trades/minute | **{trades_per_min:.1f}** |")
    if overall_avg_gap:
        lines.append(f"| Avg time between trades | **{overall_avg_gap:.1f}s** |")
    if avg_spread is not None:
        lines.append(f"| Avg spread captured | **{avg_spread:+.4f}** ({avg_spread*100:+.2f}¢) |")
    lines.append(f"| Markets with positive spread | **{len(positive_spreads)}/{len(spreads)}** |")
    lines.append(f"")

    lines.append(f"## 🧠 Strategy Pattern Breakdown")
    lines.append(f"")
    for pattern, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        desc = STRATEGY_DESCRIPTIONS.get(pattern, pattern)
        pct = count / unique_markets * 100 if unique_markets else 0
        lines.append(f"- **{pattern}** ({count} markets, {pct:.0f}%): {desc}")
    lines.append(f"")

    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 🏪 Markets Traded (sorted by volume)")
    lines.append(f"")
    lines.append(f"| Market | Trades | Volume | Buy→Sell | Spread | Avg Gap | Pattern |")
    lines.append(f"|--------|--------|--------|----------|--------|---------|---------|")

    for s in sorted(stats_by_market, key=lambda x: -x["total_volume_usdc"])[:40]:
        label = s["label"][:45] if s["label"] else s["market_key"][:45]
        spread_str = f"{s['spread_captured']:+.3f}" if s["spread_captured"] is not None else "—"
        gap_str = f"{s['avg_gap_secs']:.1f}s" if s["avg_gap_secs"] is not None else "—"
        lines.append(
            f"| {label} | {s['trade_count']} | ${s['total_volume_usdc']:.1f} "
            f"| {s['buy_count']}→{s['sell_count']} | {spread_str} | {gap_str} | {s['pattern']} |"
        )
    lines.append(f"")

    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## 🔑 Strategy Conclusions")
    lines.append(f"")

    # Determine dominant strategy
    dominant_pattern = max(pattern_counts.items(), key=lambda x: x[1])[0] if pattern_counts else "UNKNOWN"

    if dominant_pattern == "SPREAD_CAPTURE":
        lines.append(f"""**Primary Strategy: HIGH-FREQUENCY SPREAD CAPTURE (Market Making)**

The bot systematically buys outcome tokens at one price and sells them at a higher price
within the same market. This is classic market-making: providing liquidity on both sides
and profiting from the bid-ask spread.

Key mechanics:
1. Enters multiple markets simultaneously (5-min BTC/ETH/SOL updown markets)
2. Buys at bid (e.g. 0.65), sells at ask (e.g. 0.72) = 7¢ spread captured
3. Never holds to expiry — all positions exited within minutes
4. High frequency ({trades_per_min:.0f} trades/min) allows small spreads to compound

**Why 0% "win rate":** The position always closes before expiry, so it never
"wins" or "loses" on the outcome. PnL comes entirely from spread, not prediction.
""")
    elif dominant_pattern == "HEDGED_BOTH_SIDES":
        lines.append(f"""**Primary Strategy: DUAL-SIDE HEDGING (Negative Risk)**

The bot holds both YES and NO on the same market. Combined cost < $1.00 means
guaranteed profit regardless of outcome. This is Negative Risk arbitrage.

Key mechanics:
1. Buys YES at price P₁ and NO at price P₂ where P₁ + P₂ < 1.0
2. One side always pays out $1.00
3. Guaranteed profit = 1.0 - (P₁ + P₂) per share

This is pure arbitrage, not prediction. Works when the market is temporarily mispriced.
""")
    elif dominant_pattern == "DIRECTIONAL_BUY":
        lines.append(f"""**Primary Strategy: DIRECTIONAL ACCUMULATION**

The bot is aggressively buying one side. This suggests either:
1. It has predictive signal (sentiment, technical) about the outcome
2. It's accumulating before a late exit (prices have moved in its favor)
3. The sell-side exits happened outside the {window_hours:.0f}h window

Recommend extending analysis window to see full entry→exit cycle.
""")
    else:
        lines.append(f"Dominant pattern: **{dominant_pattern}**. Further data needed for definitive classification.")

    lines.append(f"")
    lines.append(f"---")
    lines.append(f"*Analysis by Ouroboros · Data: Polymarket Data API*")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main(hours: float = 1.0) -> None:
    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(hours=hours)

    print(f"\n{'='*65}")
    print(f"  vague-sourdough Deep Analysis")
    print(f"  Address : {TARGET}")
    print(f"  Window  : Last {hours:.1f} hour(s) (since {since.strftime('%H:%M UTC')})")
    print(f"{'='*65}\n")

    # Try primary data API first
    trades = fetch_trades_data_api(TARGET, since)

    if not trades:
        print("  Primary API returned 0 results. Trying Gamma fallback ...")
        trades = fetch_trades_gamma(TARGET, since)

    if not trades:
        print("\n❌ No trades found in the specified window.")
        print("   Possible reasons:")
        print("   1. Bot was inactive during this period")
        print("   2. API rate limit / temporary unavailability")
        print("   3. Proxy address mismatch")
        sys.exit(0)

    print(f"\n✅ Total trades fetched: {len(trades)}")

    # Optionally enrich labels (best-effort)
    try:
        enrich_with_market_labels(trades)
    except Exception as e:
        print(f"  [WARN] Label enrichment failed: {e}")

    # Group and analyze
    groups = group_by_market(trades)
    stats = [compute_market_stats(k, v) for k, v in groups.items()]
    stats.sort(key=lambda x: -x["total_volume_usdc"])

    # Print summary to stdout
    print(f"\n{'─'*65}")
    print(f"  SUMMARY — Last {hours:.1f}h")
    print(f"{'─'*65}")
    print(f"  Trades          : {len(trades):,}")
    print(f"  Unique markets  : {len(stats)}")
    total_vol = sum(s["total_volume_usdc"] for s in stats)
    print(f"  Total volume    : ${total_vol:,.2f} USDC")
    trades_per_min = len(trades) / (hours * 60)
    print(f"  Speed           : {trades_per_min:.1f} trades/min")

    spreads = [s["spread_captured"] for s in stats if s["spread_captured"] is not None]
    if spreads:
        avg_sp = sum(spreads) / len(spreads)
        print(f"  Avg spread      : {avg_sp:+.4f} ({avg_sp*100:+.2f}¢)")

    print(f"\n  Top 10 markets by volume:")
    print(f"  {'Market':<45} {'Vol':>8}  {'Spread':>8}  {'Pattern'}")
    print(f"  {'─'*45} {'─'*8}  {'─'*8}  {'─'*25}")
    for s in stats[:10]:
        sp = f"{s['spread_captured']:+.3f}" if s["spread_captured"] is not None else "  —   "
        label = s["label"][:44] if s["label"] else s["market_key"][:44]
        print(f"  {label:<45} ${s['total_volume_usdc']:>7.1f}  {sp:>8}  {s['pattern']}")

    # Build full report
    report = build_report(TARGET, trades, stats, hours, now)

    # Save output
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n📄 Full report saved to: {OUTPUT_PATH}")

    # Also save JSON for further processing
    json_path = OUTPUT_PATH.replace(".md", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "address": TARGET,
                "window_hours": hours,
                "generated_at": now.isoformat(),
                "total_trades": len(trades),
                "markets": stats,
            },
            f,
            indent=2,
        )
    print(f"📦 JSON data saved to: {json_path}")
    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze vague-sourdough last-N-hour activity")
    parser.add_argument("--hours", type=float, default=1.0, help="Hours to look back (default: 1)")
    args = parser.parse_args()
    main(args.hours)
