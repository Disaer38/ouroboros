# Polymarket High-Frequency Arbitrage Bot — Architecture

> **Strategy**: Negative Risk Arbitrage on 5-minute BTC/ETH binary markets.  
> **Target**: Replicate `guh123` (Wry-Leaker) — $3,900/day, 13.5 trades/min.  
> **Stack**: Python (primary) for direct `py-clob-client` integration.

---

## Table of Contents

1. [Strategy Math](#1-strategy-math)
2. [System Overview](#2-system-overview)
3. [Component Breakdown](#3-component-breakdown)
   - [3.1 Market Registry](#31-market-registry)
   - [3.2 Ingestion Layer (WebSocket + REST)](#32-ingestion-layer-websocket--rest)
   - [3.3 Opportunity Scanner](#33-opportunity-scanner)
   - [3.4 Execution Engine](#34-execution-engine)
   - [3.5 Position Tracker](#35-position-tracker)
   - [3.6 Logger / Telemetry](#36-logger--telemetry)
4. [Data Flow](#4-data-flow)
5. [Fee Structure & Profitability Model](#5-fee-structure--profitability-model)
6. [Tech Stack](#6-tech-stack)
7. [Configuration Reference](#7-configuration-reference)
8. [Deployment](#8-deployment)

---

## 1. Strategy Math

### 1.1 Core Invariant

In a binary market with outcomes **Up** and **Down**, exactly one side pays $1.00 at resolution. If:

```
ask_up + ask_down < 1.00
```

…buying both sides guarantees a **risk-free profit** regardless of outcome.

### 1.2 Profit Formula

```
gross_profit = 1.00 - ask_up - ask_down           # per share pair
net_profit   = gross_profit - total_fees          # after all costs
```

For a trade of `N` shares on each side:

```
P&L = N × (1.00 - ask_up - ask_down - fee_per_share_up - fee_per_share_down)
```

### 1.3 Fee Structure

Polymarket CLOB fees (2026):

| Fee Type         | Rate       | Notes                              |
|------------------|------------|------------------------------------|
| Taker fee        | **2%**     | Applied to notional (price × size) |
| Maker fee        | **0%**     | Post-Only orders get 0% fee        |
| Neg-risk fee     | **0%**     | No fee for neg-risk merge          |
| Gas              | ~$0        | Gasless relayer (meta-tx)          |

**Effective cost per leg (taker)**:

```
fee_up   = ask_up  × size × 0.02
fee_down = ask_down × size × 0.02

total_fee_rate = (ask_up + ask_down) × 0.02
```

**Break-even condition** (minimum profitable spread):

```
ask_up + ask_down < 1.00 / (1 + 0.02 + 0.02)
ask_up + ask_down < 1.00 / 1.04
ask_up + ask_down < 0.9615
```

In practice, use a safety buffer:

```
MIN_ENTRY_SUM = 0.950   # leaves ~1% net margin after 2% taker on each side
```

### 1.4 Per-Trade Example

```
ask_up   = 0.21   (21¢)
ask_down = 0.72   (72¢)
sum      = 0.93   ← below 0.950 threshold ✓

size     = 20 shares each

gross_profit = (1.00 - 0.93) × 20 = $1.40
fees         = (0.21 × 20 × 0.02) + (0.72 × 20 × 0.02) = $0.084 + $0.288 = $0.372
net_profit   = $1.40 - $0.372 = $1.028  ← ~3.5% on $29.40 deployed
```

### 1.5 Equal-Shares vs Equal-Dollar Sizing

guh123 uses **equal shares** (same number of shares both sides), not equal dollars.

```
# Equal shares — guaranteed symmetric payoff
size = min(available_up, available_down, MAX_SIZE)
cost = ask_up × size + ask_down × size

# Locked profit (regardless of outcome)
locked_pnl = (1.00 - ask_up - ask_down - fees_rate) × size
```

This ensures exactly 1 side always pays — and the payout on that side fully covers costs + profit.

### 1.6 Time Dynamics

Markets close at expiry. Spread **widens** as expiry approaches:

```
spread(T_rem) ≈ 0.700 - 0.122 × T_rem_minutes
```

- At T=5min: spread ≈ 9.6% (thin)
- At T=1min: spread ≈ 54% (huge)

**Implication**: Opportunities appear throughout the market's life, but become more abundant near expiry. The bot must act on **any** window where sum < threshold.

---

## 2. System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        POLYMARKET ARB BOT                       │
│                                                                  │
│  ┌──────────────┐    ┌────────────────┐    ┌─────────────────┐  │
│  │  Market       │    │   Ingestion    │    │   Opportunity   │  │
│  │  Registry     │───▶│   Layer        │───▶│   Scanner       │  │
│  │  (Gamma API)  │    │   (WS + REST)  │    │   (Math Core)   │  │
│  └──────────────┘    └────────────────┘    └────────┬────────┘  │
│                                                       │          │
│                              ┌────────────────────────▼──────┐  │
│                              │      Execution Engine         │  │
│                              │   (py-clob-client, async)     │  │
│                              └────────────────────────┬──────┘  │
│                                                        │         │
│  ┌──────────────────────┐    ┌───────────────────────▼──────┐  │
│  │  Position Tracker    │◀───│      CLOB API / Relayer       │  │
│  │  (in-memory + disk)  │    │  (clob.polymarket.com)        │  │
│  └──────────────────────┘    └──────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                 Logger / Telemetry                        │   │
│  │       (JSONL on disk, Telegram alerts, metrics)          │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Breakdown

### 3.1 Market Registry

**Purpose**: Discover and track all active 5m/15m BTC/ETH markets.

**Source**: Gamma API — `https://gamma-api.polymarket.com/markets`

**Poll interval**: Every 30 seconds (new markets spawn continuously).

**Filter criteria**:
```python
ACTIVE_SLUGS = re.compile(r'(btc|eth)-updown-(5m|15m)-\d+')
conditions:
  - active == True
  - NOT expired
  - end_date_iso > now + 10s  (don't enter markets about to close)
```

**Output**: Dict `{ condition_id → MarketMeta }` shared across components.

```python
@dataclass
class MarketMeta:
    condition_id: str          # CLOB market ID
    slug: str                  # e.g. "btc-updown-5m-1741840500"
    asset: str                 # "BTC" | "ETH"
    timeframe: str             # "5m" | "15m"
    token_up: str              # token_id for Up outcome
    token_down: str            # token_id for Down outcome
    expires_at: datetime       # resolution time
    is_active: bool
```

### 3.2 Ingestion Layer (WebSocket + REST)

**Purpose**: Maintain real-time orderbook state for all active markets.

#### 3.2.1 WebSocket (primary)

- **Endpoint**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Subscribe**: One subscription per `condition_id` (or batch subscribe)
- **Events received**:
  - `book` — full orderbook snapshot
  - `price_change` — incremental update

```python
# Subscribe message
{
  "type": "subscribe",
  "channel": "market",
  "market": "<condition_id>"
}

# Received event (book update)
{
  "event_type": "book",
  "market": "<condition_id>",
  "bids": [{"price": "0.72", "size": "150"}, ...],
  "asks": [{"price": "0.74", "size": "200"}, ...]
}
```

**Local state**: In-memory `OrderBook` per market, keyed by `token_id`.

```python
@dataclass
class OrderBook:
    token_id: str
    bids: SortedDict[float, float]  # price → size
    asks: SortedDict[float, float]
    updated_at: float               # Unix timestamp

    def best_ask(self) -> tuple[float, float]:
        """Returns (price, available_size) of best ask."""
        return min(self.asks.items())
```

#### 3.2.2 REST Fallback

- **Endpoint**: `GET https://clob.polymarket.com/book?token_id={token_id}`
- Used on: WebSocket disconnect, initial bootstrap, gap-fill.
- Poll interval: 500ms per market (only when WS is down).

#### 3.2.3 Reconnect Strategy

```
WS disconnect → wait 250ms → reconnect with exponential backoff (max 5s)
On reconnect → REST snapshot all active markets → resume WS
```

### 3.3 Opportunity Scanner

**Purpose**: Continuously evaluate orderbooks and emit trading signals.

**Trigger**: Every time an orderbook update arrives (event-driven, not polling).

**Algorithm**:

```python
def scan(market: MarketMeta, books: dict[str, OrderBook]) -> Signal | None:
    book_up   = books[market.token_up]
    book_down = books[market.token_down]

    ask_up,   size_up   = book_up.best_ask()
    ask_down, size_down = book_down.best_ask()

    combined = ask_up + ask_down

    if combined >= MIN_ENTRY_SUM:        # 0.950
        return None

    # How much can we trade?
    tradeable = min(size_up, size_down, MAX_SIZE)  # 24 shares max

    if tradeable < MIN_SIZE:             # 5 shares min
        return None

    # Time filter — don't enter with < 10s remaining
    t_rem = (market.expires_at - datetime.utcnow()).total_seconds()
    if t_rem < 10:
        return None

    # Expected profit
    fee_rate  = (ask_up + ask_down) * 0.02 * 2   # taker on both legs
    net_margin = 1.00 - combined - fee_rate
    if net_margin <= 0:
        return None

    return Signal(
        market=market,
        ask_up=ask_up,     size_up=tradeable,
        ask_down=ask_down, size_down=tradeable,
        combined=combined,
        net_margin=net_margin,
        t_remaining=t_rem,
    )
```

**Deduplication**: Track `last_signal_at[condition_id]`. Suppress repeat signals within 1 second for the same market.

### 3.4 Execution Engine

**Purpose**: Place orders for both legs of an opportunity, as close to simultaneously as possible.

#### 3.4.1 Order Type

Use **FAK (Fill and Kill) limit orders** at the ask price.

- FAK fills what's available at that price or better, cancels the rest.
- Guarantees no resting open orders (avoids stale exposure).
- Equivalent to aggressive limit order / market order with price protection.

```python
order_up = {
    "token_id":   market.token_up,
    "price":      ask_up,
    "size":       size,
    "side":       "BUY",
    "order_type": "FAK",
}
order_down = {
    "token_id":   market.token_down,
    "price":      ask_down,
    "size":       size,
    "side":       "BUY",
    "order_type": "FAK",
}
```

#### 3.4.2 Parallel Execution

Both legs placed concurrently using `asyncio.gather`:

```python
async def execute(signal: Signal) -> ExecutionResult:
    async with self.rate_limiter:   # max 10 req/s
        results = await asyncio.gather(
            self.clob.place_order(signal.order_up),
            self.clob.place_order(signal.order_down),
            return_exceptions=True
        )
    return ExecutionResult(up=results[0], down=results[1])
```

#### 3.4.3 Partial Fill Handling

If one leg fills and the other fails/partial:

```python
if filled_up > 0 and filled_down == 0:
    # Leg imbalance — we have naked Up position
    # Options:
    # 1. Retry Down leg immediately (best case)
    # 2. Log as "orphan position" — hold to expiry (50/50 outcome)
    # 3. Cancel Up leg if possible (only if limit order not yet filled)
    await handle_orphan(market, filled_up, side="UP")
```

Strategy for orphan: **retry once** within 500ms, then **hold to expiry** (since it's a binary outcome, it's not a loss per se — just not delta-neutral).

#### 3.4.4 Rate Limiting

```
Polymarket API limit: ~10 requests/second (conservative estimate)
Bot target:           13.5 trades/min = 0.225 trades/sec (far below limit)
Safety margin:        max 5 order pairs/sec burst
```

Implemented as token bucket with 10 tokens/sec refill rate.

#### 3.4.5 Capital Guard

```python
MAX_DEPLOYED_USDC  = 5_000    # max capital at risk at any time
MAX_PER_MARKET     = 200      # max per single market entry
MAX_ORDER_SIZE     = 24       # max shares per leg (matches guh123)
MIN_ORDER_SIZE     = 5        # minimum economically viable
DAILY_LOSS_LIMIT   = -500     # halt if daily PnL < this
```

### 3.5 Position Tracker

**Purpose**: Track all open positions and completed trades.

**State**: In-memory dict + JSONL persistence.

```python
@dataclass
class Position:
    condition_id: str
    market_slug:  str
    entry_time:   datetime
    expires_at:   datetime
    size:         float           # shares (equal on both sides)
    cost_up:      float           # USDC paid for Up leg
    cost_down:    float           # USDC paid for Down leg
    total_cost:   float           # cost_up + cost_down
    locked_pnl:   float           # expected net profit
    status:       str             # "open" | "resolved" | "orphan"
    fill_up:      float | None    # actual fill price
    fill_down:    float | None
```

**Resolution**: On market expiry, fetch result via Gamma API. Mark position as resolved. Calculate actual PnL.

**PnL Reconciliation**: Every 15 minutes, reconcile in-memory state against CLOB `/positions` endpoint.

### 3.6 Logger / Telemetry

**Purpose**: Full audit trail, real-time monitoring, alerts.

#### Log Streams (JSONL files)

| File                      | Content                          | Rotation   |
|---------------------------|----------------------------------|------------|
| `logs/trades.jsonl`       | Every order placed + fill result | Daily      |
| `logs/signals.jsonl`      | Every opportunity scanned        | Daily      |
| `logs/positions.jsonl`    | Position lifecycle events        | Daily      |
| `logs/errors.jsonl`       | Exceptions, partial fills        | Daily      |
| `logs/performance.jsonl`  | PnL snapshots every 1 min        | Daily      |

#### Metrics (in-memory, exported every 60s)

```python
metrics = {
    "uptime_sec":          int,
    "signals_per_min":     float,
    "trades_per_min":      float,
    "fill_rate":           float,    # filled / attempted
    "pnl_today":           float,
    "pnl_session":         float,
    "capital_deployed":    float,
    "open_positions":      int,
    "orphan_positions":    int,
    "ws_reconnects":       int,
    "api_errors":          int,
}
```

#### Telegram Alerts

Send via Bot API on critical events:

| Event                         | Severity |
|-------------------------------|----------|
| Daily loss limit hit          | CRITICAL |
| API auth failure              | CRITICAL |
| WS down > 10s                 | WARNING  |
| Orphan position > $100        | WARNING  |
| Hourly PnL report             | INFO     |
| Bot start / stop              | INFO     |

---

## 4. Data Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  [Every 30s]                                                     │
│  Gamma API ──────────────▶ Market Registry                       │
│                              (add/remove active markets)         │
│                                    │                             │
│                                    ▼                             │
│                          WS Subscription Manager                 │
│                          (subscribe to new condition_ids)        │
│                                    │                             │
│  [Real-time events]                ▼                             │
│  WS Feed ────────────────▶ OrderBook Cache                       │
│                          (in-memory, per token_id)               │
│                                    │                             │
│  [On every update]                 ▼                             │
│                          Opportunity Scanner                     │
│                          scan() → Signal | None                  │
│                                    │                             │
│                              [Signal emitted]                    │
│                                    ▼                             │
│                          Execution Engine                        │
│                          asyncio.gather(order_up, order_down)    │
│                                    │                             │
│                          [Fill response]                         │
│                                    ▼                             │
│                          Position Tracker                        │
│                          (record entry, monitor expiry)          │
│                                    │                             │
│                          [Every 1 min / on event]                │
│                                    ▼                             │
│                          Logger / Telemetry                      │
│                          (JSONL + Telegram + metrics)            │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Concurrency Model

```
Main Event Loop (asyncio)
├── Task: market_registry_poller()       — every 30s
├── Task: ws_manager()                   — persistent, reconnects
│     ├── on_message → orderbook.update()
│     └── on_message → scanner.on_update(market_id)
├── Task: scanner.process_queue()        — consumes update events
│     └── if signal: execution_queue.put(signal)
├── Task: executor.process_queue()       — consumes signals
│     └── asyncio.gather(place_up, place_down)
├── Task: position_tracker.monitor()     — checks expiries
└── Task: telemetry.report()             — every 60s
```

Single-process, fully async. No threads needed.

---

## 5. Fee Structure & Profitability Model

### 5.1 All-In Cost Model

```
Per-trade costs (taker mode):
  fee_up   = price_up   × size × 0.02
  fee_down = price_down × size × 0.02
  gas      = $0.00  (gasless relayer)

Total fee = (price_up + price_down) × size × 0.02 × 2
          ≈ combined_price × size × 0.04
```

At `combined = 0.93` and `size = 20`:
```
total_fee = 0.93 × 20 × 0.04 = $0.744
gross_pnl = (1.00 - 0.93) × 20 = $1.40
net_pnl   = $1.40 - $0.744 = $0.656
margin    = 0.656 / (0.93 × 20) = 3.5%
```

### 5.2 Minimum Viable Spread

```python
def is_profitable(ask_up: float, ask_down: float, size: float) -> bool:
    combined   = ask_up + ask_down
    gross_pnl  = (1.00 - combined) * size
    total_fees = combined * size * 0.04   # 2% each side taker
    return gross_pnl > total_fees

# Simplified:
# 1.00 - combined > combined * 0.04
# 1.00 > combined * 1.04
# combined < 1.00 / 1.04 = 0.9615

MIN_ENTRY_SUM = 0.955   # conservative (adds 0.5% safety buffer)
```

### 5.3 Maker Mode (Advanced Optimization)

If we can post **maker orders** (Post-Only) at prices slightly **worse than the best ask**, we pay 0% fee:

```
Post Up limit at: ask_up + 0.001   (one tick above best ask)
Post Down limit at: ask_down + 0.001
```

This brings MIN_ENTRY_SUM up to `0.990` (viable spread = 1%+).  
Risk: orders may not fill if market moves.  
guh123 appears to use taker (immediate fills) — prioritizes fill certainty.

### 5.4 Capital Efficiency

Capital is locked per position until market expiry (~5 min):

```
capital_per_trade  = (ask_up + ask_down) × size
                   ≈ 0.93 × 20 = $18.60

At 13.5 trades/min, each locked 5 min:
  concurrent_positions = 13.5 × 5 = 67.5
  capital_locked       = 67.5 × $18.60 ≈ $1,255

Required working capital: ~$2,000 (with 60% buffer)
```

---

## 6. Tech Stack

### Primary: Python + asyncio

| Component            | Library / Tool             | Reason                                      |
|----------------------|----------------------------|---------------------------------------------|
| CLOB trading         | `py-clob-client`           | Official SDK, handles auth, order signing   |
| WebSocket            | `websockets` (asyncio)     | Lightweight, fully async                    |
| HTTP client          | `aiohttp`                  | Async REST calls                            |
| Data structures      | `sortedcontainers`         | SortedDict for orderbook (O(log n) updates) |
| Config               | `pydantic-settings`        | .env + type safety                          |
| Logging              | stdlib `logging` + JSONL   | Structured, zero-dep                        |
| Telegram alerts      | `python-telegram-bot`      | Async, already in Ouroboros stack           |
| Testing              | `pytest` + `pytest-asyncio`| Unit test order logic, mock WS              |

### Why Python (not TypeScript)

1. `py-clob-client` is the **official** SDK — avoids re-implementing ECDSA signing and order encoding.
2. All research / calibration code (A-S model, VPIN) already in Python.
3. `asyncio` handles 10+ concurrent WebSocket subscriptions easily.
4. TypeScript would require reverse-engineering the signing protocol.

### File Structure

```
polymarket_arb/
├── main.py                 # Entry point, wires components
├── config.py               # Pydantic settings (from .env)
├── market_registry.py      # Gamma API poller, MarketMeta
├── orderbook.py            # OrderBook dataclass, WS manager
├── scanner.py              # Opportunity Scanner, Signal
├── executor.py             # Execution Engine, FAK orders
├── position_tracker.py     # Position lifecycle, PnL
├── logger.py               # JSONL + Telegram telemetry
├── clob_client.py          # Thin wrapper around py-clob-client
├── models.py               # Shared dataclasses
└── tests/
    ├── test_scanner.py
    ├── test_executor.py
    └── fixtures/           # Mock orderbook snapshots
```

---

## 7. Configuration Reference

```env
# .env
# ─── Wallet / Auth ───────────────────────────────────────
PRIVATE_KEY=0x...            # EVM private key (EOA)
POLYMARKET_API_KEY=...       # Derived L2 key (from create_or_derive_api_creds)
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
PROXY_WALLET=0x...           # Your Polymarket proxy address

# ─── Strategy Parameters ─────────────────────────────────
MIN_ENTRY_SUM=0.955          # Max combined ask to enter
MAX_ORDER_SIZE=24            # Max shares per leg
MIN_ORDER_SIZE=5             # Min shares per leg
MAX_DEPLOYED_USDC=5000       # Hard cap on deployed capital
MAX_PER_MARKET_USDC=200      # Cap per market
DAILY_LOSS_LIMIT=-500        # Halt bot if hit

# ─── Target Markets ──────────────────────────────────────
TARGET_ASSETS=BTC,ETH
TARGET_TIMEFRAMES=5m,15m

# ─── Connectivity ────────────────────────────────────────
CLOB_HOST=https://clob.polymarket.com
GAMMA_HOST=https://gamma-api.polymarket.com
WS_HOST=wss://ws-subscriptions-clob.polymarket.com/ws/market
CHAIN_ID=137                 # Polygon mainnet

# ─── Telemetry ───────────────────────────────────────────
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
LOG_DIR=./logs
```

---

## 8. Deployment

### Environment

- **Platform**: Any VPS with Python 3.11+ (preferably EU/US data center for latency).
- **Geo note**: Polymarket API is accessible globally; no geo-restriction on CLOB.
- **RAM**: 256MB+ sufficient (orderbook is tiny at this scale).
- **CPU**: 1 core, not CPU-bound.

### Prerequisites

```bash
pip install py-clob-client websockets aiohttp sortedcontainers pydantic-settings python-telegram-bot
```

### Key Setup Step: API Credential Derivation

```python
# Run once to derive L2 API keys from private key
from py_clob_client.client import ClobClient

client = ClobClient(host=CLOB_HOST, chain_id=137, key=PRIVATE_KEY)
creds = client.create_or_derive_api_creds()
print(creds)  # Save to .env
```

### Launch

```bash
python polymarket_arb/main.py
```

### Health Check

Bot exposes simple metrics via Telegram `/status` command:

```
📊 Bot Status
Uptime: 2h 34m
Trades today: 1,847
PnL today: +$62.40
Open positions: 23
Capital deployed: $1,140 / $5,000
WS status: connected
Last trade: 4s ago
```

---

## Appendix A: Glossary

| Term           | Definition                                                     |
|----------------|----------------------------------------------------------------|
| Neg-Risk       | Negative risk: guaranteed profit from buying both outcomes     |
| FAK            | Fill and Kill: limit order that fills available, cancels rest  |
| CLOB           | Central Limit Order Book                                       |
| CTF            | Conditional Token Framework (Gnosis, powers Polymarket)        |
| Condition ID   | Unique market identifier on CLOB                               |
| Token ID       | Unique outcome token identifier (Up or Down side)              |
| Proxy Wallet   | Polymarket's internal smart wallet for each user               |
| Combined Ask   | `ask_up + ask_down` — the key metric watched by the scanner    |
| VPIN           | Volume-Synchronized Probability of Informed Trading            |

## Appendix B: Risk Register

| Risk                        | Likelihood | Impact   | Mitigation                          |
|-----------------------------|------------|----------|-------------------------------------|
| API downtime                | Medium     | High     | Retry logic, REST fallback          |
| Partial fill (leg imbalance)| Medium     | Medium   | Orphan handler, retry once          |
| Fee increase                | Low        | Medium   | Config-driven MIN_ENTRY_SUM         |
| Capital lock > 5min         | Low        | Low      | Small size per trade, spread out    |
| Competitor latency          | High       | Low      | Already low target (13.5/min)       |
| Market resolution delay     | Low        | Low      | Hold to expiry always               |
| Private key leak            | Very Low   | Critical | .env never committed, use secret mgr|
