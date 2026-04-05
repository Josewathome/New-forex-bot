# src/config.py

# ══════════════════════════════════════════════════════════════════
#  config.py  —  ALL settings live here. One file. Nothing else.
#  Add config.py to .gitignore so your password is never uploaded.
# ══════════════════════════════════════════════════════════════════


# ── MT5 Account Credentials ────────────────────────────────────────
TRADING_ID       = 57427774
MT5_PASSWORD     = "g3YuQ%U9CU9#Pg$"
MT5_SERVER       = "HFMarketsKE-Demo2"
ACCOUNT_NICKNAME = "Demojose"
ACCOUNT_CURRENCY = "KES"          # currency your MT5 account is in
ACCOUNT_BALANCE  = 500_000.0      # starting balance (used in backtest)
LEVERAGE         = "1:400"

# Path to terminal64.exe — leave blank if MT5 is already open.
MT5_PATH = ""


# ── News API ───────────────────────────────────────────────────────
NEWS_API_KEY = "sk-NN7QNf0uhaCs_whD0Rly2T7v1oCdTQIVqI-6d0wWH4I"


# ── Symbols to Trade ───────────────────────────────────────────────
SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY", "AUDUSD"]


# ── Risk Management ────────────────────────────────────────────────
RISK_PERCENT        = 1.0    # % of account balance risked per trade
COMMISSION_PER_LOT  = 3.0    # HFM round-trip commission in USD per lot
MIN_RR              = 3.0    # minimum Risk:Reward ratio (break-even win rate = 25%)
MAX_OPEN_TRADES     = 1      # max simultaneous trades per symbol

# ── UPGRADED: Dynamic ATR-Based SL Multipliers ─────────────────────
ATR_SL_MULTIPLIERS: dict[str, float] = {
    "EURUSD": 0.20, "GBPUSD": 0.20, "AUDUSD": 0.20,   # Majors
    "USDJPY": 0.25, "EURJPY": 0.25, "GBPJPY": 0.25,   # JPY pairs
    "XAUUSD": 0.30, "XAGUSD": 0.30,                   # Metals
}
ATR_SL_MULTIPLIER_DEFAULT = 0.20   # fallback for any symbol not in the dict

EXTRA_PIPS_SL: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 6.0, "EURJPY": 6.0, "GBPJPY": 6.0,
    "XAUUSD": 10.0, "XAGUSD": 10.0,
}
EXTRA_PIPS_SL_DEFAULT = 3.0


# ── Zone Detection (Order Blocks & FVGs) ───────────────────────────
OB_IMPULSE_CANDLES    = 3
OB_IMPULSE_MULTIPLIER = 2.0
LIQUIDITY_LOOKBACK    = 10   # CHANGED from 20 → was too far, now 10
FVG_MIN_ATR_RATIO     = 0.3


# ── M5 Change of Character (ChoCh) Entry Filters ──────────────────
MIN_DISPLACEMENT_VOLUME_RATIO = 1.1
CANDLE_SIZE_RATIO = 1.5


# ── Weighted Scoring System ────────────────────────────────────────
SETUP_SCORE_THRESHOLD = 3
SETUP_RISK_A_PLUS     = 1.0
SETUP_RISK_B          = 0.5
SETUP_RISK_C          = 0.25
COUNTER_TREND_MIN_RR  = 1.5

# ── ATR Zone Buffer ────────────────────────────────────────────────
ZONE_ATR_BUFFER_PCT = 0.10


# ── Candle Fetch Counts ───────────────────────────────────────────
H4_CANDLE_COUNT  = 50
H1_CANDLE_COUNT  = 100
M5_CANDLE_COUNT  = 60
M1_CANDLE_COUNT  = 30


# ── H4 Macro Bias Filter ──────────────────────────────────────────
H4_EQUILIBRIUM_LOOKBACK = 20


# ── News Guardrail ────────────────────────────────────────────────
HIGH_IMPACT_BUFFER_MINS    = 30
MEDIUM_IMPACT_BUFFER_MINS  = 30
NEWS_BUFFER_MINUTES        = 15
STRICT_NEWS_FILTER         = True
ATR_COST_THRESHOLD         = 0.10

# ── Trading Sessions (London/NY overlap only) ──────────────────────
TRADING_SESSIONS = {
    "LONDON":   {"start": 8,  "end": 16},
    "NEW_YORK":  {"start": 13, "end": 21},
}


# ── Live Loop ──────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = 60


# ── Backtest ───────────────────────────────────────────────────────
NEWS_SLIPPAGE_PIPS   = 5.0
MAX_FORWARD_M1_BARS  = 4_320
BACKTEST_MONTHS_BACK = 6
BACKTEST_START_DATE  = ""
BACKTEST_END_DATE    = ""


# ── Symbol Metadata (5-digit broker) ───────────────────────────────
SYMBOL_DIGITS: dict[str, int] = {
    "EURUSD": 5, "GBPUSD": 5, "AUDUSD": 5, "USDCAD": 5,
    "USDCHF": 5, "NZDUSD": 5, "EURGBP": 5,
    "USDJPY": 3, "EURJPY": 3, "GBPJPY": 3,
    "XAUUSD": 2, "XAGUSD": 3,
}

TICK_VALUE_USD: dict[str, float] = {
    "EURUSD": 10.0, "GBPUSD": 10.0, "AUDUSD": 10.0,
    "USDCAD":  7.7, "USDCHF": 11.2, "NZDUSD": 10.0,
    "USDJPY":  9.1, "EURJPY":  9.1, "GBPJPY":  9.1,
    "XAUUSD": 10.0, "XAGUSD":  5.0,
}

KES_PER_USD = 130.0

SYMBOL_CURRENCIES: dict[str, list[str]] = {
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "XAUUSD": ["USD"],
    "USDJPY": ["USD", "JPY"],
    "AUDUSD": ["AUD", "USD"],
}

SYMBOL_MIN_SCORE: dict[str, int] = {
    "EURUSD": 5,
    "GBPUSD": 5,
}


# ── Trade Monitor ──────────────────────────────────────────────────
MONITOR_ENABLED             = True
MONITOR_ACT_ON_SIGNALS      = True

# NEW: faster exit conviction (was 3 → now 2)
MONITOR_CLOSE_CONVICTION    = 2
# Faster breakeven trigger (was 1.5 → now 1.2)
BREAKEVEN_RR_TRIGGER        = 1.2
MONITOR_TRAIL_CONVICTION    = 1
MONITOR_CONFIRM_WINDOW_M5   = 3

MFE_STALL_BARS              = 6
OPPOSING_ZONE_WARN_ATR      = 1.0

# ── Multi‑level Take Profit (NEW) ─────────────────────────────────
TIERED_TP_ENABLED        = True
TIERED_TP1_RATIO         = 2.0
TIERED_TP1_CLOSE_PCT     = 0.50      # close 50% at TP1
TIERED_TP2_RATIO         = 3.5       # second target
TIERED_TP2_CLOSE_PCT     = 0.35      # close 35% at TP2
FINAL_RUNNER_PCT         = 0.15      # let 15% run to original liquidity pool

# ── Minimum SL distance ────────────────────────────────────────────
MIN_SL_PIPS: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 5.0, "EURJPY": 5.0, "GBPJPY": 5.0,
    "XAUUSD": 50.0, "XAGUSD": 30.0,
}
MIN_SL_PIPS_DEFAULT = 3.0