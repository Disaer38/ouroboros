# vague-sourdough Strategy Analysis

> Generated: 2026-03-12 19:57:10 UTC
> Address: `0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d`
> Window: Last 1.0 hour(s)

---

## Overview

| Metric | Value |
|--------|-------|
| Total Trades | **638** |
| Unique Slugs (rounds) | **47** |
| Total Cost (USDC) | **$14,057.2796** |
| Trades/minute | **10.6** |
| 5-min windows active | **12** |
| Avg trades/window | **53.2** |
| Max trades in one window | **105** |

## Strategy Pattern Breakdown

| Pattern | Slugs | % |
|---------|-------|---|
| DUAL_SIDE_MM | 40 | 85% |
| DUAL_SIDE_MM (neg-risk) | 10 | 21% |
| SINGLE_SIDE_BUY | 7 | 15% |

**Interpretation:**
Dominant pattern is DUAL_SIDE_MM — the bot buys both Up and Down in most rounds.
This is a negative-risk arbitrage strategy when combined_avg_price < 1.0.

### Negative-Risk Rounds

- Rounds with combined_avg_price < 1.0: **10**
- Avg combined price (Up+Down): **0.8822**
- Implied total expected profit: **$+367.6027**

---

## Per-Slug Summary (top 50 by cost)

| Title | Outcomes | Trades | Cost | Up avg | Down avg | Combined | Exp P/L | Pattern |
|-------|----------|--------|------|--------|----------|----------|---------|---------|
| Bitcoin Up or Down - March 12, 3:05PM-3:10PM  | Down+Up | 29 | $855.966 | 0.644 | 0.532 | 1.176 | $-487.5556 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:00PM-3:05PM  | Down+Up | 21 | $853.916 | 0.653 | 0.434 | 1.088 | $-256.6622 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:10PM-3:15PM  | Down+Up | 34 | $747.802 | 0.395 | 0.181 | 0.576 | $+404.0696 | DUAL_SIDE_MM (neg-risk) |
| XRP Up or Down - March 12, 3:10PM-3:15PM ET | Down+Up | 24 | $637.769 | 0.648 | 0.616 | 1.264 | $-197.4979 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:20PM-3:25PM  | Down+Up | 21 | $635.520 | 0.641 | 0.378 | 1.019 | $-289.8551 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:40PM-3:45PM E | Down+Up | 31 | $599.672 | 0.637 | 0.446 | 1.083 | $-88.4663 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:40PM-3:45PM  | Down+Up | 18 | $586.752 | 0.306 | 0.745 | 1.052 | $-163.7244 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:35PM-3:40PM  | Down+Up | 19 | $557.410 | 0.349 | 0.672 | 1.021 | $-140.5791 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:50PM-3:55PM  | Down+Up | 21 | $548.444 | 0.433 | 0.637 | 1.070 | $-44.2818 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:25PM-3:30PM  | Down+Up | 15 | $532.449 | 0.674 | 0.394 | 1.067 | $-236.7129 | DUAL_SIDE_MM |
| Ethereum Up or Down - March 12, 3:35PM-3:40PM | Down+Up | 16 | $516.527 | 0.537 | 0.559 | 1.096 | $-67.5372 | DUAL_SIDE_MM |
| Ethereum Up or Down - March 12, 3:05PM-3:10PM | Down+Up | 21 | $501.755 | 0.548 | 0.412 | 0.961 | $+5.4892 | DUAL_SIDE_MM (neg-risk) |
| Ethereum Up or Down - March 12, 3:40PM-3:45PM | Down+Up | 14 | $480.377 | 0.505 | 0.729 | 1.233 | $-165.0650 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:45PM-3:50PM  | Down+Up | 21 | $458.550 | 0.614 | 0.513 | 1.127 | $-215.0937 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:35PM-3:40PM E | Down+Up | 23 | $446.960 | 0.409 | 0.673 | 1.082 | $-126.8344 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:05PM-3:10PM E | Down+Up | 28 | $446.456 | 0.275 | 0.624 | 0.899 | $-13.2094 | DUAL_SIDE_MM (neg-risk) |
| Solana Up or Down - March 12, 3:20PM-3:25PM E | Down+Up | 30 | $404.222 | 0.312 | 0.631 | 0.943 | $-37.0601 | DUAL_SIDE_MM (neg-risk) |
| Bitcoin Up or Down - March 12, 3:15PM-3:20PM  | Down+Up | 17 | $352.807 | 0.620 | 0.294 | 0.914 | $+31.7200 | DUAL_SIDE_MM (neg-risk) |
| XRP Up or Down - March 12, 3:05PM-3:10PM ET | Down+Up | 27 | $315.513 | 0.348 | 0.615 | 0.963 | $+7.0282 | DUAL_SIDE_MM (neg-risk) |
| XRP Up or Down - March 12, 3:35PM-3:40PM ET | Down+Up | 17 | $307.807 | 0.448 | 0.677 | 1.125 | $-51.1778 | DUAL_SIDE_MM |
| Ethereum Up or Down - March 12, 3:00PM-3:05PM | Down+Up | 12 | $265.061 | 0.587 | 0.540 | 1.127 | $-63.2793 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:45PM-3:50PM E | Down+Up | 19 | $259.392 | 0.837 | 0.243 | 1.080 | $-79.6115 | DUAL_SIDE_MM |
| Ethereum Up or Down - March 12, 3:50PM-3:55PM | Down+Up | 6 | $256.800 | 0.339 | 0.738 | 1.076 | $-24.2865 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:50PM-3:55PM E | Down+Up | 11 | $241.474 | 0.424 | 0.662 | 1.087 | $-59.3321 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:00PM-3:05PM E | Down+Up | 15 | $227.684 | 0.582 | 0.397 | 0.979 | $-18.1515 | DUAL_SIDE_MM (neg-risk) |
| Ethereum Up or Down - March 12, 3:45PM-3:50PM | Down+Up | 7 | $221.724 | 0.748 | 0.359 | 1.107 | $-33.2507 | DUAL_SIDE_MM |
| Bitcoin Up or Down - March 12, 3:30PM-3:35PM  | Down+Up | 10 | $220.020 | 0.561 | 0.166 | 0.728 | $+23.9598 | DUAL_SIDE_MM (neg-risk) |
| XRP Up or Down - March 12, 3:45PM-3:50PM ET | Down+Up | 14 | $200.928 | 0.700 | 0.495 | 1.195 | $-51.7088 | DUAL_SIDE_MM |
| Ethereum Up or Down - March 12, 3:30PM-3:35PM | Up | 3 | $160.800 | 0.538 | — | — | — | SINGLE_SIDE_BUY |
| Ethereum Up or Down - March 12, 3:20PM-3:25PM | Down+Up | 6 | $141.865 | 0.747 | 0.555 | 1.302 | $-84.9756 | DUAL_SIDE_MM |
| XRP Up or Down - March 12, 3:00PM-3:05PM ET | Down+Up | 12 | $139.780 | 0.865 | 0.375 | 1.240 | $-68.2101 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:25PM-3:30PM E | Down+Up | 10 | $128.877 | 0.691 | 0.519 | 1.210 | $-74.7669 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:15PM-3:20PM E | Down+Up | 10 | $94.448 | 0.834 | 0.383 | 1.217 | $-23.4025 | DUAL_SIDE_MM |
| XRP Up or Down - March 12, 3:50PM-3:55PM ET | Down+Up | 9 | $83.919 | 0.432 | 0.566 | 0.999 | $-22.9293 | DUAL_SIDE_MM (neg-risk) |
| Ethereum Up or Down - March 12, 3:25PM-3:30PM | Down+Up | 3 | $78.300 | 0.494 | 0.520 | 1.014 | $-48.3000 | DUAL_SIDE_MM |
| XRP Up or Down - March 12, 3:30PM-3:35PM ET | Up | 6 | $66.094 | 0.811 | — | — | — | SINGLE_SIDE_BUY |
| Ethereum Up or Down - March 12, 3:10PM-3:15PM | Down | 3 | $62.745 | — | 0.676 | — | — | SINGLE_SIDE_BUY |
| Bitcoin Up or Down - March 12, 2:55PM-3:00PM  | Down+Up | 3 | $60.507 | 0.520 | 0.700 | 1.220 | $-31.6472 | DUAL_SIDE_MM |
| XRP Up or Down - March 12, 3:20PM-3:25PM ET | Down+Up | 4 | $57.294 | 0.871 | 0.705 | 1.575 | $-37.4736 | DUAL_SIDE_MM |
| Solana Up or Down - March 12, 3:30PM-3:35PM E | Up | 6 | $48.979 | 0.843 | — | — | — | SINGLE_SIDE_BUY |
| XRP Up or Down - March 12, 3:25PM-3:30PM ET | Down+Up | 5 | $47.845 | 0.713 | 0.535 | 1.248 | $-40.7053 | DUAL_SIDE_MM |
| XRP Up or Down - March 12, 3:40PM-3:45PM ET | Down+Up | 6 | $46.538 | 0.408 | 0.696 | 1.104 | $-20.4976 | DUAL_SIDE_MM |
| Ethereum Up or Down - March 12, 3:15PM-3:20PM | Down+Up | 4 | $44.103 | 0.757 | 0.262 | 1.019 | $-34.7231 | DUAL_SIDE_MM |
| Ethereum Up or Down - March 12, 2:55PM-3:00PM | Down | 1 | $42.500 | — | 0.849 | — | — | SINGLE_SIDE_BUY |
| Solana Up or Down - March 12, 2:55PM-3:00PM E | Down | 1 | $37.600 | — | 0.940 | — | — | SINGLE_SIDE_BUY |
| Solana Up or Down - March 12, 3:10PM-3:15PM E | Down+Up | 3 | $18.314 | 0.090 | 0.772 | 0.862 | $-13.3138 | DUAL_SIDE_MM (neg-risk) |
| XRP Up or Down - March 12, 2:55PM-3:00PM ET | Down | 2 | $17.014 | — | 0.798 | — | — | SINGLE_SIDE_BUY |

---

## Up Price Distribution

Histogram of prices paid for 'Up' outcome (all trades):

```
  0.00–0.10 | ██                             6
  0.10–0.20 | ███                            8
  0.20–0.30 | ████████                       20
  0.30–0.40 | ██████████████████             42
  0.40–0.50 | ██████████████████████████████ 67
  0.50–0.60 | ████████████████████████       54
  0.60–0.70 | ████████████████████           45
  0.70–0.80 | █████████████████              38
  0.80–0.90 | ██████████████                 33
  0.90–1.00 | ███████████                    25
```

## Down Price Distribution

Histogram of prices paid for 'Down' outcome (all trades):

```
  0.00–0.10 | ██                             6
  0.10–0.20 | ███████                        21
  0.20–0.30 | █████████                      24
  0.30–0.40 | ████████████                   32
  0.40–0.50 | ███████████                    31
  0.50–0.60 | ██████████████████████████████ 80
  0.60–0.70 | ███████████████████            51
  0.70–0.80 | █████████████                  36
  0.80–0.90 | ███                            10
  0.90–1.00 | ███                            9
```

---

## Strategy Conclusion

**Primary Strategy: DUAL-SIDE MARKET MAKING (Negative Risk Arbitrage)**

The bot buys both Up and Down outcomes within the same 5-minute slug. When the
combined entry price (Up_avg + Down_avg) < 1.0, one side always pays out $1.00,
guaranteeing profit regardless of the outcome.

Key mechanics:
1. Enters a 5-min BTC/ETH/SOL updown market by buying BOTH Up and Down
2. Seeks slugs where Up_price + Down_price < 1.0 (negative risk / arb)
3. Guaranteed profit per round = 1.0 − (Up_cost + Down_cost) / min_size
4. Speed: ~11 trades/min, 53.2 trades/window on average

**Why both sides?** If the book is temporarily mispriced, buying both locks in a
risk-free return. The bot is not predicting direction — it's exploiting price gaps.

---
*Analysis by Ouroboros · Data: Polymarket Data API*