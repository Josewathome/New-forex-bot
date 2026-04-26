# ZoneBot — Algorithmic Forex Trading Bot

> Zone-based Smart Money Concept (SMC / ICT) trading engine with automatic trade management, multi-phase entry, and a live web dashboard.

[![Python](https://img.shields.io/badge/Python-3.10-blue?logo=python)](https://www.python.org/)
[![MetaTrader 5](https://img.shields.io/badge/Broker-MetaTrader%205-blueviolet)](https://www.metatrader5.com/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker)](https://docs.docker.com/compose/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## Overview

ZoneBot connects to a MetaTrader 5 broker account and autonomously identifies high-probability trade setups using Order Block, Fair Value Gap, and Change of Character (ChoCh) analysis across H4 / H1 / M5 / M1 timeframes. Every trade is scored against six conviction factors, sized dynamically to risk, and managed through a three-stage take-profit ladder. A built-in web dashboard gives you live analytics and real-time control without touching the code.

**What it does every 60 seconds:**

1. Re-maps supply/demand zones on every H1 bar close.
2. Scores entry candidates across all six pairs and executes the highest-conviction setups first — **Phase 1**.
3. Re-enters top-tier setups (score ≥ 5 / 6) on the same symbol once a first trade hits TP1, "doubling down on a proven winner" — **Phase 2**.
4. Monitors open trades: trails SL to swing pivots, closes at TP1 → TP2 → runner, exits stalling trades after a configurable duration.
5. Pauses trading 30 minutes before any high-impact news event.

---

## Trading Pairs

| Symbol | Type |
|--------|------|
| EURUSD | Forex major |
| GBPUSD | Forex major |
| XAUUSD | Gold (spot) |
| USDJPY | Forex major |
| AUDUSD | Forex major |
| USDCHF | Forex major |

Active sessions: **London 08:00–16:00 UTC** · **New York 13:00–21:00 UTC**

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                       Docker Container (mt5)                      │
│                                                                  │
│   ┌──────────────────────┐       ┌──────────────────────────┐   │
│   │   MetaTrader 5       │       │      Python Bot           │   │
│   │   (Wine + noVNC)     │◄─────►│   src/main.py             │   │
│   │                      │ RPyC  │                          │   │
│   │   Port 3000 → noVNC  │       │   Flask Dashboard         │   │
│   │   Port 18812 → RPC   │       │   Port 8080 → 8000        │   │
│   └──────────────────────┘       └──────────────────────────┘   │
│                                                                  │
│   Volume: ./ → /bot   (project files mounted from host)          │
└──────────────────────────────────────────────────────────────────┘
```

**Component map:**

| Layer | Module | Responsibility |
|---|---|---|
| Entry point | `src/main.py` | Two-phase loop, startup wiring |
| Analysis | `src/application/analysis.py` | Zone mapping, ChoCh detection, scoring |
| Execution | `src/application/execution_service.py` | Candidate collection, ranking, trade placement |
| Monitoring | `src/application/trade_monitor.py` | TP1/TP2 partial closes, SL trail, stall exit |
| MT5 bridge | `src/infrastructure/mt5_bridge/mt5_gateway.py` | Raw MT5 API via RPyC (Docker) or native (Windows) |
| Dashboard | `src/api/server.py` + `src/dashboard/index.html` | Flask REST API + browser UI |

---

## Prerequisites

### Docker (recommended — works on Windows, macOS, Linux)

| Requirement | Minimum |
|---|---|
| Docker Desktop | 24+ |
| Docker Compose | 2.20+ (bundled with Docker Desktop) |
| Free RAM | 2 GB |
| Free Disk | 3 GB (MT5 image is ~1.4 GB) |
| MT5 account | Demo or live account with any supported broker |

### Native Windows (no Docker)

| Requirement | Notes |
|---|---|
| Python 3.10 | Exact version — `metatrader5` package requires 3.10 |
| MetaTrader 5 | Desktop client installed and logged in |
| `metatrader5` package | Windows-only; installs automatically via `requirements.txt` |

> **Linux / macOS users must use Docker.** The `metatrader5` Python package is Windows-only; the container uses `mt5linux` over RPyC to bridge the gap.

---

## Quick Start (Docker)

### 1 — Clone the repository

```bash
git clone https://github.com/Josewathome/zonebot.git
cd zonebot
```

### 2 — Create your environment file

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```dotenv
# MT5 Account Credentials
TRADING_ID=123456789           # Your MT5 account number
MT5_PASSWORD=YourPassword      # Broker login password
MT5_SERVER=BrokerName-Server   # e.g. HFMarketsKE-Demo2

# VNC login for the MT5 desktop UI inside the container
VNC_USER=admin
VNC_PASSWORD=changeme

# Dashboard authentication (required to access the web UI and REST API)
DASHBOARD_PASSWORD=choose_a_strong_password

# News filtering (optional — MT5 built-in calendar is used when this is blank)
NEWS_API_KEY=
```

> `.env` is listed in `.gitignore` and will never be committed.

### 3 — Build and start

```bash
docker-compose up -d --build
```

On first run this pulls `gmag11/metatrader5_vnc` (~1.4 GB) and installs Python dependencies. Subsequent starts take a few seconds.

### 4 — Log in to MetaTrader 5 (one-time setup)

Open **http://localhost:3000** in your browser. You will see the MT5 desktop running inside the container via noVNC.

1. Click **File → Open Account** and enter your broker credentials.
2. Wait for the charts to load.
3. Go to **Tools → Options → Expert Advisors** and tick **✓ Allow Algorithmic Trading**.

> This step is only required once per fresh container. MT5 saves the session to a Docker volume (`mt5_config`) so it survives restarts.

### 5 — Release the bot

```bash
docker exec mt5 touch /bot/ready_to_trade
```

The bot's entrypoint waits for this file before starting the trading loop. This lets you verify MT5 is fully loaded before the bot connects.

> **Note:** Because `.:/bot` maps your host folder into the container, this file is created at `c:\Users\hp\Desktop\forex_bot\ready_to_trade` on your machine. On any future container restart the bot will skip the wait and start immediately. Delete the file if you ever want to pause before trading on a restart:
> ```powershell
> Remove-Item c:\Users\hp\Desktop\forex_bot\ready_to_trade
> ```

### 6 — Watch the startup logs

```bash
docker logs -f mt5
```

Healthy startup looks like:

```
MT5 connected ✓  account=123456789  balance=10000.00 KES  server=HFMarketsKE-Demo2
Broker UTC offset detected: UTC+2  (server_ts=1745600000  real_utc=1745592800  age=3s)
Initial zone mapping...
Bot live. Watching: EURUSD | GBPUSD | XAUUSD | USDJPY | AUDUSD | USDCHF
```

### 7 — Open the dashboard

Go to **http://localhost:8000** for live trade analytics, configuration controls, and streaming logs.

---

## Running the Backtest

The backtester replays historical candle data through the same analysis engine used for live trading and writes a detailed text report.

> **Important — Docker always requires `wine python`.**
> The `MetaTrader5` package is installed inside Wine, not in the Linux layer of the container. Using `python` (without `wine`) will fail with `ModuleNotFoundError`. Always use the full command shown below.
>
> The backtest also needs a **live MT5 connection** to pull historical candle data — MT5 must be running and logged in to your broker account regardless of which scenario applies.

---

### Scenario A — Bot has never started yet (fresh container, first run)

Follow the Quick Start steps until MT5 is installed and you are logged in (steps 1–4). Then, **before** touching `ready_to_trade`, run the backtest:

```bash
# Run backtest directly — bot is not running yet
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest"
```

When the backtest finishes you can start the live bot normally:

```bash
docker exec mt5 touch /bot/ready_to_trade
```

---

### Scenario B — Live bot is currently running

The backtest runs as a completely separate process alongside the live bot — **you do not need to stop trading**. Open a second terminal and run:

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest"
```

Both processes share the same MT5 connection. The live bot continues placing and managing trades while the backtest reads historical bars in the background.

---

### Scenario C — Bot has been running for a day and you want a backtest

Same command as Scenario B — just open a new terminal while the bot is running:

```bash
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest"
```

If you prefer to stop the live bot first (e.g. to avoid any MT5 load during the run):

```bash
# 1. Stop the live bot process inside the container (sends SIGTERM for a clean shutdown)
docker exec mt5 pkill -f "src.main"

# 2. Run the backtest
docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 wine python -m src.main --backtest"

# 3. Restart the container to bring the live bot back up
docker-compose restart
```

> After `docker-compose restart` the container detects `ready_to_trade` already exists (from your first setup) and starts the live bot immediately.

---

### Natively (Windows — no Docker)

```bash
python -m src.main --backtest
```

---

### What the backtest does

1. Fetches H4 / H1 / M5 / M1 history from MT5 for all configured symbols.
2. Runs the full zone-mapping → signal detection → scoring → entry → trade-management simulation.
3. Writes a report to `src/backtest_results/backtest_YYYY-MM-DD_YYYY-MM-DD_report.txt`.

Because `.:/bot` maps your host folder into the container, the report file appears directly on your desktop under `src/backtest_results/` — no copying needed.

### Configuring the date range (`src/config.py`)

```python
BACKTEST_MONTHS_BACK = 6          # default: last 6 months
BACKTEST_START_DATE  = ""         # override with "2024-01-01"
BACKTEST_END_DATE    = ""         # override with "2024-06-30"
```

Backtests also run **automatically every Saturday at 06:00 UTC** and can be emailed to you when `EMAIL_ENABLED = True` in config.

> **Tip:** If MT5 has insufficient history, open the symbol chart inside MT5, right-click the chart, and select **Load More History** before running a backtest.

---

## Running Live Trading (Native Windows)

Ensure MT5 is open, logged in, and algorithmic trading is enabled.

```bash
# Install dependencies (first time only)
pip install -r src/requirements.txt

# Start the bot
python -m src.main
```

**Graceful shutdown:** press `Ctrl+C`. The bot saves a clean checkpoint before exiting.

---

## Docker Reference

### Common commands

| Action | Command |
|---|---|
| Start in background | `docker-compose up -d` |
| Rebuild after code changes | `docker-compose up -d --build` |
| Stream live logs | `docker logs -f mt5` |
| Open a shell inside the container | `docker exec -it mt5 bash` |
| Run backtest | `docker exec -it --user abc mt5 bash -c "cd /bot && DISPLAY=:1 PYTHONUTF8=1 wine python -m src.main --backtest"` |
| Stop the container | `docker-compose down` |
| Stop and erase MT5 saved session | `docker-compose down -v` |

### Ports

| Port | Service | URL |
|---|---|---|
| `3000` | MT5 desktop (noVNC browser) | http://localhost:3000 |
| `8000` | Bot dashboard | http://localhost:8000 |

### Persistent volumes

| Volume | Contents |
|---|---|
| `mt5_config` | MT5 account session, chart layouts — survives container restarts |
| `./ → /bot` | Your project files live-mounted from the host — code changes take effect on container restart |

---

## Configuration

All settings live in [`src/config.py`](src/config.py). Credentials are read from environment variables; everything else is edited directly in the file. Changes to most values are also available live through the dashboard's **Configuration** tab without a restart.

### Risk & Position Sizing

| Setting | Default | Description |
|---|---|---|
| `RISK_PERCENT` | `1.0` | Percentage of account balance risked per trade |
| `MIN_RR` | `3.0` | Minimum reward-to-risk ratio required to enter |
| `MAX_OPEN_TRADES` | `6` | Maximum simultaneous positions across all symbols |
| `MAX_TRADES_PER_SYMBOL` | `2` | Maximum concurrent positions on one symbol |
| `MARGIN_SAFETY_FACTOR` | `2.0` | Skip trade if required margin > free_margin ÷ factor |

### Two-Phase Entry

| Setting | Default | Description |
|---|---|---|
| `PHASE2_ENABLED` | `True` | Enable conviction re-entry after TP1 is hit |
| `PHASE2_MIN_TRADE_SCORE` | `5` | Minimum score (out of 6) for a Phase 2 re-entry |

Phase 1 collects candidates from all symbols, ranks by score, and executes the strongest setup first. Phase 2 adds a second position on a symbol only after an existing trade on that symbol has already hit TP1 (a proven winner) and a completely fresh signal evaluation still scores ≥ 5.

### Take-Profit Ladder

| Setting | Default | Description |
|---|---|---|
| `TIERED_TP1_RATIO` | `2.0` | First TP level — close 60 % of position at 2× SL distance |
| `TIERED_TP2_RATIO` | `4.0` | Second TP level — close 25 % of position at 4× SL distance |
| `FINAL_RUNNER_PCT` | `0.15` | Remaining 15 % runs to the liquidity-pool TP |
| `BREAKEVEN_RR_TRIGGER` | `2.5` | Move SL to entry + 0.3 R when trade reaches this R:R |
| `TP1_PROFIT_LOCK_R` | `0.3` | After TP1, lock SL at entry + 0.3 R to protect profit |

### News Filter

The bot blocks new entries around high-impact economic events. Two calendar sources are supported and combined automatically:

| `NEWS_API_KEY` set? | Source |
|---|---|
| Yes | ForexFactory (primary) **+** MT5 built-in calendar (enrichment) — events from both sources are merged and deduplicated |
| No | MT5 built-in calendar only — broker-provided data, no external API key needed |

When both sources are active, events from the same currency within a 15-minute window are deduplicated; the higher-impact classification wins.

| Setting | Default | Description |
|---|---|---|
| `HIGH_IMPACT_BUFFER_MINS` | `30` | Pause new entries 30 min before/after high-impact events |
| `STRICT_NEWS_FILTER` | `True` | Block entry when either currency in the pair has an event |
| `NEWS_REFRESH_INTERVAL_MINUTES` | `10` | How often to re-fetch the economic calendar |

> **No API key required for basic protection.** MT5 ships an economic calendar for all connected brokers. Set `NEWS_API_KEY` only if you want ForexFactory data as a primary/richer source.

### Trade Scoring (0 – 6)

A setup must reach `TRADE_SCORE_THRESHOLD` (default `2`) to be executed. Dynamic risk scales with the final score.

| Factor | Points | Condition |
|---|---|---|
| HTF alignment | 0 – 2 | H4 bias matches zone direction |
| Liquidity sweep | 1 | First touch of an Order Block |
| Clean FVG | 1 | Fair Value Gap size > 50 % of H1 ATR |
| Strong displacement | 1 | Volume ratio ≥ 1.5 × average |
| Kill zone | 1 | Entry during London (08–11) or New York (14–17) UTC |
| **Max score** | **6** | |

**Dynamic risk by score:** `≥ 4 → 1.5 %` · `3 → 1.0 %` · `≤ 2 → 0.5 %`

### Session & Timing

```python
TRADING_SESSIONS = {
    "LONDON":   {"start": 8,  "end": 16},  # UTC
    "NEW_YORK":  {"start": 13, "end": 21},  # UTC
}
KILL_ZONES = {
    "LONDON":   (8, 11),   # high-probability London open window
    "NEW_YORK": (14, 17),  # high-probability New York open window
}
LOOP_INTERVAL_SECONDS      = 60     # main loop cadence
MAX_TRADE_DURATION_MINUTES = 600    # force-close stalled trades after 10 hours
```

---

## Web Dashboard

Access at **http://localhost:8000** (Docker) or **http://localhost:8080** (local).

### Authentication

The dashboard and all REST API endpoints are protected by JWT (JSON Web Token) authentication. On first visit, a login screen prompts for your `DASHBOARD_PASSWORD`.

| Token | Lifetime | Storage |
|---|---|---|
| Access token | 15 minutes (configurable) | Browser `sessionStorage` — cleared on tab close |
| Refresh token | 7 days (configurable) | Browser `localStorage` — persists across tabs |

The access token is refreshed silently in the background when it expires — you will not be logged out mid-session. The refresh token is revoked on logout.

**Rotating the JWT secret** (Configuration → Security tab) immediately invalidates all active sessions everywhere. Use this if you suspect a token has been compromised. You will be prompted for your password before rotation proceeds.

If `DASHBOARD_PASSWORD` is not set in `.env`, a random one-time password is printed to the log at startup:

```
DASHBOARD_PASSWORD not set — auto-generated: <random-token>
Add DASHBOARD_PASSWORD=<above> to your .env file.
```

### Dashboard tabs

| Tab | What you see |
|---|---|
| **Analytics** | Win rate, total PnL, per-symbol breakdown table, recent trade history |
| **Pairs** | Add or remove trading symbols — takes effect within one cycle |
| **Configuration** | Edit risk %, R:R, TP ratios, loop interval in real time; rotate JWT secret |
| **Logs** | Colour-coded live log viewer (last 200 lines — INFO / WARN / ERROR) |

### REST API

All endpoints require `Authorization: Bearer <access_token>` except the auth endpoints themselves.

**Auth (public)**

| Method | Endpoint | Body | Response |
|---|---|---|---|
| `POST` | `/api/auth/login` | `{"password": "..."}` | `{access_token, refresh_token, expires_in}` |
| `POST` | `/api/auth/refresh` | `{"refresh_token": "..."}` | `{access_token, expires_in}` |
| `POST` | `/api/auth/logout` | `{"refresh_token": "..."}` | `{ok: true}` |
| `POST` | `/api/auth/rotate-secret` | `{"password": "..."}` | `{ok: true, message}` |

**Protected data endpoints**

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | Bot uptime, server time, active symbols |
| `GET` | `/api/stats` | Trade analytics — global and per-symbol |
| `GET` | `/api/trades?n=50` | Recent trade list (last *n* trades) |
| `GET` | `/api/config` | Current live configuration values |
| `PATCH` | `/api/config` | Update config values without restart |
| `GET` | `/api/symbols` | Active symbol list |
| `POST` | `/api/symbols` | Add a symbol `{"symbol": "NZDUSD"}` |
| `DELETE` | `/api/symbols/<SYM>` | Remove a symbol |
| `GET` | `/api/logs?n=200` | Tail of `trading_bot.log` |

**Example — fetch status with curl:**
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"your_password"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/status
```

---

## Project Structure

```
zonebot/
├── src/
│   ├── main.py                           # Entry point — live trading & backtest mode
│   ├── config.py                         # All settings in one file
│   ├── application/
│   │   ├── analysis.py                   # Zone mapping, ChoCh detection, setup scoring
│   │   ├── execution_service.py          # Phase 1 & 2 entry, EntryCandidate ranking
│   │   ├── trade_monitor.py              # Open-trade management (TP, SL trail, stall)
│   │   ├── backtester.py                 # Historical simulation engine
│   │   ├── news_manager.py               # Economic calendar guard
│   │   ├── scheduler.py                  # Cleanup & weekly backtest scheduling
│   │   └── email_service.py              # Backtest report delivery via SMTP
│   ├── domain/
│   │   ├── entities.py                   # Trade, Zone, Candle, Signal models
│   │   ├── repositories.py               # Abstract repository interfaces
│   │   └── value_objects.py              # PipCalculator, RiskParameters
│   ├── infrastructure/
│   │   ├── mt5_bridge/mt5_gateway.py     # MT5 API wrapper (RPyC-aware, UTC detection)
│   │   ├── market_data_repo.py           # Candle & tick data fetching
│   │   ├── trade_repo.py                 # Position management with UTC correction
│   │   ├── news_client.py                # ForexFactory + MyFxBook HTTP client
│   │   ├── mt5_news_client.py            # MT5 built-in calendar client (no API key needed)
│   │   ├── composite_news_client.py      # Merges + deduplicates multiple news sources
│   │   ├── trade_journal.py              # Persistent trade history for dashboard
│   │   ├── checkpoint_service.py         # State save/restore on restart
│   │   ├── cleanup_service.py            # Log / cache / zone retention
│   │   └── spread_calculator.py          # Cost and slippage model
│   ├── api/server.py                     # Flask REST API (dashboard backend)
│   ├── dashboard/index.html              # Single-page dashboard UI (dark theme)
│   └── requirements.txt
├── docker-compose.yml                    # Single-service Docker setup
├── Dockerfile                            # python:3.10-slim + mt5linux
├── entrypoint.sh                         # Waits for MT5 RPyC, then starts bot
├── .env.example                          # Credential template — copy to .env
└── README.md
```

---

## State & Recovery

| Mechanism | Location | Retention | Purpose |
|---|---|---|---|
| Checkpoints | `.checkpoints/` | Last save | Survives restarts; resumes loop from last known state |
| JWT secret | `.checkpoints/jwt_secret.txt` | Permanent | Signing key for dashboard auth tokens; rotatable via UI |
| Config overrides | `.checkpoints/config_overrides.json` | Permanent | Dashboard config edits that survive restarts |
| Zone snapshots | `zones/` | 5 days | Per-symbol timestamped zone files |
| Trade journal | `trade_journal.json` | Permanent | Drives dashboard analytics; grows over time |
| Logs | `logs/trading_bot.log` | 3 days | Auto-cleaned by daily CleanupService |

---

## Troubleshooting

### Bot says "MT5 connection failed"
- Open **http://localhost:3000** and confirm MT5 is running inside the container.
- Ensure **Tools → Options → Expert Advisors → Allow Algorithmic Trading** is ticked.
- Double-check `TRADING_ID`, `MT5_PASSWORD`, and `MT5_SERVER` in your `.env` file.

### No trades are being placed
- Check logs for `SCORE BELOW SYM MIN` or `low trade score` — the conviction filter may be rejecting all setups in current market conditions.
- Confirm the current time falls within a London or New York session.
- Look for `NEWS BLOCK` log lines — a high-impact event may be within the 30-minute buffer.
- Verify `MAX_OPEN_TRADES` (6) and `MAX_TRADES_PER_SYMBOL` (2) limits have not been reached.

### News calendar shows 0 events
- If `NEWS_API_KEY` is blank, the bot uses MT5's built-in calendar. Check `MT5 calendar: 0 event definitions` in the logs — this means MT5 has no calendar data for your broker. Try a different broker or set `NEWS_API_KEY` for the ForexFactory source.
- If `NEWS_API_KEY` is set but ForexFactory returns 0 events, confirm the key is valid and check for HTTP 403 errors in the log — `docker logs mt5 | grep ForexFactory`.

### Dashboard login fails / "Invalid password"
- Check that `DASHBOARD_PASSWORD` is set correctly in your `.env` file and that the container was restarted after the change.
- If `DASHBOARD_PASSWORD` is blank, the bot auto-generates a one-time password at startup — find it in the logs: `docker logs mt5 | grep "auto-generated"`.
- If you have forgotten the password, set a new one in `.env` and restart the container. The JWT secret in `.checkpoints/jwt_secret.txt` is unaffected and existing sessions will remain valid until they expire.

### "Token expired" / constantly redirected to login
- Access tokens last 15 minutes; the dashboard refreshes them silently using the refresh token. If silent refresh fails (e.g. the bot was restarted and the secret rotated), sign in again.
- Extend `JWT_ACCESS_EXPIRES_MINUTES` or `JWT_REFRESH_EXPIRES_DAYS` in `.env` if you want longer sessions.

### Dashboard shows no data
- Confirm port 8000 is not occupied: `docker logs mt5 | grep Dashboard`.
- Restart the container: `docker-compose restart`.

### Backtest produces no trades
- MT5 may not have enough history. Open the chart, right-click → **Load More History**, then retry.
- Reduce `BACKTEST_MONTHS_BACK = 1` for a quick first test.

### Container exits immediately after starting
```bash
docker logs mt5
```
Common causes: missing environment variables (the bot exits hard if `TRADING_ID`, `MT5_PASSWORD`, or `MT5_SERVER` are not set), or the RPyC bridge on port 18812 failed to start inside the container.

---

## Risk Disclaimer

This software is provided for educational and research purposes only. Algorithmic trading carries significant financial risk. Past backtest performance does not guarantee future results. Always test on a **demo account** before using real funds. You are solely responsible for any trading decisions made by this bot on your account.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
