"""
Deep analysis of vague-sourdough trader on Polymarket.

Fetches the last N hours of trading activity for:
  0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d

API response fields used:
  side         - always "BUY" (API shows buy-side fills only)
  asset        - token ID (unique per outcome)
  conditionId  - market condition ID
  slug         - e.g. "btc-updown-5m-1773345000"
  title        - e.g. "Bitcoin Up or Down - March 12, 3:45PM-3:50PM ET"
  outcome      - "Up" or "Down"
  outcomeIndex - 0 or 1
  price        - price paid (0.0–1.0)
  size         - shares purchased
  timestamp    - unix epoch (seconds)

Analysis:
  - Groups by slug, then by outcome ("Up" vs "Down")
  - DUAL_SIDE_MM: both Up and Down bought in the same slug
  - Calculates cost, implied payout, expected profit/loss per slug
  - Spread = Up_avg_price + Down_avg_price (< 1.0 = guaranteed profit)
  - Speed metrics: trades per 5-min window

Saves report to: data/sourdough_analysis.md + data/sourdough_analysis.json

Usage:
    cd /content/ouroboros_repo
    python -m bots.analyze_sourdough
    python -m bots.analyze_sourdough --hours 2
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
            if v > 1e12:
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        s = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fetch_trades(address: str, since: datetime, max_pages: int = 50) -> list[dict]:
    """Fetch trades from data-api.polymarket.com/trades, stopping at `since`."""
    url = f"{DATA_API}/trades"
    trades: list[dict] = []
    offset = 0
    limit = 500

    print(f"  Fetching trades since {since.strftime('%H:%M:%S UTC')} ...")
    for page in range(max_pages):
        params = {"user": address, "limit": limit, "offset": offset}
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"  [WARN] page {page}: {e}")
            break

        if not batch or not isinstance(batch, list):
            break

        cutoff_reached = False
        for t in batch:
            ts = _parse_ts(t.get("timestamp"))
            if ts and ts < since:
                cutoff_reached = True
                break
            trades.append(t)

        print(f"  Page {page+1}: +{len(batch)} trades | total: {len(trades)}")

        if cutoff_reached or len(batch) < limit:
            break
        offset += len(batch)
        time.sleep(0.1)

    return trades


# ─────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────

def _wavg_price(trade_list: list[dict]) -> float:
    """Size-weighted average price."""
    total_cost = sum(t["price"] * t["size"] for t in trade_list)
    total_size = sum(t["size"] for t in trade_list)
    return total_cost / total_size if total_size > 0 else 0.0


def analyze_slugs(trades: list[dict]) -> list[dict[str, Any]]:
    """
    Group trades by slug → outcome, compute per-slug stats.

    Returns list of slug stat dicts sorted by total cost descending.
    """
    # slug → outcome → [trade_dicts]
    by_slug: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    slug_meta: dict[str, dict] = {}  # slug → {title, first_ts, last_ts}

    for t in trades:
        slug = t.get("slug") or t.get("conditionId") or "unknown"
        outcome = t.get("outcome") or "Unknown"
        price = float(t.get("price") or 0)
        size = float(t.get("size") or 0)
        ts = _parse_ts(t.get("timestamp"))

        by_slug[slug][outcome].append({"price": price, "size": size, "ts": ts, "raw": t})

        if slug not in slug_meta:
            slug_meta[slug] = {
                "title": t.get("title") or slug,
                "conditionId": t.get("conditionId"),
                "first_ts": ts,
                "last_ts": ts,
            }
        else:
            if ts and (slug_meta[slug]["first_ts"] is None or ts < slug_meta[slug]["first_ts"]):
                slug_meta[slug]["first_ts"] = ts
            if ts and (slug_meta[slug]["last_ts"] is None or ts > slug_meta[slug]["last_ts"]):
                slug_meta[slug]["last_ts"] = ts
            if not slug_meta[slug]["title"] or slug_meta[slug]["title"] == slug:
                slug_meta[slug]["title"] = t.get("title") or slug

    results = []
    for slug, outcome_map in by_slug.items():
        outcomes_present = sorted(outcome_map.keys())
        is_dual = len(outcomes_present) >= 2

        # Per-outcome stats
        outcome_stats: dict[str, dict] = {}
        for outcome, tlist in outcome_map.items():
            avg_p = _wavg_price(tlist)
            total_cost = sum(t["price"] * t["size"] for t in tlist)
            total_size = sum(t["size"] for t in tlist)
            outcome_stats[outcome] = {
                "trade_count": len(tlist),
                "avg_price": round(avg_p, 4),
                "total_cost": round(total_cost, 4),
                "total_size": round(total_size, 4),
            }

        total_cost_all = sum(s["total_cost"] for s in outcome_stats.values())
        trade_count_all = sum(s["trade_count"] for s in outcome_stats.values())

        # Dual-side metrics
        combined_avg_price = None
        payout = None
        expected_profit = None
        is_negative_risk = False

        if is_dual and "Up" in outcome_stats and "Down" in outcome_stats:
            up_avg = outcome_stats["Up"]["avg_price"]
            down_avg = outcome_stats["Down"]["avg_price"]
            combined_avg_price = round(up_avg + down_avg, 4)
            # Payout: one side pays 1.0 per share; use min size as guaranteed winner payout
            up_size = outcome_stats["Up"]["total_size"]
            down_size = outcome_stats["Down"]["total_size"]
            # Conservative payout = min(up_size, down_size) * 1.0
            payout = round(min(up_size, down_size), 4)
            expected_profit = round(payout - total_cost_all, 4)
            is_negative_risk = combined_avg_price < 1.0

        meta = slug_meta.get(slug, {})
        first_ts = meta.get("first_ts")
        last_ts = meta.get("last_ts")

        pattern = "DUAL_SIDE_MM" if is_dual else "SINGLE_SIDE_BUY"
        if is_dual and is_negative_risk:
            pattern = "DUAL_SIDE_MM (neg-risk)"

        results.append({
            "slug": slug,
            "title": (meta.get("title") or slug)[:80],
            "conditionId": meta.get("conditionId"),
            "pattern": pattern,
            "is_dual": is_dual,
            "is_negative_risk": is_negative_risk,
            "outcomes_present": outcomes_present,
            "outcome_stats": outcome_stats,
            "trade_count": trade_count_all,
            "total_cost": round(total_cost_all, 4),
            "combined_avg_price": combined_avg_price,
            "payout": payout,
            "expected_profit": expected_profit,
            "first_ts": first_ts.strftime("%H:%M:%S") if first_ts else None,
            "last_ts": last_ts.strftime("%H:%M:%S") if last_ts else None,
        })

    results.sort(key=lambda x: -x["total_cost"])
    return results


def price_histogram(trades: list[dict], outcome: str = "Up", bins: int = 10) -> list[str]:
    """Return ASCII histogram lines of price distribution for a given outcome."""
    prices = [
        float(t.get("price") or 0)
        for t in trades
        if (t.get("outcome") or "").strip() == outcome
    ]
    if not prices:
        return [f"  No {outcome} trades found."]

    lo, hi = 0.0, 1.0
    width = (hi - lo) / bins
    counts = [0] * bins
    for p in prices:
        idx = min(int((p - lo) / width), bins - 1)
        counts[idx] = counts[idx] + 1

    max_count = max(counts) or 1
    bar_width = 30
    lines = []
    for i, c in enumerate(counts):
        bucket_lo = lo + i * width
        bucket_hi = bucket_lo + width
        bar = "█" * int(c / max_count * bar_width)
        lines.append(f"  {bucket_lo:.2f}–{bucket_hi:.2f} | {bar:<{bar_width}} {c}")
    return lines


def speed_metrics(trades: list[dict]) -> dict[str, Any]:
    """Trades per 5-min window (keyed by slug timestamp suffix)."""
    # Extract the 5-min window epoch from slug: "btc-updown-5m-1773345000" → 1773345000
    window_counts: dict[int, int] = defaultdict(int)
    for t in trades:
        slug = t.get("slug") or ""
        parts = slug.rsplit("-", 1)
        if len(parts) == 2:
            try:
                window_ts = int(parts[1])
                window_counts[window_ts] += 1
            except ValueError:
                pass

    if not window_counts:
        # Fall back: bucket by floor(timestamp / 300)
        for t in trades:
            ts = _parse_ts(t.get("timestamp"))
            if ts:
                bucket = int(ts.timestamp()) // 300 * 300
                window_counts[bucket] += 1

    counts = list(window_counts.values())
    if not counts:
        return {"windows_seen": 0, "avg_trades_per_window": 0, "max_trades_per_window": 0}

    return {
        "windows_seen": len(counts),
        "avg_trades_per_window": round(sum(counts) / len(counts), 1),
        "max_trades_per_window": max(counts),
    }


# ─────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────

def build_report(
    address: str,
    trades: list[dict],
    slug_stats: list[dict[str, Any]],
    window_hours: float,
    generated_at: datetime,
) -> str:
    total_trades = len(trades)
    total_slugs = len(slug_stats)
    dual_slugs = [s for s in slug_stats if s["is_dual"]]
    single_slugs = [s for s in slug_stats if not s["is_dual"]]
    neg_risk_slugs = [s for s in slug_stats if s["is_negative_risk"]]

    dual_pct = len(dual_slugs) / total_slugs * 100 if total_slugs else 0
    total_cost = sum(s["total_cost"] for s in slug_stats)
    trades_per_min = total_trades / (window_hours * 60) if window_hours > 0 else 0

    speed = speed_metrics(trades)

    lines: list[str] = []
    lines.append("# vague-sourdough Strategy Analysis")
    lines.append("")
    lines.append(f"> Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"> Address: `{address}`")
    lines.append(f"> Window: Last {window_hours:.1f} hour(s)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total Trades | **{total_trades:,}** |")
    lines.append(f"| Unique Slugs (rounds) | **{total_slugs}** |")
    lines.append(f"| Total Cost (USDC) | **${total_cost:,.4f}** |")
    lines.append(f"| Trades/minute | **{trades_per_min:.1f}** |")
    lines.append(f"| 5-min windows active | **{speed['windows_seen']}** |")
    lines.append(f"| Avg trades/window | **{speed['avg_trades_per_window']}** |")
    lines.append(f"| Max trades in one window | **{speed['max_trades_per_window']}** |")
    lines.append("")
    lines.append("## Strategy Pattern Breakdown")
    lines.append("")
    lines.append(f"| Pattern | Slugs | % |")
    lines.append(f"|---------|-------|---|")
    lines.append(f"| DUAL_SIDE_MM | {len(dual_slugs)} | {dual_pct:.0f}% |")
    lines.append(f"| DUAL_SIDE_MM (neg-risk) | {len(neg_risk_slugs)} | {len(neg_risk_slugs)/total_slugs*100:.0f}% |" if total_slugs else "| DUAL_SIDE_MM (neg-risk) | 0 | — |")
    lines.append(f"| SINGLE_SIDE_BUY | {len(single_slugs)} | {100-dual_pct:.0f}% |")
    lines.append("")
    lines.append("**Interpretation:**")
    if dual_pct >= 80:
        lines.append("Dominant pattern is DUAL_SIDE_MM — the bot buys both Up and Down in most rounds.")
        lines.append("This is a negative-risk arbitrage strategy when combined_avg_price < 1.0.")
    elif dual_pct >= 40:
        lines.append("Mixed strategy: dual-side in majority of rounds, with some directional single-side bets.")
    else:
        lines.append("Mostly single-side directional buying. Dual-side is opportunistic.")
    lines.append("")

    # Negative-risk summary
    if neg_risk_slugs:
        total_expected_profit = sum(
            s["expected_profit"] for s in neg_risk_slugs if s["expected_profit"] is not None
        )
        avg_combined = sum(
            s["combined_avg_price"] for s in neg_risk_slugs if s["combined_avg_price"] is not None
        ) / len(neg_risk_slugs)
        lines.append("### Negative-Risk Rounds")
        lines.append("")
        lines.append(f"- Rounds with combined_avg_price < 1.0: **{len(neg_risk_slugs)}**")
        lines.append(f"- Avg combined price (Up+Down): **{avg_combined:.4f}**")
        lines.append(f"- Implied total expected profit: **${total_expected_profit:+.4f}**")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Per-Slug Summary (top 50 by cost)")
    lines.append("")
    lines.append("| Title | Outcomes | Trades | Cost | Up avg | Down avg | Combined | Exp P/L | Pattern |")
    lines.append("|-------|----------|--------|------|--------|----------|----------|---------|---------|")

    for s in slug_stats[:50]:
        outcomes_str = "+".join(s["outcomes_present"])
        up_avg = s["outcome_stats"].get("Up", {}).get("avg_price")
        down_avg = s["outcome_stats"].get("Down", {}).get("avg_price")
        up_str = f"{up_avg:.3f}" if up_avg is not None else "—"
        down_str = f"{down_avg:.3f}" if down_avg is not None else "—"
        comb_str = f"{s['combined_avg_price']:.3f}" if s["combined_avg_price"] is not None else "—"
        pl_str = f"${s['expected_profit']:+.4f}" if s["expected_profit"] is not None else "—"
        title = s["title"][:45]
        lines.append(
            f"| {title} | {outcomes_str} | {s['trade_count']} "
            f"| ${s['total_cost']:.3f} | {up_str} | {down_str} | {comb_str} | {pl_str} | {s['pattern']} |"
        )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Up Price Distribution")
    lines.append("")
    lines.append("Histogram of prices paid for 'Up' outcome (all trades):")
    lines.append("")
    lines.append("```")
    for row in price_histogram(trades, outcome="Up"):
        lines.append(row)
    lines.append("```")
    lines.append("")

    lines.append("## Down Price Distribution")
    lines.append("")
    lines.append("Histogram of prices paid for 'Down' outcome (all trades):")
    lines.append("")
    lines.append("```")
    for row in price_histogram(trades, outcome="Down"):
        lines.append(row)
    lines.append("```")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Strategy Conclusion")
    lines.append("")
    if dual_pct >= 50:
        lines.append(f"""**Primary Strategy: DUAL-SIDE MARKET MAKING (Negative Risk Arbitrage)**

The bot buys both Up and Down outcomes within the same 5-minute slug. When the
combined entry price (Up_avg + Down_avg) < 1.0, one side always pays out $1.00,
guaranteeing profit regardless of the outcome.

Key mechanics:
1. Enters a 5-min BTC/ETH/SOL updown market by buying BOTH Up and Down
2. Seeks slugs where Up_price + Down_price < 1.0 (negative risk / arb)
3. Guaranteed profit per round = 1.0 − (Up_cost + Down_cost) / min_size
4. Speed: ~{trades_per_min:.0f} trades/min, {speed['avg_trades_per_window']} trades/window on average

**Why both sides?** If the book is temporarily mispriced, buying both locks in a
risk-free return. The bot is not predicting direction — it's exploiting price gaps.
""")
    else:
        lines.append(f"""**Primary Strategy: DIRECTIONAL SINGLE-SIDE BUYING**

The bot predominantly buys one side (Up or Down) per round. Dual-side appears in
only {dual_pct:.0f}% of rounds, suggesting opportunistic arb rather than systematic MM.

Recommend checking Up/Down price distributions above to identify entry thresholds.
""")

    lines.append("---")
    lines.append("*Analysis by Ouroboros · Data: Polymarket Data API*")

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

    trades = fetch_trades(TARGET, since)

    if not trades:
        print("\nNo trades found in the specified window.")
        print("  1. Bot may have been inactive")
        print("  2. API rate limit / temporary unavailability")
        sys.exit(0)

    print(f"\nTotal trades fetched: {len(trades)}")

    # Analyze
    slug_stats = analyze_slugs(trades)
    total_slugs = len(slug_stats)
    dual_count = sum(1 for s in slug_stats if s["is_dual"])
    neg_risk_count = sum(1 for s in slug_stats if s["is_negative_risk"])
    total_cost = sum(s["total_cost"] for s in slug_stats)
    trades_per_min = len(trades) / (hours * 60)
    speed = speed_metrics(trades)

    # Stdout summary
    print(f"\n{'─'*65}")
    print(f"  SUMMARY — Last {hours:.1f}h")
    print(f"{'─'*65}")
    print(f"  Trades            : {len(trades):,}")
    print(f"  Unique rounds     : {total_slugs}")
    print(f"  Total cost        : ${total_cost:,.4f} USDC")
    print(f"  Speed             : {trades_per_min:.1f} trades/min")
    print(f"  Dual-side rounds  : {dual_count}/{total_slugs} ({dual_count/total_slugs*100:.0f}%)" if total_slugs else "  Dual-side rounds  : 0/0")
    print(f"  Neg-risk rounds   : {neg_risk_count}/{total_slugs}" if total_slugs else "  Neg-risk rounds   : 0/0")
    print(f"  Avg trades/window : {speed['avg_trades_per_window']}")

    print(f"\n  Top 10 rounds by cost:")
    print(f"  {'Title':<46} {'Cost':>7}  {'Combined':>8}  {'Pattern'}")
    print(f"  {'─'*46} {'─'*7}  {'─'*8}  {'─'*22}")
    for s in slug_stats[:10]:
        comb = f"{s['combined_avg_price']:.3f}" if s["combined_avg_price"] is not None else "  —   "
        title = s["title"][:45]
        print(f"  {title:<46} ${s['total_cost']:>6.3f}  {comb:>8}  {s['pattern']}")

    # Build and save report
    report = build_report(TARGET, trades, slug_stats, hours, now)

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nFull report saved to: {OUTPUT_PATH}")

    json_path = OUTPUT_PATH.replace(".md", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "address": TARGET,
                "window_hours": hours,
                "generated_at": now.isoformat(),
                "total_trades": len(trades),
                "total_slugs": total_slugs,
                "dual_side_slugs": dual_count,
                "neg_risk_slugs": neg_risk_count,
                "speed": speed,
                "slugs": slug_stats,
            },
            f,
            indent=2,
        )
    print(f"JSON data saved to: {json_path}")
    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze vague-sourdough last-N-hour activity")
    parser.add_argument("--hours", type=float, default=1.0, help="Hours to look back (default: 1)")
    args = parser.parse_args()
    main(args.hours)
