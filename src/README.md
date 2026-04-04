# Institutional Zone-Reactive Trading Bot

A fully automated, high-precision algorithmic trading system using **Price Action, Volume, and Time** only.  
No black-box indicators. No RSI, no MACD.

---

## Architecture Overview (Domain-Driven Design)

```
trading_bot/
│
├── domain/                         ← Pure business logic. Zero external deps.
│   ├── entities.py                 # Candle, Zone, Trade, NewsEvent, TradeSignal …
│   ├── value_objects.py            # PipCalculator, BrokerCost, RiskParameters
│   └── repositories.py            # Abstract interfaces (ports)
│
├── infrastructure/                 ← External I/O adapters (adapters)
│   ├── mt5_bridge/
│   │   ├── mt5_gateway.py         # ← Windows ONLY. Pure MT5 API calls.
│   │   ├── zmq_server.py          # ← Windows ONLY. ZMQ REP server wrapping gateway.
│   │   └── zmq_client.py          # ← WSL side. ZMQ REQ client.
│   ├── market_data_repo.py        # Implements IMarketDataRepository via ZMQ
│   ├── trade_repo.py              # Implements ITradeRepository via ZMQ
│   ├── zone_repo.py               # In-memory + JSON-persisted zone store
│   ├── news_client.py             # ForexFactory + MyFxBook HTTP client
│   └── cache_store.py             # Local JSON file cache (30-min TTL)
│
├── application/                    ← Use cases / orchestration
│   ├── analysis.py                # Phase 1: OB, FVG, ATR, Liquidity, LTF signals
│   ├── news_manager.py            # Phase 2: News guardrails + broker cost check
│   ├── execution_service.py       # Phase 3: Zone monitor + trade placement
│   └── backtester.py              # Phase 4: Walk-forward simulation
│
├── bridge/
│   └── windows_mt5_server.py      # Deployment notes for Windows side
│
├── main.py                         ← Dependency wiring + main loop
├── config.json                     ← All runtime parameters
└── requirements.txt
```

---

## How the WSL ↔ Windows Bridge Works

```
┌─────────────────────────────┐       ZeroMQ REQ/REP         ┌──────────────────────────────┐
│          WSL (Linux)        │ ──────────────────────────── │       Windows Host            │
│                             │       TCP :5555               │                               │
│  main.py                    │                               │  zmq_server.py                │
│  └─ ZMQClient               │  { method, params }  ──────► │  └─ RequestDispatcher         │
│     └─ sends JSON requests  │  { ok, result }      ◄─────  │     └─ MT5Gateway             │
│                             │                               │        └─ MetaTrader5 API     │
└─────────────────────────────┘                               └──────────────────────────────┘
```

The WSL side never imports `MetaTrader5`. It communicates exclusively through the bridge.

---

## Setup Instructions

### Step 1 — Windows Side

1. Install Python 3.10+ on Windows (not in WSL).
2. Install dependencies:
   ```powershell
   pip install MetaTrader5 pyzmq
   ```
3. Copy these two files to a Windows folder:
   - `infrastructure/mt5_bridge/mt5_gateway.py`
   - `infrastructure/mt5_bridge/zmq_server.py`
4. Open MetaTrader 5. Allow Algo Trading.
5. Launch the server:
   ```powershell
   python zmq_server.py --login 57427774 --password g3YuQ%U9CU9#Pg$ --server HFMarketsKE-Demo2
   ```

### Step 2 — WSL Side

1. Install WSL dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Find your Windows IP from WSL:
   ```bash
   cat /etc/resolv.conf | grep nameserver
   # e.g.  nameserver 172.20.112.1
   ```
3. Edit `config.json`:
   ```json
   "bridge": { "zmq_host": "172.20.112.1", "zmq_port": 5555 }
   ```
4. Add your ForexNews API key:
   ```json
   "news": { "api_key": "sk-your-key-here" }
   ```
5. Run the bot:
   ```bash
   python main.py
   ```

---

## Trading Logic Summary

### Phase 1 — Zone Mapping (`analysis.py`)
Runs on every H1 bar close for all 5 symbols.

| Zone Type        | Logic |
|-----------------|-------|
| **Bullish OB**  | Last bearish candle before an impulsive upward BMS. Zone = `[body_low, body_high]`. |
| **Bearish OB**  | Last bullish candle before an impulsive downward BMS. |
| **Bullish FVG** | 3-candle: `candle[i-1].high < candle[i+1].low`. Gap = imbalance range. |
| **Bearish FVG** | 3-candle: `candle[i-1].low > candle[i+1].high`. |
| **Liquidity Pool** | Highest High + Lowest Low of previous 20 H1 candles. Acts as TP target. |

### Phase 2 — News Guardrails (`news_manager.py`)
Runs every 30 minutes.

- **ForexFactory API** → today's high-impact events (UTC times, live mode).
- **MyFxBook API** → date-range events with timezone conversion (backtest mode).
- **No-Trade Window**: event_time ± 15 minutes.
- **Broker Friction**: if `total_cost_pips > ATR(14) × 0.10`, abort.

### Phase 3 — Execution (`execution_service.py`)
Runs every 60 seconds.

When price enters a mapped zone:
1. Drop to M1 candles.
2. **Rejection Wick**: wick > 50% of candle's total range, pointing away from zone.
3. **Volume**: rejection candle volume > 1.5× avg of previous 10 M1 candles.
4. **Engulfing Confirm**: following candle engulfs the rejection candle body.
5. **SL**: `zone.low − (spread + 2 pips)` in price terms.
6. **TP**: nearest H1 liquidity pool (swing high/low).
7. **RR**: must be ≥ 3:1. Trade rejected otherwise.
8. **Lot Size**: `risk_amount / (sl_pips × tick_value)` where `risk_amount = balance × 1%`.

### Phase 4 — Backtesting (`backtester.py`)
- Walk-forward: re-maps zones every H1 bar.
- Injects historical MyFxBook news events.
- **News-Affected Slippage**: +5 pips added to spread when signal overlaps a high-impact window.
- Outputs CSV report + prints summary statistics.

---

## Pip Formula (5-Digit Brokers)

```
point  = 10^(-digits)                # 0.00001 for EURUSD (5-digit)
pips   = price_difference / point / 10
```

Examples:
- EURUSD: digits=5, point=0.00001, 1 pip = 0.0001
- USDJPY: digits=3, point=0.001,   1 pip = 0.01
- XAUUSD: digits=2, point=0.01,    1 pip = 0.1

---

## Broker Costs (HFM)

| Symbol  | Commission | Spread (typical) |
|---------|-----------|-----------------|
| EURUSD  | +$3/lot   | 0.0 pip         |
| USDJPY  | +$3/lot   | 0.4 pip         |
| XAUUSD  | +$3/lot   | variable        |

`Total Cost = spread_pips + (commission_usd / tick_value)`

---

## Configuration Reference (`config.json`)

| Key | Description |
|-----|-------------|
| `symbols` | List of symbols to trade |
| `risk.risk_percent` | % of balance risked per trade (default 1%) |
| `risk.commission_per_lot` | HFM round-trip commission in USD |
| `risk.min_rr_ratio` | Minimum Risk:Reward required (default 3.0) |
| `news.api_key` | ForexNews API key from `forexnews.gre8i.com` |
| `news.buffer_minutes` | No-trade window radius around event (default 15) |
| `bridge.zmq_host` | Windows IP as seen from WSL |
| `bridge.zmq_port` | ZMQ port (default 5555) |
| `mode` | `"live"` or `"backtest"` |

---

## Key Design Decisions

- **DDD layers**: Domain has zero imports from infrastructure. Application depends only on domain interfaces. Infrastructure implements those interfaces.
- **MT5 isolation**: `mt5_gateway.py` and `zmq_server.py` are the only files that import `MetaTrader5`. They run on Windows only.
- **UTC everywhere**: Every timestamp in the system is `datetime` with `tzinfo=timezone.utc`. Broker server time and news API times are all normalised on ingestion.
- **No black-box indicators**: Only price action (candle body/wick geometry), raw volume, and timestamps.
- **Caching**: All API responses cached to `.cache/` with TTL (30 min news, 5 min spreads) to avoid rate limits.