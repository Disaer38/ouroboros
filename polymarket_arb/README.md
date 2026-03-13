# Polymarket Negative Risk Arbitrage Bot

Strategy: buy both UP and DOWN when `ask_up + ask_down < 0.955` (after 2% fee × 2 legs + 0.5% buffer).

## Phase 1 (current): Read-Only Detector
Monitors BTC/ETH 5m/15m markets. Prints arbitrage opportunities to stdout.

```bash
cd polymarket_arb
pip install httpx
python main.py
```

## Architecture
See `../docs/polymarket_arb/ARCHITECTURE.md`.

## Phases
- [x] Phase 1: Opportunity detection (read-only, REST polling)
- [ ] Phase 2: WebSocket orderbook + paper trading
- [ ] Phase 3: Real execution via `py-clob-client`
