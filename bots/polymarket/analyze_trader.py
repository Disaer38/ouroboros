#!/usr/bin/env python3
"""
Deep analysis script for vague-sourdough strategy.
Address: 0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d

Run: python bots/polymarket/analyze_trader.py
"""

import json
import requests
from collections import defaultdict
from datetime import datetime

TRADER = "0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d"
DATA_API = "https://data-api.polymarket.com"

def fetch_all_activity(user: str, max_pages: int = 20) -> list:
    """Fetch all trade activity across pages."""
    all_trades = []
    offset = 0
    limit = 500
    for _ in range(max_pages):
        url = f"{DATA_API}/activity?user={user}&limit={limit}&offset={offset}"
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if not data:
            break
        all_trades.extend(data)
        if len(data) < limit:
            break
        offset += limit
    return all_trades

def fetch_positions(user: str) -> list:
    """Fetch current positions."""
    url = f"{DATA_API}/positions?user={user}&limit=500"
    return requests.get(url, timeout=15).json()

def fetch_value(user: str) -> float:
    """Fetch total portfolio value."""
    url = f"{DATA_API}/value?user={user}"
    resp = requests.get(url, timeout=15).json()
    return resp[0]["value"] if resp else 0

def analyze_strategy(trades: list, positions: list) -> dict:
    """
    Analyze the key strategy patterns:
    1. Both-sides trading (market making)
    2. Ladder buying (DCA into positions)
    3. Asset preferences
    4. Timing patterns
    5. Position sizing
    """
    # Group by market (conditionId)
    markets = defaultdict(lambda: {"up_trades": [], "down_trades": [], "title": "", "slug": ""})

    for t in trades:
        if t["type"] != "TRADE":
            continue
        cid = t["conditionId"]
        markets[cid]["title"] = t.get("title", "")
        markets[cid]["slug"] = t.get("slug", "")
        outcome = t.get("outcome", "")
        trade_info = {
            "ts": t["timestamp"],
            "price": t["price"],
            "size": t["size"],
            "usdc": t["usdcSize"],
            "side": t["side"],
        }
        if outcome == "Up":
            markets[cid]["up_trades"].append(trade_info)
        elif outcome == "Down":
            markets[cid]["down_trades"].append(trade_info)

    # Both-sides analysis
    both_sides_count = sum(1 for m in markets.values() if m["up_trades"] and m["down_trades"])
    total_markets = len(markets)

    # Ladder analysis: multiple trades in same market/direction within short timespan
    ladder_count = 0
    avg_trades_per_market_side = []
    for m in markets.values():
        for side_trades in [m["up_trades"], m["down_trades"]]:
            if len(side_trades) > 1:
                ladder_count += 1
            if side_trades:
                avg_trades_per_market_side.append(len(side_trades))

    # Price ladder detection: within same market+outcome, prices increase
    ladder_markets = 0
    for m in markets.values():
        for side_trades in [m["up_trades"], m["down_trades"]]:
            if len(side_trades) >= 3:
                prices = sorted([t["price"] for t in side_trades])
                # Check if buying across multiple price levels
                price_spread = max(prices) - min(prices)
                if price_spread > 0.1:
                    ladder_markets += 1

    # Asset distribution
    asset_counts = defaultdict(int)
    asset_usdc = defaultdict(float)
    for t in trades:
        if t["type"] != "TRADE":
            continue
        # Extract asset name from title
        title = t.get("title", "")
        if "Bitcoin" in title or "BTC" in title:
            asset = "BTC"
        elif "Ethereum" in title or "ETH" in title:
            asset = "ETH"
        elif "Solana" in title or "SOL" in title:
            asset = "SOL"
        elif "XRP" in title:
            asset = "XRP"
        else:
            asset = "OTHER"
        asset_counts[asset] += 1
        asset_usdc[asset] += t.get("usdcSize", 0)

    # Timing: how many 5min windows covered
    timestamps = [t["timestamp"] for t in trades if t["type"] == "TRADE"]
    time_windows = set(ts // 300 * 300 for ts in timestamps)

    # Position sizing stats
    usdc_sizes = [t["usdcSize"] for t in trades if t["type"] == "TRADE" and t["usdcSize"] > 0]
    avg_trade_usdc = sum(usdc_sizes) / len(usdc_sizes) if usdc_sizes else 0
    max_trade_usdc = max(usdc_sizes) if usdc_sizes else 0
    total_usdc_volume = sum(usdc_sizes)

    # Price entry analysis: what price levels does he prefer?
    prices = [t["price"] for t in trades if t["type"] == "TRADE"]
    low_prob_entries = sum(1 for p in prices if p < 0.3)   # <30% probability
    high_prob_entries = sum(1 for p in prices if p > 0.7)  # >70% probability
    mid_prob_entries = sum(1 for p in prices if 0.3 <= p <= 0.7)

    # Current positions P&L summary
    winning_positions = [p for p in positions if p.get("cashPnl", 0) > 0]
    losing_positions = [p for p in positions if p.get("cashPnl", 0) < 0]
    total_unrealized_pnl = sum(p.get("cashPnl", 0) for p in positions)

    # Detect "both sides" for current positions
    position_markets = defaultdict(list)
    for p in positions:
        position_markets[p["conditionId"]].append(p)
    hedged_markets = sum(1 for ps in position_markets.values() if len(ps) == 2)

    return {
        "total_trades": len([t for t in trades if t["type"] == "TRADE"]),
        "total_markets": total_markets,
        "both_sides_markets": both_sides_count,
        "both_sides_pct": both_sides_count / total_markets * 100 if total_markets else 0,
        "ladder_count": ladder_count,
        "ladder_markets_with_wide_spread": ladder_markets,
        "avg_trades_per_side": sum(avg_trades_per_market_side) / len(avg_trades_per_market_side) if avg_trades_per_market_side else 0,
        "time_windows_covered": len(time_windows),
        "asset_distribution": dict(asset_counts),
        "asset_usdc_volume": {k: round(v, 2) for k, v in asset_usdc.items()},
        "avg_trade_usdc": round(avg_trade_usdc, 2),
        "max_trade_usdc": round(max_trade_usdc, 2),
        "total_usdc_volume": round(total_usdc_volume, 2),
        "price_distribution": {
            "low_prob_<30pct": low_prob_entries,
            "mid_prob_30-70pct": mid_prob_entries,
            "high_prob_>70pct": high_prob_entries,
        },
        "current_unrealized_pnl": round(total_unrealized_pnl, 2),
        "winning_positions": len(winning_positions),
        "losing_positions": len(losing_positions),
        "hedged_markets_now": hedged_markets,
    }

def detect_core_strategy(stats: dict) -> dict:
    """Identify the exact strategy from statistics."""
    strategy = {
        "name": "",
        "description": "",
        "key_behaviors": [],
        "edge_source": "",
        "risk_profile": "",
        "replication_difficulty": "",
    }

    both_sides_pct = stats["both_sides_pct"]
    price_dist = stats["price_distribution"]

    if both_sides_pct > 60:
        strategy["name"] = "DUAL-SIDE MARKET MAKER / LIQUIDITY PROVIDER"
        strategy["description"] = (
            "Trader buys BOTH Up and Down outcomes in the same market simultaneously. "
            "This is not directional betting — it is market making. By providing liquidity "
            "on both sides, he profits from the spread (the fact that Up+Down < $1.00)."
        )
        strategy["key_behaviors"] = [
            f"Enters both sides in {both_sides_pct:.0f}% of markets",
            "Uses ladder/DCA approach: multiple small orders at different price levels",
            "Focuses on 5-minute BTC/ETH/SOL/XRP intraday markets",
            "High-frequency: covers many 5-min windows per session",
            f"Avg trade size: ${stats['avg_trade_usdc']:.2f} USDC",
            f"Total volume analyzed: ${stats['total_usdc_volume']:.2f} USDC",
        ]
        strategy["edge_source"] = (
            "The edge comes from the SPREAD. In a 5-min Up/Down market, "
            "if Up = 0.25 and Down = 0.75, buying both costs $1.00 (no edge). "
            "But if the market is inefficient — e.g., Up = 0.18, Down = 0.79 (sum = 0.97) — "
            "buying BOTH guarantees $1.00 payout for $0.97 cost = 3% risk-free profit. "
            "This is a classic arbitrage on market inefficiency."
        )
        strategy["risk_profile"] = (
            "Low-medium. The strategy profits regardless of which side wins, "
            "as long as the entry spread is favorable. Risk: market becomes "
            "efficient before full position is built."
        )
        strategy["replication_difficulty"] = "MEDIUM — requires fast execution and spread monitoring"

    # Price preference analysis
    low = price_dist["low_prob_<30pct"]
    high = price_dist["high_prob_>70pct"]
    total_trades = stats["total_trades"]

    if low / total_trades > 0.3:
        strategy["key_behaviors"].append(
            f"Prefers buying LOW probability outcomes ({low/total_trades*100:.0f}% of trades <30¢) — "
            "these have the highest upside if they win"
        )
    if high / total_trades > 0.3:
        strategy["key_behaviors"].append(
            f"Also buys HIGH probability outcomes ({high/total_trades*100:.0f}% of trades >70¢) — "
            "these are the 'certain' side of the hedge"
        )

    return strategy

def print_report(stats: dict, strategy: dict, portfolio_value: float):
    """Print human-readable analysis report."""
    print("\n" + "="*70)
    print("  VAGUE-SOURDOUGH STRATEGY ANALYSIS")
    print("  Address: 0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d")
    print("="*70)

    print(f"\n📊 PORTFOLIO SNAPSHOT")
    print(f"  Total portfolio value:  ${portfolio_value:,.2f}")
    print(f"  Unrealized P&L (open):  ${stats['current_unrealized_pnl']:+,.2f}")
    print(f"  Winning positions:      {stats['winning_positions']}")
    print(f"  Losing positions:       {stats['losing_positions']}")
    print(f"  Hedged markets now:     {stats['hedged_markets_now']}")

    print(f"\n📈 TRADING ACTIVITY")
    print(f"  Total trades analyzed:  {stats['total_trades']}")
    print(f"  Unique markets:         {stats['total_markets']}")
    print(f"  Both-sides markets:     {stats['both_sides_markets']} ({stats['both_sides_pct']:.0f}%)")
    print(f"  5-min windows covered:  {stats['time_windows_covered']}")
    print(f"  Avg trades/side:        {stats['avg_trades_per_side']:.1f}")

    print(f"\n💰 POSITION SIZING")
    print(f"  Avg trade (USDC):       ${stats['avg_trade_usdc']:.2f}")
    print(f"  Max single trade:       ${stats['max_trade_usdc']:.2f}")
    print(f"  Total USDC volume:      ${stats['total_usdc_volume']:.2f}")

    print(f"\n🎯 ASSET DISTRIBUTION")
    for asset, count in sorted(stats['asset_distribution'].items(), key=lambda x: -x[1]):
        vol = stats['asset_usdc_volume'].get(asset, 0)
        print(f"  {asset}: {count} trades, ${vol:.2f} USDC volume")

    print(f"\n🔢 ENTRY PRICE DISTRIBUTION")
    pd = stats['price_distribution']
    total = stats['total_trades']
    print(f"  <30¢ (low prob):   {pd['low_prob_<30pct']:4d} ({pd['low_prob_<30pct']/total*100:.0f}%)")
    print(f"  30-70¢ (mid prob): {pd['mid_prob_30-70pct']:4d} ({pd['mid_prob_30-70pct']/total*100:.0f}%)")
    print(f"  >70¢ (high prob):  {pd['high_prob_>70pct']:4d} ({pd['high_prob_>70pct']/total*100:.0f}%)")

    print(f"\n🧠 STRATEGY IDENTIFIED: {strategy['name']}")
    print(f"\n  {strategy['description']}")

    print(f"\n📋 KEY BEHAVIORS:")
    for b in strategy['key_behaviors']:
        print(f"  • {b}")

    print(f"\n⚡ EDGE SOURCE:")
    print(f"  {strategy['edge_source']}")

    print(f"\n⚖️ RISK PROFILE:")
    print(f"  {strategy['risk_profile']}")

    print(f"\n🔄 REPLICATION: {strategy['replication_difficulty']}")

    print("\n" + "="*70)
    print("  HOW TO REPLICATE THIS STRATEGY")
    print("="*70)
    print("""
  1. SCAN 5-MIN MARKETS: Watch BTC/ETH/SOL/XRP 5-min Up/Down markets
     that open every 5 minutes on Polymarket.

  2. CHECK SPREAD: For each market, fetch orderbook for both Up and Down.
     Calculate: Up_best_ask + Down_best_ask
     If sum < 1.00: there is an arb opportunity!
     Example: Up=0.18, Down=0.79 → sum=0.97 → 3% guaranteed profit

  3. BUY BOTH SIDES: Place buy orders on both Up AND Down outcomes.
     Use ladder orders (multiple price levels) to accumulate.

  4. WAIT FOR RESOLUTION: 5-min market resolves → one side pays $1,
     other side pays $0. Net result: profit = (1 - sum_of_entries) per pair.

  5. POSITION SIZING: Start small ($1-5 per trade), scale based on spread.
     Target: 2-5% spread minimum to cover fees.

  NOTE: This requires sub-second execution. The spread windows are brief.
  A bot is essential — manual trading is too slow.
""")


def main():
    print("Fetching trade history...")
    trades = fetch_all_activity(TRADER, max_pages=5)
    print(f"  Fetched {len(trades)} trades")

    print("Fetching current positions...")
    positions = fetch_positions(TRADER)
    print(f"  Fetched {len(positions)} positions")

    print("Fetching portfolio value...")
    portfolio_value = fetch_value(TRADER)
    print(f"  Portfolio value: ${portfolio_value:,.2f}")

    print("Analyzing strategy...")
    stats = analyze_strategy(trades, positions)
    strategy = detect_core_strategy(stats)

    print_report(stats, strategy, portfolio_value)

    # Save JSON results
    result = {
        "trader": TRADER,
        "analyzed_at": datetime.utcnow().isoformat(),
        "portfolio_value": portfolio_value,
        "stats": stats,
        "strategy": strategy,
    }
    with open("bots/polymarket/strategy_analysis.json", "w") as f:
        json.dump(result, f, indent=2)
    print("\n✅ Full analysis saved to bots/polymarket/strategy_analysis.json")


if __name__ == "__main__":
    main()
