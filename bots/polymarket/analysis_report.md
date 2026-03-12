# Strategy Analysis — vague-sourdough

**Address:** `0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d`  
**Analyzed at:** 2026-03-12 19:53:11 UTC  
**Time window:** Last 1 hour  

---

## 🧠 Strategy Identified

### **NEG-RISK / SPREAD ARBITRAGE** (confidence: HIGH)

This is **not** directional betting. The trader buys **both** Up and Down outcomes
in the same 5-minute market. In 83% of markets they traded, they were on **both sides**.
In 9 out of 42 markets, the combined entry prices were **less than $1.00**,
which means the payout is **mathematically guaranteed to exceed the cost**.

**How the edge works:**
- Market opens: `BTC-Up = 0.63`, `BTC-Down = 0.17` → sum = **0.80**
- Buy 1 share of each side for $0.80 total
- Market resolves: one side pays $1.00, other $0.00
- Net: $1.00 − $0.80 = **+$0.20 profit (25%)** — guaranteed regardless of direction
- Best observed spread this hour: **+19.6%** on a single BTC round

---

## 📊 Key Stats (Last Hour)

| Metric | Value |
|--------|-------|
| Trades | **590** |
| Unique markets | **42** |
| Both-sides markets | **35 (83.3%)** |
| Neg-risk rounds (sum < 1.0) | **9** |
| Ladder/DCA series | **57** |
| 5-min windows covered | **11** (~11 rounds) |
| Avg trade size | **$21.8** USDC |
| Max trade size | **$175.5** USDC |
| Total USDC volume | **$12,864.07** |
| Trade rate | **10.8 trades/min** |

---

## 🎯 Asset Distribution

| Asset | Trades | Volume (USDC) |
|-------|--------|---------------|
| BTC | 206 | $5,985.23 |
| SOL | 158 | $2,328.86 |
| XRP | 132 | $1,879.40 |
| ETH | 94 | $2,670.57 |

**BTC dominates** (35% of trades, 46% of volume).
All assets are 5-minute Up/Down binary markets.

---

## 🔢 Entry Price Distribution

| Band | % of trades |
|------|------------|
| < 30¢ (low probability) | 14.2% |
| 30–70¢ (mid probability) | 62.4% |
| > 70¢ (high probability) | 23.4% |

62% of entries are in the 30-70¢ zone — this is where both Up and Down live
when the market is near 50/50 (sum ≈ 1.0). The 23% high-prob entries are
the "sure" side of a neg-risk pair (e.g., Up=0.80 + Down=0.17 = 0.97).

---

## 🔬 Best Neg-Risk Opportunities (Last Hour)

| Market | Avg Up | Avg Down | Guaranteed Profit |
|--------|--------|----------|-------------------|
| Bitcoin Up or Down - March 12, 3:30PM-3:35PM ET | 0.637 | 0.166 | **19.64%** |
| Bitcoin Up or Down - March 12, 3:10PM-3:15PM ET | 0.429 | 0.416 | **15.48%** |
| Solana Up or Down - March 12, 3:45PM-3:50PM ET | 0.45 | 0.405 | **14.5%** |
| Solana Up or Down - March 12, 3:10PM-3:15PM ET | 0.09 | 0.77 | **14.0%** |
| Solana Up or Down - March 12, 3:05PM-3:10PM ET | 0.463 | 0.51 | **2.69%** |

**Conclusion:** In some rounds the spread reached 19.6%. At $100 deployed,
that is $19.60 guaranteed return in 5 minutes. Annualized: astronomical.

---

## 📉 Ladder / DCA Behavior

57 distinct ladder series detected — multiple buy orders at escalating price
levels in the same market and outcome. This serves two purposes:

1. **Fills at different market depths** — avoids moving the price too much
2. **Accumulates position even as spread narrows** — early orders lock in the best spread,
   later orders still profitable but at lower margin

---

## 🔄 Full Replication Blueprint

```
STEP 1: MONITOR  — Watch for new 5-min Up/Down market opening
        Assets: BTC, ETH, SOL, XRP (in that priority order)
        API: GET https://gamma-api.polymarket.com/events?tag=crypto&active=true

STEP 2: CHECK    — Fetch best ask for both Up and Down tokens
        CLOB: GET https://clob.polymarket.com/book?token_id={asset_id}
        Compute: sum = ask_up + ask_down
        Threshold: sum < 0.97  (3% spread after ~0.5% fees)

STEP 3: ENTER    — If sum < 0.97, buy BOTH sides
        Use ladder: 3-5 orders at different price levels
        Sizing: $10-20 per order, total $50-100 per round
        Speed: must be within 60s of market open (spread closes fast)

STEP 4: HOLD     — No action needed. Market resolves in ≤5 min.

STEP 5: COLLECT  — Winning side pays $1/share.
        Gross PnL = shares * (1.0 - avg_entry_sum)
        Net  PnL = Gross - Polymarket fees (0.5%)

EXPECTED EDGE: 2-20% per round when spread is available.
FREQUENCY: ~6-12 rounds/hour across BTC/ETH/SOL/XRP.
WIN RATE: ~100% (math, not prediction).
BOTTLENECK: Speed to detect and enter before spread closes.
```

---

## ⚠️ Key Risks

1. **Spread closes before full fill** — partial fill on one side = directional exposure
2. **Market resolution manipulation** — edge case, very rare on Polymarket
3. **Fees erode edge** — at 2% spread, fees eat ~25% of profit
4. **Liquidity** — large orders move the price, reducing effective spread
5. **Bot competition** — other bots see the same spread, race to fill first
