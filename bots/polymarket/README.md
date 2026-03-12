# Polymarket Arb Bot

Automated arbitrage bot for Polymarket BTC/ETH "Up or Down" markets (5min/15min intraday + daily).

## Strategy: Negative Risk Arbitrage

On binary markets, YES + NO = $1 at resolution (one side always wins).

If `YES_ask + NO_ask < 1.0 - fees`, buying both sides is **risk-free profit**.

```
YES = 0.46, NO = 0.46 → total = 0.92
Payout = $1.00/share
Gross margin = 8%
After 2% taker fee on each side: net ~4%
```

**Breakeven**: `YES_ask + NO_ask < 0.97` (3% gross margin)

## Quick Start

### Paper trading (no wallet needed)

```bash
cd /path/to/ouroboros_repo

# Single scan — test that everything works
python -m bots.polymarket.bot --once

# Continuous paper trading (scan every 30s)
python -m bots.polymarket.bot --interval 30

# Verbose output
python -m bots.polymarket.bot --once -v
```

### Live trading

**Step 1**: Get credentials from your Polygon wallet:
```bash
python -c "
from bots.polymarket.auth import derive_and_print_creds
derive_and_print_creds('0xYOUR_PRIVATE_KEY')
"
```

**Step 2**: Set environment variables:
```
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

**Step 3**: Run in live mode:
```bash
python -m bots.polymarket.bot --mode live --max-position 5 --max-exposure 50
```

## Parameters

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `paper` | `paper` (simulate) or `live` (real trades) |
| `--interval` | `30` | Scan interval in seconds |
| `--max-position` | `10` | Max USDC per single trade |
| `--max-exposure` | `50` | Max total USDC deployed simultaneously |
| `--profit-threshold` | `0.97` | Enter trade if YES+NO < this value |
| `--min-depth` | `2` | Min USDC depth required on each side |
| `--once` | off | Run one scan and exit |
| `-v` | off | Verbose/debug logging |

## Architecture

```
bot.py           — Main loop, CLI entry point
scanner.py       — Market discovery (Gamma API) + neg-risk scan (CLOB API)
paper_trader.py  — Paper trading engine with P&L tracking
auth.py          — Wallet authentication for live trading
models.py        — Data classes: Market, Orderbook, ArbOpportunity, PaperTrade
README.md        — This file
```

## Market Discovery

The bot targets two types of markets:

| Type | Schedule | Example |
|------|----------|---------|
| **5-minute** | Weekdays, every 5min, ~9AM-5PM ET | "Bitcoin Up or Down - March 13, 3:00PM-3:05PM ET" |
| **15-minute** | Weekdays, every 15min, ~9AM-5PM ET | "Bitcoin Up or Down - March 13, 3:00PM-3:15PM ET" |
| **Daily** | Every day | "Bitcoin Up or Down on March 13" |

Discovery uses:
1. **Gamma /events API** → intraday 5min/15min markets
2. **Gamma /markets API** → daily/hourly markets as fallback

## P&L Accounting

```
Shares = position_usdc / (YES_ask + NO_ask)
YES_cost = YES_ask × shares × 1.02  (2% taker fee)
NO_cost  = NO_ask  × shares × 1.02  (2% taker fee)
Total_cost = YES_cost + NO_cost
Payout = shares × $1.00             (one side always wins)
Profit = Payout - Total_cost
```

## Logs (Google Drive)

| File | Contents |
|------|----------|
| `Ouroboros/logs/paper_trades.jsonl` | All paper trades (enter + resolve events) |
| `Ouroboros/logs/opportunities.jsonl` | All detected arb opportunities |

## Wallet Requirements (Live Mode)

- **USDC on Polygon**: The collateral token (deposit at polymarket.com)
- **MATIC on Polygon**: Gas for transactions (~$0.001 per trade)
- **Minimum**: ~$10 USDC + $1 MATIC to start

Your wallet address on Polygon is derived from your private key:
```python
from eth_account import Account
acct = Account.from_key("0xYOUR_KEY")
print(acct.address)  # Send USDC/MATIC to this address on Polygon
```
