# ══════════════════════════════════════════════════════════════════
#  config.py  —  ALL settings live here. One file. Nothing else.
#  Add config.py to .gitignore so your password is never uploaded.
# ══════════════════════════════════════════════════════════════════


# ── MT5 Account Credentials ────────────────────────────────────────
TRADING_ID       = 57427774
MT5_PASSWORD     = "g3YuQ%U9CU9#Pg$"
MT5_SERVER       = "HFMarketsKE-Demo2"
ACCOUNT_NICKNAME = "Demojose"
ACCOUNT_CURRENCY = "KES"
ACCOUNT_BALANCE  = 10_000
LEVERAGE         = "1:400"

MT5_PATH = ""


# ── News API ───────────────────────────────────────────────────────
NEWS_API_KEY = "sk-NN7QNf0uhaCs_whD0Rly2T7v1oCdTQIVqI-6d0wWH4I"


# ── Symbols to Trade ───────────────────────────────────────────────
SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY", "AUDUSD", "USDCHF"]


# ── Risk Management ────────────────────────────────────────────────
RISK_PERCENT        = 1.0
COMMISSION_PER_LOT  = 3.0
MIN_RR              = 3.0
MAX_OPEN_TRADES     = 1

# ── UPGRADED: Dynamic ATR-Based SL Multipliers ─────────────────────
ATR_SL_MULTIPLIERS: dict[str, float] = {
    "EURUSD": 0.20, "GBPUSD": 0.20, "AUDUSD": 0.20,
    "USDJPY": 0.25, "EURJPY": 0.25, "GBPJPY": 0.25,
    "XAUUSD": 0.30, "XAGUSD": 0.30,
    "USDCHF": 0.20,
}
ATR_SL_MULTIPLIER_DEFAULT = 0.20

EXTRA_PIPS_SL: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 6.0, "EURJPY": 6.0, "GBPJPY": 6.0,
    "XAUUSD": 10.0, "XAGUSD": 10.0,
    "USDCHF": 3.0,
}
EXTRA_PIPS_SL_DEFAULT = 3.0


# ── Zone Detection ────────────────────────────────────────────────
OB_IMPULSE_CANDLES    = 3
OB_IMPULSE_MULTIPLIER = 2.0
LIQUIDITY_LOOKBACK    = 10
FVG_MIN_ATR_RATIO     = 0.3


# ── M5 ChoCh Entry Filters ────────────────────────────────────────
MIN_DISPLACEMENT_VOLUME_RATIO = 1.1
CANDLE_SIZE_RATIO = 1.5


# ── Weighted Scoring ──────────────────────────────────────────────
SETUP_SCORE_THRESHOLD = 2
SETUP_RISK_A_PLUS     = 1.0
SETUP_RISK_B          = 0.5
SETUP_RISK_C          = 0.25
COUNTER_TREND_MIN_RR  = 1.5

ZONE_ATR_BUFFER_PCT = 0.15

H4_CANDLE_COUNT  = 50
H1_CANDLE_COUNT  = 100
M5_CANDLE_COUNT  = 60
M1_CANDLE_COUNT  = 30

H4_EQUILIBRIUM_LOOKBACK = 20

# ── News Guardrail ────────────────────────────────────────────────
HIGH_IMPACT_BUFFER_MINS    = 30
MEDIUM_IMPACT_BUFFER_MINS  = 0   # only HIGH events block trading
NEWS_BUFFER_MINUTES        = 15
STRICT_NEWS_FILTER         = True
ATR_COST_THRESHOLD         = 0.10

# ── Trading Sessions ──────────────────────────────────────────────
TRADING_SESSIONS = {
    "LONDON":   {"start": 8,  "end": 16},
    "NEW_YORK":  {"start": 13, "end": 21},
}

LOOP_INTERVAL_SECONDS = 60

# ── Backtest ──────────────────────────────────────────────────────
NEWS_SLIPPAGE_PIPS   = 5.0
MAX_FORWARD_M1_BARS  = 4_320
BACKTEST_MONTHS_BACK = 6
BACKTEST_START_DATE  = ""
BACKTEST_END_DATE    = ""

# ── Symbol Metadata ───────────────────────────────────────────────
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
    "USDCHF": ["USD", "CHF"],
}

SYMBOL_MIN_SCORE: dict[str, int] = {
    "EURUSD": 2,
    "GBPUSD": 2,
}

# ── Trade Monitor ─────────────────────────────────────────────────
MONITOR_ENABLED             = True
MONITOR_ACT_ON_SIGNALS      = True

# FIX: Lowered from 3 → 2.
# With BREAKEVEN_RR_TRIGGER at 2.5 the breakeven_trigger signal almost never
# fires, making the maximum reachable conviction = 2 (reverse_choch + mfe_stall).
# Both of those signals are 88-97% accurate in the last backtest report but were
# taking ZERO actions because conviction 3 was unreachable. This unlocks them.
MONITOR_CLOSE_CONVICTION    = 2

BREAKEVEN_RR_TRIGGER        = 2.5
MONITOR_TRAIL_CONVICTION    = 1
MONITOR_CONFIRM_WINDOW_M5   = 3
MFE_STALL_BARS              = 15
OPPOSING_ZONE_WARN_ATR      = 1.0
MIN_PROFIT_RATIO_TO_CLOSE   = 1.5

# ── Multi‑level Take Profit ────────────────────────────────────────
TIERED_TP_ENABLED        = True
TIERED_TP1_RATIO         = 2.0
TIERED_TP1_CLOSE_PCT     = 0.60
TIERED_TP2_RATIO         = 4.0
TIERED_TP2_CLOSE_PCT     = 0.25
FINAL_RUNNER_PCT         = 0.15

# ── NEW: STALL EXIT RULE ──────────────────────────────────────────
STALL_EXIT_REQUIRES_TP1 = True

# ── NEW: TRADE QUALITY SCORING ────────────────────────────────────
TRADE_SCORE_THRESHOLD   = 2
SCORE_HTF_ALIGNMENT     = 2
SCORE_LIQUIDITY_SWEEP   = 1
SCORE_CLEAN_FVG         = 1
SCORE_STRONG_DISPLACEMENT = 1
SCORE_KILL_ZONE         = 0

# ── NEW: DYNAMIC POSITION SIZING ──────────────────────────────────
RISK_HIGH_CONVICTION    = 1.5
RISK_MEDIUM_CONVICTION  = 1.0
RISK_LOW_CONVICTION     = 0.5

# ── NEW: TIME‑BASED EXIT ──────────────────────────────────────────
MAX_TRADE_DURATION_MINUTES = 600

# ── NEW: SESSION FILTER (KILL ZONES) ──────────────────────────────
KILL_ZONES = {
    "LONDON":   (8, 11),
    "NEW_YORK": (14, 17),
}

# ── Minimum SL distance ───────────────────────────────────────────
MIN_SL_PIPS: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 5.0, "EURJPY": 5.0, "GBPJPY": 5.0,
    "XAUUSD": 50.0, "XAGUSD": 30.0,
    "USDCHF": 3.0,
}
MIN_SL_PIPS_DEFAULT = 3.0