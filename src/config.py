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
# REMOVED: static EXTRA_PIPS_SL dict (was the primary cause of stop-hunting).
# These multipliers are applied to the H1 ATR(14) value to produce a
# volatility-adjusted buffer that expands in volatile sessions and
# contracts in quiet ones.
#
#   dynamic_buffer_price = ATR(14) × multiplier
#
# Calibrated from the backtest data:
#   USDJPY H1 ATR ≈ 25-35 pips  →  0.25 × 30 = 7.5 pip buffer  ✓
#   XAUUSD H1 ATR ≈ 50-70 pips  →  0.30 × 60 = 18 pip buffer   ✓
#   EURUSD H1 ATR ≈ 12-20 pips  →  0.20 × 15 = 3 pip buffer    ✓
ATR_SL_MULTIPLIERS: dict[str, float] = {
    "EURUSD": 0.20, "GBPUSD": 0.20, "AUDUSD": 0.20,   # Majors
    "USDJPY": 0.25, "EURJPY": 0.25, "GBPJPY": 0.25,   # JPY pairs
    "XAUUSD": 0.30, "XAGUSD": 0.30,                   # Metals
}
ATR_SL_MULTIPLIER_DEFAULT = 0.20   # fallback for any symbol not in the dict

# Legacy static SL buffer kept as a hard fallback ONLY if ATR is 0
# (e.g. first candle of session, no prior close to compute TR).
EXTRA_PIPS_SL: dict[str, float] = {
    "EURUSD": 3.0, "GBPUSD": 3.0, "AUDUSD": 3.0,
    "USDJPY": 6.0, "EURJPY": 6.0, "GBPJPY": 6.0,
    "XAUUSD": 10.0, "XAGUSD": 10.0,
}
EXTRA_PIPS_SL_DEFAULT = 3.0


# ── Zone Detection (Order Blocks & FVGs) ───────────────────────────
OB_IMPULSE_CANDLES    = 3    # candles forward to confirm an OB impulse
OB_IMPULSE_MULTIPLIER = 2.0  # impulse body must be ≥ 2.0× the OB candle body (raised from 1.5)
LIQUIDITY_LOOKBACK    = 20   # H1 candles used to find swing high/low (TP target)

# FVGs smaller than this fraction of ATR are micro-noise — skip them.
# ATR(14) on H1 EURUSD ≈ 0.0015 → min FVG size = 0.0015 × 0.3 = 0.00045 (4.5 pips).
# This alone cuts zone count by ~40–50%.
FVG_MIN_ATR_RATIO     = 0.3


# ── UPGRADED: M5 Change of Character (ChoCh) Entry Filters ─────────
# These replace the old M1 wick/volume/engulfing tripwire that
# caused 80%+ false-positive entries on noise candles.
#
# MIN_DISPLACEMENT_VOLUME_RATIO: The displacement candle that creates
# the ChoCh must have volume at least this multiple of the 10-bar M5
# average. Relaxed from 1.5 → 1.1 because FX tick volume is a proxy
# for activity, not absolute dollar flow. We want to detect a shift,
# not wait for a massive institutional explosion every single time.
MIN_DISPLACEMENT_VOLUME_RATIO = 1.1   # relaxed from 1.5 — detect shift, not explosion

# CANDLE_SIZE_RATIO: Displacement body must be ≥ this multiple of the
# average body size of the preceding 3 candles. Price action is a more
# reliable "truth" in decentralised FX than tick volume.
# Note: this check is OR-logic with volume — passing EITHER confirms displacement.
CANDLE_SIZE_RATIO = 1.5

# LEGACY — kept so existing diagnostics.py report lines still compile.
# No longer used in the primary signal path (find_m5_choch_signal).
REJECTION_WICK_RATIO = 0.3
VOLUME_RATIO_MIN     = 2.0


# ── Weighted Scoring System (replaces binary kill-switches) ─────────
# Every zone is scored before execution.  The score determines the
# setup grade and therefore the risk allocation.  This converts the
# old "one No kills the trade" architecture into a probabilistic
# "multiple Maybes can equal a Yes" system.
#
# Scoring (per zone):
#   +3 pts — Zone direction matches H4 bias        (trend alignment)
#   +2 pts — Price is in discount (bull) or premium (bear) half
#             of the H4 range                       (optimal pricing)
#
# Grade thresholds:
#   A+  score ≥ 5  →  H4 aligned + optimal price  →  full risk
#   B   score ≥ 3  →  H4 aligned, price not ideal  →  half risk
#   C   score < 3  →  counter-trend or ranging      →  quarter risk (scalp)
#
# A minimum score of 0 is used — even C-grade counter-trend setups
# are allowed, but with reduced risk and a smaller TP target (COUNTER_TREND_MIN_RR).
SETUP_SCORE_THRESHOLD = 3         # minimum score to attempt any trade (3 = A+/B only, no counter-trend)
SETUP_RISK_A_PLUS     = 1.0       # % of balance risked on A+ setups
SETUP_RISK_B          = 0.5       # % of balance risked on B setups
SETUP_RISK_C          = 0.25      # % of balance risked on C (counter-trend) setups

# Counter-trend scalps target a smaller RR (they're going against the flow)
COUNTER_TREND_MIN_RR  = 1.5       # replaces MIN_RR=2.5 for C-grade trades

# ── ATR Zone Buffer ("Fuzzy" Zone Detection) ────────────────────────
# In code 1.10000 ≠ 1.10001.  In the market they are identical.
# This buffer prevents "point-perfect failure" by expanding each zone's
# edges by 10% of the H1 ATR before checking for a tap.
# Institutions routinely front-run order blocks by 1-3 pips — the
# buffer ensures those near-misses are still caught.
ZONE_ATR_BUFFER_PCT = 0.10        # zone edges expanded by ATR × 10%


# ── UPGRADED: Candle Fetch Counts ──────────────────────────────────
H4_CANDLE_COUNT  = 50    # H4 bars fetched for macro bias + equilibrium
H1_CANDLE_COUNT  = 100   # H1 bars fetched each zone-mapping cycle
M5_CANDLE_COUNT  = 60    # M5 bars for ChoCh scan (replaces M1)
M1_CANDLE_COUNT  = 30    # M1 bars kept for backward-compat (backtester)


# ── UPGRADED: H4 Macro Bias Filter ─────────────────────────────────
# Number of H4 candles to use for the premium/discount range.
# The midpoint (equilibrium) of this range acts as the filter boundary:
#   Bullish bias → only longs BELOW equilibrium (discount zone)
#   Bearish bias → only shorts ABOVE equilibrium (premium zone)
H4_EQUILIBRIUM_LOOKBACK = 20   # H4 candles used to compute range


# ── News Guardrail (Tiered Buffers) ───────────────────────────────
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



# ══════════════════════════════════════════════════════════════════
#  PASTE THESE LINES INTO config.py
#  Place them after the ATR_COST_THRESHOLD line.
# ══════════════════════════════════════════════════════════════════

# ── Trade Monitor ──────────────────────────────────────────────────────────────

# Master switch — False disables all monitor checks entirely.
MONITOR_ENABLED             = True

# When False: record all events, change nothing (gather data mode).
# When True:  act on signals — trail SL to swing, close on high conviction.
# START WITH False. Run one full backtest. Read the monitor performance
# section of the report. Only flip to True once you know the accuracy.
MONITOR_ACT_ON_SIGNALS      = True

# ── Trail SL to swing structure (NOT to entry) ─────────────────────────────────
#
# When the monitor trails the SL, it moves it to the most recent M5 swing
# low (for longs) or swing high (for shorts) that sits above the original SL.
# This gives the trade room to breathe through a normal retrace.
#
# A RETRACE TO A SWING LOW ≠ A REVERSAL. Only a genuine break of the swing
# structure is treated as a stop.
#
# BREAKEVEN_RR_TRIGGER: how many × the SL distance the trade must be in
# profit before the first trail is considered.
# Default 2.0 = price must move 2× the SL distance in our favour.
BREAKEVEN_RR_TRIGGER        = 1.5

# ATR multiple subtracted below the swing low (long) / added above the
# swing high (short) to avoid getting stopped on a wick at the exact swing.
# 0.5 × ATR gives a half-ATR breathing gap below the swing.
MONITOR_TRAIL_ATR_BUFFER    = 0.5

# ── Conviction thresholds ─────────────────────────────────────────────────────
#
# The monitor needs multiple DIFFERENT signal types to agree before acting.
# A single signal is almost always a retrace, not a reversal.
#
# MONITOR_TRAIL_CONVICTION:
#   Minimum unique signal types within CONFIRM_WINDOW before trailing SL.
#   1 = trail on any single signal (less safe)
#   2 = trail only when 2+ different signals agree (recommended)
MONITOR_TRAIL_CONVICTION    = 1   # Start conservative — trail on BE trigger alone

# MONITOR_CLOSE_CONVICTION:
#   Minimum unique signal types before closing the trade early.
#   3 means: need BE trigger + opposing zone + reverse ChoCh (or stall) all
#   firing within the confirmation window before closing.
MONITOR_CLOSE_CONVICTION    = 3

# How many M5 bars to look back when counting unique signals.
# 3 bars = 15 minutes.  If 3 different signals fire within 15 min, act.
MONITOR_CONFIRM_WINDOW_M5   = 3

# ── Individual signal settings ────────────────────────────────────────────────
# M5 bars without a new MFE before the stall signal fires.
MFE_STALL_BARS              = 6    # 6 × 5 min = 30 minutes of no progress

# ATR multiples before an opposing zone triggers a warning.
# 1.0 = warn when price is within 1 full H1 ATR of the zone edge.
OPPOSING_ZONE_WARN_ATR      = 1.0