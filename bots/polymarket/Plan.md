# Polymarket Dominant Side Paper Trader — Architecture Plan

**Version**: 1.0  
**Date**: 2026-03-13  
**Strategy**: "Dominant Side" — buy the outcome with P ≥ 0.60 on 5-minute crypto markets  
**Status**: Phase 1 complete — Phase 2 in progress

---

## 1. Executive Summary

We are building a **paper trading bot** that implements the "Dominant Side" strategy on Polymarket's 5-minute BTC/ETH/SOL/XRP Up-or-Down markets.

The strategy thesis (derived from vague-sourdough analysis):
> In very short-term binary markets, when one outcome drifts to ≥60% probability, market conviction is directional. The dominant side wins more often than random walk predicts. We buy it.

This is fundamentally different from the **neg-risk arbitrage** (buy both sides) that the existing `paper_trader.py` implements.

---

## 2. Existing Code Inventory

| File | Status | Decision |
|------|--------|----------|
| `models.py` | Good | Reuse `Market`, `Orderbook`. Add `DominantTrade`. |
| `market_discovery.py` | Excellent | Reuse as-is. `get_active_market()` + `MarketWatcher` solid. |
| `clob_client.py` | Excellent | Reuse as-is. Production-grade. |
| `scanner.py` | Partial | Reuse `discover_markets()`. Skip neg-risk logic. |
| `paper_trader.py` | Wrong strategy | Neg-risk only. Keep separate, do not extend. |
| `strategy_sourdough.py` | Partial | Some good patterns, but messy. Rewrite clean. |
| `paper_mm.py` | Different | MM engine. Unrelated. Keep separate. |
| `run_paper.py` | Wrong strategy | Entry for neg-risk. Write new `run_dominant.py`. |

---

## 3. Target Architecture

```
bots/polymarket/
├── models.py                   # EXISTING — add DominantTrade dataclass
├── market_discovery.py         # EXISTING — no changes
├── clob_client.py              # EXISTING — no changes
├── scanner.py                  # EXISTING — reuse discover_markets()
│
├── dominant/                   # NEW — Dominant Side strategy package
│   ├── __init__.py
│   ├── config.py               # Strategy parameters (thresholds, bet sizes)
│   ├── signal.py               # Core signal: P(YES) vs threshold → BUY/SKIP
│   ├── exchange.py             # PaperExchange — virtual order execution & balance
│   ├── engine.py               # TradingEngine — main loop
│   └── reporter.py             # Telegram reporting + Drive logging
│
└── run_dominant.py             # NEW — Entry point
```

---

## 4. Data Flow

```
MarketWatcher (market_discovery.py)
    │
    ▼
ClobClient.get_both_books() → (ob_yes, ob_no)
    │
    ▼
signal.generate(ob_yes, ob_no, market) → Signal
    │
    ├─ NO_SIGNAL → sleep
    │
    └─ BUY_YES / BUY_NO
         │
         ▼
    PaperExchange.buy(signal, market)
         │
         ├─ can_buy() checks (exposure, open trades, balance)
         │
         └─ Create DominantTrade → log to Drive
              │
              ▼ (at market expiry + 30s)
         PaperExchange.resolve(market_id, outcome)
              │
              └─ Update PnL → reporter
```

---

## 5. Module Interfaces

### 5.1 `dominant/config.py`

```python
@dataclass
class DominantConfig:
    min_dominant_prob: float = 0.60
    min_time_remaining_sec: float = 60.0
    bet_size_usdc: float = 10.0
    max_open_trades: int = 4
    max_daily_loss_usdc: float = 100.0
    assets: list = field(default_factory=lambda: ["btc", "eth"])
    scan_interval_sec: float = 5.0
```

### 5.2 `dominant/signal.py`

Input: two orderbooks (YES + NO)
Output: Signal(side, prob, price, depth_usdc, time_remaining)

Logic:
- p_yes = ob_yes.best_ask (ask price ≈ implied probability)
- if p_yes >= 0.60 → BUY_YES
- if p_yes <= 0.40 → BUY_NO (p_no = 1 - p_yes >= 0.60)
- else → NO_SIGNAL

### 5.3 `dominant/exchange.py` — PaperExchange

```python
class PaperExchange:
    balance: float                              # Virtual USDC
    positions: dict[str, DominantPosition]     # Open positions
    closed_trades: list[DominantTrade]

    def buy(self, signal, market) -> Optional[DominantTrade]
    def can_buy(self, signal) -> tuple[bool, str]
    def resolve(self, market_id, outcome) -> Optional[DominantTrade]
    def resolve_expired(self) -> list[DominantTrade]

    @property open_exposure_usdc: float
    @property realized_pnl: float
    @property win_rate: float
```

### 5.4 `dominant/engine.py` — TradingEngine

Main async loop:
1. For each asset: get active market → fetch books → signal → trade
2. Resolve expired positions (poll Gamma for resolution)
3. Sleep scan_interval_sec
4. Repeat until stopped

---

## 6. Key Design Decisions

**Probability proxy**: Use `best_ask` as the implied probability. On Polymarket, the ask price for a binary outcome IS the market's probability estimate.

**Resolution**: After `end_date + 30s`, poll Gamma API for market resolution field. Retry up to 3 times. Fallback: use price at expiry as proxy.

**No early exit**: Hold to expiry. Binary: $1.00 win or $0.00 loss. No take-profit in v1.

**One trade per round**: Enforce via `_traded_market_ids` set per session.

**Taker fee**: 2% applied to each fill. `cost = shares * price * 1.02`.

---

## 7. Risk Management

| Rule | Value |
|------|-------|
| Min probability | 0.60 |
| Min time remaining | 60s |
| Bet size | $10 USDC (fixed) |
| Max open positions | 4 |
| Max daily loss | $100 |
| One trade per market | Yes |

---

## 8. Success Metrics (48h Paper Run)

| Metric | Target |
|--------|--------|
| Win rate | > 55% |
| Avg PnL per trade | > $0.50 after fees |
| Total trades | > 50 |
| Max drawdown | < 20% |

---

## 9. Implementation Phases

### Phase 1 — Architecture & Setup ✅ DONE
- [x] Review existing code
- [x] Design architecture
- [x] Create Plan.md
- [x] Create `dominant/` package skeleton

### Phase 2 — Core Implementation
- [ ] `dominant/config.py`
- [ ] `dominant/signal.py`
- [ ] `dominant/exchange.py` (PaperExchange)
- [ ] Add `DominantTrade` to `models.py`

### Phase 3 — Engine & Runner
- [ ] `dominant/engine.py`
- [ ] `dominant/reporter.py`
- [ ] `run_dominant.py`

### Phase 4 — Live Paper Run
- [ ] First 5-min test run
- [ ] 48-hour autonomous run
- [ ] Analyze results

### Phase 5 — Optimization (if edge confirmed)
- [ ] Multi-asset simultaneous scanning
- [ ] Dynamic bet sizing (Kelly)
- [ ] Slippage simulation

---

*Architecture: March 13, 2026 | Lead: Ouroboros*