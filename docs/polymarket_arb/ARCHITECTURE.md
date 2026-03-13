# Polymarket Negative Risk Arbitrage Bot — System Architecture

> Version: 1.0.0  
> Strategy: Negative Risk Arbitrage (primary) + Market Making (secondary)  
> Target: `btc-updown-5m-*`, `eth-updown-5m-*`  
> Language: Python 3.11+ / asyncio  
> SDK: `py-clob-client`

---

## Executive Summary

Наблюдения за трейдером guh123 (3,500+ трейдов за 4.33 часа, $164K+ прибыли за 6 недель) подтверждают:

- **Стратегия**: покупка обеих сторон (`UP` + `DOWN`) когда `best_ask(UP) + best_ask(DOWN) < 1.00 - fees`
- **Темп**: ~13.5 трейдов/мин
- **Фокус**: 100% BTC/ETH, 85.5% на 5-минутных рынках
- **Паттерн позиции**: равное количество шер (shares) на обе стороны, не равные доллары

Математика: если `P_up + P_down = 0.96`, то покупая обе стороны за **$0.96**, получаем гарантированный payoff **$1.00** → прибыль **4.17%** независимо от исхода.

---

## 1. Структура файлов

```
polymarket_arb/
├── main.py                    # Entry point — запуск MainLoop
├── config.py                  # Конфигурация (env vars, константы)
├── services/
│   ├── __init__.py
│   ├── market_data.py         # MarketDataService — WS + orderbook
│   └── execution.py           # ExecutionService — py-clob-client wrapper
├── strategy/
│   ├── __init__.py
│   ├── base.py                # StrategyEngine — abstract base
│   ├── neg_risk.py            # NegativeRiskStrategy — основная
│   └── market_making.py       # MarketMakingStrategy — вторичная (A-S)
├── risk/
│   ├── __init__.py
│   └── manager.py             # RiskManager — pre-trade checks
├── core/
│   ├── __init__.py
│   ├── loop.py                # MainLoop — asyncio orchestration
│   ├── models.py              # Dataclasses: OrderBook, Opportunity, Order
│   └── events.py              # EventBus — internal pub/sub
└── utils/
    ├── __init__.py
    ├── logger.py              # Structured logging (JSONL)
    └── metrics.py             # PnL tracker, latency histogram
```

---

## 2. Компоненты

### 2.1 `MarketDataService`

**Файл**: `services/market_data.py`  
**Ответственность**: единственный источник правды об orderbook.

```python
class MarketDataService:
    """
    Подключается к Polymarket RTDS (Real-Time Data Service) через WebSocket.
    Поддерживает in-memory снапшот orderbook для каждого активного рынка.
    Публикует события OrderBookUpdate в EventBus.
    """
    
    # State
    _books: dict[str, OrderBook]           # token_id -> OrderBook
    _ws_connections: dict[str, WebSocket]  # market_slug -> connection
    _active_markets: list[MarketMeta]      # текущие открытые 5m/15m рынки
    
    # Публичный API
    async def start(self) -> None: ...
    async def get_book(self, token_id: str) -> OrderBook: ...
    async def get_best_ask(self, token_id: str) -> Decimal: ...
    async def get_active_markets(self) -> list[MarketMeta]: ...
    async def refresh_active_markets(self) -> None: ...  # каждые ~30 сек
```

**Источники данных**:
- **WebSocket**: `wss://clob.polymarket.com/ws` — orderbook deltas в реальном времени
- **REST (bootstrap)**: `GET /book?token_id=...` — начальный снапшот при подключении
- **Gamma API**: `GET https://gamma-api.polymarket.com/markets?...` — список активных рынков

**Алгоритм поддержки orderbook**:
```
1. При старте: GET /book -> инициализировать OrderBook
2. Подписаться на WS: {"type": "subscribe", "markets": [token_id, ...]}
3. При получении delta:
   - "price_change": обновить уровень (bid/ask)
   - "book": полный снапшот (ресинхронизация)
4. Публиковать OrderBookUpdate в EventBus
```

---

### 2.2 `ExecutionService`

**Файл**: `services/execution.py`  
**Ответственность**: исполнение ордеров через `py-clob-client`, управление сессией.

```python
class ExecutionService:
    """
    Тонкая обёртка над py-clob-client с:
    - retry логикой (exponential backoff)
    - rate limiting (≤ 10 req/sec по умолчанию)
    - batch orders (до 15 ордеров за раз)
    - логированием каждого ордера с latency
    """
    
    _client: ClobClient          # py-clob-client instance
    _rate_limiter: RateLimiter   # token bucket
    
    # Публичный API
    async def place_order(self, order: OrderRequest) -> OrderResult: ...
    async def place_batch(self, orders: list[OrderRequest]) -> list[OrderResult]: ...
    async def get_balance(self) -> Decimal: ...  # USDC баланс
    async def get_positions(self) -> list[Position]: ...
```

**Типы ордеров для стратегии**:
- `FAK` (Fill-and-Kill) — предпочтительный тип для арбитража: исполнить немедленно или отменить
- `FOK` (Fill-or-Kill) — для гарантии атомарности (обе стороны или ни одной)
- `GTC` (Good-Till-Cancel) — для market making (постановка в стакан)

**Batch execution для neg-risk**:
```python
# Отправляем UP и DOWN в одном batch-запросе
orders = [
    OrderRequest(token_id=up_token,   side="BUY", price=up_ask,   size=shares),
    OrderRequest(token_id=down_token, side="BUY", price=down_ask, size=shares),
]
results = await execution.place_batch(orders)
```

---

### 2.3 `StrategyEngine`

**Файл**: `strategy/base.py` + `strategy/neg_risk.py`  
**Ответственность**: принятие торговых решений на основе данных рынка.

```python
class StrategyEngine(ABC):
    """Abstract base — все стратегии реализуют этот интерфейс."""
    
    @abstractmethod
    async def on_book_update(self, event: OrderBookUpdateEvent) -> list[TradeSignal]:
        """Получает обновление orderbook, возвращает список сигналов (может быть пустым)."""
        ...
    
    @abstractmethod
    async def on_market_open(self, market: MarketMeta) -> None:
        """Уведомление о новом рынке (для подписки)."""
        ...
    
    @abstractmethod
    async def on_market_close(self, market: MarketMeta) -> None:
        """Уведомление о закрытии рынка (для очистки состояния)."""
        ...
```

#### `NegativeRiskStrategy`

```python
class NegativeRiskStrategy(StrategyEngine):
    """
    Основная стратегия: покупать обе стороны когда их сумма < 1 - fees.
    
    Алгоритм:
    1. При каждом OrderBookUpdate: достать best_ask(UP) и best_ask(DOWN)
    2. Проверить: sum = ask_up + ask_down
    3. Если sum < THRESHOLD (с учётом fee): создать сигнал
    4. Рассчитать размер (равные shares, ограниченные ликвидностью и риском)
    5. Передать сигнал в RiskManager -> ExecutionService
    """
    
    # Параметры
    FEE_RATE: Decimal = Decimal("0.02")       # 2% fee (уточнить из API)
    MIN_EDGE: Decimal = Decimal("0.005")      # минимальная прибыль после fees
    MAX_POSITION_PER_MARKET: Decimal = ...    # из RiskManager
    
    def _calc_threshold(self) -> Decimal:
        # Покупаем только если: 1 - ask_up - ask_down > MIN_EDGE + FEE_RATE * 2
        return Decimal("1.0") - self.MIN_EDGE - self.FEE_RATE * 2
    
    def _calc_shares(self, up_ask: Decimal, down_ask: Decimal,
                     up_avail: Decimal, down_avail: Decimal) -> Decimal:
        """
        Равные shares (не равные доллары) — паттерн guh123.
        Ограничены: min(up_avail, down_avail, max_by_risk / total_cost)
        """
        max_by_risk = self.risk_mgr.max_order_size()
        total_cost_per_share = up_ask + down_ask
        max_shares = max_by_risk / total_cost_per_share
        return min(up_avail, down_avail, max_shares)
```

#### `MarketMakingStrategy` (вторичная)

```python
class MarketMakingStrategy(StrategyEngine):
    """
    Avellaneda-Stoikov для бинарных рынков (параметры из backtesta).
    
    Калиброванные параметры (btc-updown-5m):
      σ = 0.007636  (vol per √sec)
      κ = 20.27     (order decay)
      A = 39.33     (base order rate)
      γ = 5-35      (risk aversion — implied из данных)
      T = 300 sec   (market duration)
    
    Алгоритм:
    1. Вычислить reservation price (r) с учётом inventory
    2. Вычислить optimal spread
    3. Выставить bid/ask вокруг r
    4. Обновлять котировки при каждом тике или изменении inventory
    5. VPIN > 0.35 -> прекратить котирование (toxic flow protection)
    """
    
    CALIBRATED_PARAMS = {
        'sigma': 0.007636, 'kappa': 20.27, 'A': 39.33,
        'gamma': 1.0,      'T': 300,
        'vpin_high': 0.25, 'vpin_crit': 0.35,
    }
```

---

### 2.4 `RiskManager`

**Файл**: `risk/manager.py`  
**Ответственность**: pre-trade проверки, глобальные и per-market лимиты.

```python
class RiskManager:
    """
    Единственный guard между сигналами стратегии и execution.
    Все лимиты задаются через config.py / env vars.
    """
    
    # Глобальные лимиты
    MAX_GLOBAL_EXPOSURE: Decimal  # макс. суммарная открытая позиция ($)
    MAX_DAILY_LOSS: Decimal       # стоп по дневному убытку ($)
    MIN_USDC_RESERVE: Decimal     # минимальный остаток USDC (не торговать ниже)
    
    # Per-market лимиты
    MAX_POSITION_PER_MARKET: Decimal  # макс. вложений в один рынок ($)
    MAX_ORDERS_PER_MINUTE: int        # rate limit на ордера
    
    # State (обновляется из ExecutionService)
    _current_exposure: Decimal
    _daily_pnl: Decimal
    _positions: dict[str, Decimal]  # market_id -> exposure
    
    def check(self, signal: TradeSignal) -> RiskDecision:
        """
        Возвращает APPROVE / REDUCE(new_size) / REJECT(reason).
        Вызывается синхронно перед каждым execution.
        """
        checks = [
            self._check_balance(signal),
            self._check_daily_loss(),
            self._check_global_exposure(signal),
            self._check_per_market(signal),
            self._check_rate_limit(),
        ]
        return self._aggregate(checks)
    
    def record_fill(self, fill: FillEvent) -> None:
        """Обновляет состояние после исполнения ордера."""
        ...
    
    def record_settlement(self, event: SettlementEvent) -> None:
        """Обновляет PnL после закрытия рынка."""
        ...
```

**Пороги по умолчанию** (конфигурируемые):
| Параметр | Default |
|----------|---------|
| `MAX_POSITION_PER_MARKET` | $200 |
| `MAX_GLOBAL_EXPOSURE` | $2,000 |
| `MAX_DAILY_LOSS` | $100 |
| `MIN_USDC_RESERVE` | $50 |
| `MAX_ORDERS_PER_MINUTE` | 30 |
| `MIN_EDGE` | 0.5% (после fees) |

---

### 2.5 `MainLoop`

**Файл**: `core/loop.py`  
**Ответственность**: запуск всех компонентов, event routing, graceful shutdown.

```python
class MainLoop:
    """
    asyncio-оркестратор. Запускает все компоненты как coroutines.
    Связывает их через EventBus (pub/sub).
    """
    
    def __init__(self, config: Config):
        self.event_bus = EventBus()
        self.market_data = MarketDataService(config, self.event_bus)
        self.execution   = ExecutionService(config)
        self.risk_mgr    = RiskManager(config)
        self.strategy    = NegativeRiskStrategy(self.risk_mgr)
        self.logger      = StructuredLogger(config)
    
    async def run(self) -> None:
        """
        Запускает все корутины параллельно.
        При получении SIGINT — graceful shutdown.
        """
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.market_data.start())
            tg.create_task(self._market_refresh_loop())
            tg.create_task(self._event_processing_loop())
            tg.create_task(self._metrics_reporter())
    
    async def _event_processing_loop(self) -> None:
        """Основной цикл: orderbook update -> signal -> risk check -> execute."""
        async for event in self.event_bus.subscribe(OrderBookUpdateEvent):
            signals = await self.strategy.on_book_update(event)
            for signal in signals:
                decision = self.risk_mgr.check(signal)
                if decision.approved:
                    result = await self.execution.place_batch(decision.orders)
                    self.risk_mgr.record_fill(result)
                    self.logger.log_trade(signal, decision, result)
```

---

## 3. Data Flow

```
                         POLYMARKET
                    ┌─────────────────────────────────┐
                    │  RTDS WebSocket (orderbook WS)  │
                    │  CLOB REST API  (execution)     │
                    │  Gamma API      (market list)   │
                    └──────────┬──────────────────────┘
                               │ WS deltas / REST snapshots
                               ▼
                    ┌──────────────────────┐
                    │  MarketDataService   │ ─── in-memory OrderBook state
                    │  (WS handler +       │     per token_id
                    │   book reconciler)   │
                    └──────────┬───────────┘
                               │ OrderBookUpdateEvent (via EventBus)
                               ▼
                    ┌──────────────────────┐
                    │   StrategyEngine     │ ─── NegativeRiskStrategy
                    │   on_book_update()   │     (or MarketMakingStrategy)
                    └──────────┬───────────┘
                               │ TradeSignal(token_up, token_down, shares, cost)
                               ▼
                    ┌──────────────────────┐
                    │    RiskManager       │ ─── pre-trade checks
                    │    check(signal)     │     APPROVE / REDUCE / REJECT
                    └──────────┬───────────┘
                               │ RiskDecision(approved=True, orders=[...])
                               ▼
                    ┌──────────────────────┐
                    │  ExecutionService    │ ─── place_batch([UP_order, DOWN_order])
                    │  (py-clob-client)    │     FAK / FOK orders
                    └──────────┬───────────┘
                               │ FillEvent(price_paid, shares, latency_ms)
                               ▼
                    ┌──────────────────────┐
                    │   RiskManager        │ ─── record_fill() → update exposure
                    │   + Logger           │     log trade to JSONL
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   Metrics / PnL      │  ─── per-market P&L
                    │   (settlement)       │      cumulative stats
                    └──────────────────────┘
```

### Критические пути по latency

| Шаг | Target latency |
|-----|---------------|
| WS delta → OrderBook update | < 1 ms |
| OrderBook update → Signal | < 1 ms |
| Signal → Risk check | < 0.5 ms (sync) |
| Risk → Execution (HTTP) | < 50 ms |
| **Total: WS → Fill** | **< 100 ms** |

> guh123 держит темп 13.5 трейдов/мин = **1 трейд каждые 4.4 секунды**. Это комфортно при 100ms latency — значит, конкуренция за отдельную арб-возможность есть, но не hyper-competitive.

---

## 4. EventBus (внутренний pub/sub)

```python
# core/events.py

@dataclass
class OrderBookUpdateEvent:
    token_id: str
    market_id: str
    best_ask: Decimal
    best_bid: Decimal
    asks: list[tuple[Decimal, Decimal]]  # (price, size)
    bids: list[tuple[Decimal, Decimal]]
    timestamp: float

@dataclass  
class TradeSignal:
    strategy: str                  # "neg_risk" | "mm"
    up_token: str
    down_token: str
    market_slug: str
    shares: Decimal
    up_ask: Decimal
    down_ask: Decimal
    edge: Decimal                  # = 1 - up_ask - down_ask - fees
    timestamp: float

@dataclass
class FillEvent:
    signal: TradeSignal
    orders_placed: int
    orders_filled: int
    total_cost: Decimal
    latency_ms: float
    timestamp: float
```

---

## 5. Конфигурация (`config.py`)

```python
# Все параметры — через env vars с дефолтами

@dataclass
class Config:
    # Auth
    PRIVATE_KEY: str            = env("POLY_PRIVATE_KEY")
    API_KEY: str                = env("POLY_API_KEY")
    API_SECRET: str             = env("POLY_API_SECRET")
    API_PASSPHRASE: str         = env("POLY_PASSPHRASE")
    
    # Network
    HOST: str                   = "https://clob.polymarket.com"
    CHAIN_ID: int               = 137  # Polygon
    
    # Strategy
    TARGET_SLUGS: list[str]     = ["btc-updown-5m", "eth-updown-5m"]
    MIN_EDGE_PCT: float         = 0.005   # 0.5% minimum profit
    FEE_RATE: float             = 0.02    # 2% taker fee (verify via API)
    
    # Risk
    MAX_POSITION_PER_MARKET: float  = 200.0
    MAX_GLOBAL_EXPOSURE: float      = 2000.0
    MAX_DAILY_LOSS: float           = 100.0
    MIN_USDC_RESERVE: float         = 50.0
    MAX_ORDERS_PER_MINUTE: int      = 30
    
    # Execution
    ORDER_TYPE: str             = "FAK"   # Fill-and-Kill
    MAX_RETRIES: int            = 3
    RETRY_DELAY_MS: int         = 200
    
    # Monitoring
    LOG_DIR: str                = "./logs"
    METRICS_INTERVAL_SEC: int   = 60
```

---

## 6. Ключевые решения архитектуры

### 6.1 Равные shares vs равные доллары

guh123 покупает **равное количество шер** (shares) на обе стороны — не равные доллары:

```
UP:   1,907 shares × $0.207 = $394
DOWN: 1,940 shares × $0.773 = $1,499   ← разница 1.7% в shares

Payoff при любом исходе: ~$1,940 (по большей позиции)
Стоимость: $1,893
Edge: $47 (2.5%)
```

**Почему**: при разрешении рынка платят по **количеству шер** победившей стороны. Если купить равное количество шер, гарантированный выигрыш = `shares × $1.00`, независимо от того, какая сторона победила.

### 6.2 Batch orders — необходимость

Покупка UP и DOWN — **два отдельных ордера**. Polymarket не поддерживает атомарные спредовые ордера. Поэтому:
- Используем `place_batch` (до 15 ордеров за раз)
- **Риск leg-risk**: первый ордер может исполниться, второй — нет (если цена ушла)
- Митигация: `FOK` (Fill-or-Kill) на обе ноги, или мониторинг + быстрое закрытие

### 6.3 Capital lock-up

Капитал заблокирован до разрешения рынка (~5 минут). При $2,000 капитала и среднем размере позиции $200 — одновременно активно ~10 рынков. Это соответствует реальности: guh123 торгует на нескольких 5m рынках параллельно.

### 6.4 Fee calculation

```python
def calc_true_edge(ask_up: Decimal, ask_down: Decimal, fee_rate: Decimal) -> Decimal:
    """
    True edge после комиссий.
    Fee берётся с каждой стороны при покупке.
    """
    cost = ask_up + ask_down
    total_fee = (ask_up + ask_down) * fee_rate  # уточнить: fee от notional или от side
    payoff = Decimal("1.0")
    return payoff - cost - total_fee
```

> ⚠️ **Важно**: точная формула fee требует верификации через Polymarket docs. Fee может браться только при торговле (0% при резолюции), что значительно меняет расчёт.

---

## 7. Фазы разработки

### Phase 1: Core (MVP) — ~1 неделя

- [ ] `models.py` — dataclasses
- [ ] `market_data.py` — REST bootstrap + WS connection
- [ ] `execution.py` — `py-clob-client` wrapper, dry-run mode
- [ ] `neg_risk.py` — основная логика
- [ ] `risk/manager.py` — базовые лимиты
- [ ] `main.py` — запуск, graceful shutdown
- [ ] Логирование всех ордеров в JSONL

### Phase 2: Reliability — ~3 дня

- [ ] WS reconnect с экспоненциальным backoff
- [ ] Orderbook resync при разрыве соединения
- [ ] Leg-risk mitigation (мониторинг частично исполненных ордеров)
- [ ] Dry-run / paper trading режим
- [ ] Health check endpoint

### Phase 3: Optimization — ~1 неделя

- [ ] Calibrate `MIN_EDGE` из live данных
- [ ] `MarketMakingStrategy` (A-S model)
- [ ] VPIN calculation для toxic flow detection
- [ ] Position sizing optimization
- [ ] Backtest harness

---

## 8. Зависимости

```txt
# requirements.txt
py-clob-client>=0.15.0
websockets>=12.0
aiohttp>=3.9.0
python-dotenv>=1.0.0
eth-account>=0.11.0   # для подписи (используется py-clob-client)
```

---

## 9. Запуск

```bash
# 1. Установка
pip install -r requirements.txt

# 2. Конфиг
cp .env.example .env
# Заполнить POLY_PRIVATE_KEY, POLY_API_KEY и т.д.

# 3. Dry-run (без реальных ордеров)
DRY_RUN=true python main.py

# 4. Live
python main.py
```

---

## 10. Метрики и мониторинг

```python
# Минимальный набор метрик для оценки работы бота

{
  "timestamp": "2026-03-13T22:00:00Z",
  "session_duration_min": 60,
  "trades_total": 810,           # ~13.5/min
  "trades_per_min": 13.5,
  "fills_rate": 0.95,            # 95% исполнения
  "avg_edge_pct": 0.031,         # средняя прибыль 3.1%
  "avg_latency_ms": 45,
  "pnl_gross": 250.50,
  "fees_paid": 38.20,
  "pnl_net": 212.30,
  "active_markets": 8,
  "leg_risk_events": 2,          # один ордер исполнился, второй нет
  "risk_rejections": 5
}
```

---

*Документ отражает текущее понимание системы на основе анализа guh123 и публичной документации Polymarket. Ряд параметров (fee_rate, WS endpoint, batch limits) требует верификации в live среде.*
