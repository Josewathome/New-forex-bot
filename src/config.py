# ══════════════════════════════════════════════════════════════════
#  config.py  —  ALL settings live here. One file. Nothing else.
#
#  Sensitive values (passwords, API keys) are read from environment
#  variables first, falling back to the defaults below.
#  In Docker: set them in docker-compose.yml / .env (already done).
#  Locally:   export them in your shell, or keep the defaults for dev.
#
#  FIX 4: config.py previously had all credentials hardcoded.
#  Now os.environ is checked first so docker-compose env vars take
#  effect without touching this file.
# ══════════════════════════════════════════════════════════════════

import os

# ── MT5 Account Credentials ────────────────────────────────────────
# IMPORTANT: Set these via environment variables or docker-compose.yml.
# No credentials should be hardcoded here — use the .env file locally.
_trading_id_raw = os.environ.get("TRADING_ID")
if not _trading_id_raw:
    raise RuntimeError("TRADING_ID environment variable is not set. Set it in your .env or docker-compose.yml.")
TRADING_ID = int(_trading_id_raw)

MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
if not MT5_PASSWORD:
    raise RuntimeError("MT5_PASSWORD environment variable is not set.")

MT5_SERVER = os.environ.get("MT5_SERVER")
if not MT5_SERVER:
    raise RuntimeError("MT5_SERVER environment variable is not set.")
ACCOUNT_NICKNAME = "Demojose"
ACCOUNT_CURRENCY = "KES"
ACCOUNT_BALANCE  = 10_000
LEVERAGE         = "1:400"

MT5_PATH = ""

# ── News API ───────────────────────────────────────────────────────
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")

# ── Dashboard Auth ─────────────────────────────────────────────────
# Password for the web dashboard. If empty, a random one is printed to
# the log at startup (check logs/trading_bot.log for the one-time password).
DASHBOARD_PASSWORD          = os.environ.get("DASHBOARD_PASSWORD", "")
JWT_ACCESS_EXPIRES_MINUTES  = int(os.environ.get("JWT_ACCESS_EXPIRES_MINUTES", "15"))
JWT_REFRESH_EXPIRES_DAYS    = int(os.environ.get("JWT_REFRESH_EXPIRES_DAYS", "7"))

# ── Symbols to Trade ───────────────────────────────────────────────
# EURUSD removed: only pair with negative net P&L in backtest (-98k on 40 trades).
# Re-add once entry quality improves (win rate was 7.5%, avg loss -17k).
SYMBOLS = ["GBPUSD", "XAUUSD", "USDJPY", "AUDUSD", "USDCHF"]


# ── Broker Time Zone ───────────────────────────────────────────────
# AUTO-DETECTED at startup: main.py calls gw.detect_utc_offset() and
# overwrites this value before any trades are processed.
# This default (2 = EET, common for ECN brokers) is only used as a
# fallback when detection fails — e.g. bot started while market is closed
# and no fresh EURUSD tick is available.  Change it if your broker is
# permanently on a different offset and you start the bot off-hours.
BROKER_UTC_OFFSET_HOURS: int = 2

# ── Risk Management ────────────────────────────────────────────────
RISK_PERCENT        = 1.0
COMMISSION_PER_LOT  = 3.0
MIN_RR              = 2.0   # was 3.0 — backtest: rr rejections eating valid signals; 2R still profitable
MAX_OPEN_TRADES     = 6
MAX_TRADES_PER_SYMBOL = 2    # max concurrent trades on a single symbol (Phase 1 cap)

# ── Phase 2 — Conviction Re-Entry ─────────────────────────────────
# Phase 2 fires AFTER Phase 1 in the same cycle.
# A symbol is only eligible if it already has a trade with TP1 hit
# (proven winner) and the new signal scores at maximum conviction.
PHASE2_ENABLED         = True
PHASE2_MIN_TRADE_SCORE = 5   # minimum score out of 6 to qualify for re-entry

# ── Margin Safety ────────────────────────────────────────────────
# A new trade is skipped if the margin MT5 requires for it exceeds
# free_margin / MARGIN_SAFETY_FACTOR.  Factor=2 means the trade may
# consume at most half the remaining free margin, keeping a buffer for
# existing trades' floating losses and swap charges.
# Set to 1.0 to allow trades right up to the margin limit (not recommended).
MARGIN_SAFETY_FACTOR: float = 2.0

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

# Max zones kept per H1 bar scan (newest + largest preferred).
# Without a cap, EURUSD was generating ~2.6 zones per bar = pure noise.
MAX_OBS_PER_BAR  = 10
MAX_FVGS_PER_BAR = 8


# ── M5 ChoCh Entry Filters ────────────────────────────────────────
# Raised from 1.1 → 1.3 (volume) and 1.5 → 2.0 (body).
# The old OR gate with loose thresholds produced an 8.3% win rate.
# These tighter values still allow genuine institutional moves through.
MIN_DISPLACEMENT_VOLUME_RATIO = 1.3
CANDLE_SIZE_RATIO = 2.0

# ── Legacy M1 LTF Signal Filters (find_ltf_signal / backtester) ───
REJECTION_WICK_RATIO = 0.6   # Lower wick must be >= 60% of candle range for bullish rejection
VOLUME_RATIO_MIN     = 1.0   # Volume must be >= 1× average to count as valid rejection candle


# ── Weighted Scoring ──────────────────────────────────────────────
SETUP_SCORE_THRESHOLD = 2
# Risk fractions returned by grade_setup() (A+/B/C grades).
# NOTE: execution_service overrides these with dynamic_risk() (score-based)
# after placing the trade, so only COUNTER_TREND_MIN_RR has a hard effect
# on live trades (it lowers the RR bar for C-grade counter-trend setups).
SETUP_RISK_A_PLUS     = 1.0
SETUP_RISK_B          = 0.5
SETUP_RISK_C          = 0.25
COUNTER_TREND_MIN_RR  = 1.5   # Min RR required for C-grade (counter-trend) setups

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
"""
When using the HFM Premium Account, you will likely need to raise your
New Threshold: Set ATR_COST_THRESHOLD = 0.20. This allows a cost up to 20% of the H1 ATR, which accommodates the ~1.4 pip spread on an 8-pip H1 move.
Volatility Buffer: If trading specifically during the Iran conflict spikes, a threshold of 0.25 might be necessary to avoid being completely filtered out, though this increases your "cost of business" significantly.
"""

ATR_COST_THRESHOLD         = 0.25


# How often the news manager re-fetches events from the API.
# 10 min keeps the blind-spot window below the HIGH_IMPACT_BUFFER_MINS (30 min)
# so a late-added event is always caught before the bot enters within its window.
NEWS_REFRESH_INTERVAL_MINUTES = 10

# ── Trading Sessions ──────────────────────────────────────────────
TRADING_SESSIONS = {
    "LONDON":   {"start": 8,  "end": 16},
    "NEW_YORK":  {"start": 13, "end": 21},
}

LOOP_INTERVAL_SECONDS = 60


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
    # GBPUSD: 9 losses (worst record), 3 consecutive -114/-113/-112 in Nov.
    # Raise to 3 — B-grade or better only; counter-trend entries cut.
    "GBPUSD": 3,
    # USDJPY: 4,231 score rejections vs 5 signals — H4 ranging kills score every time.
    # USDJPY profit comes from monitor exits, not classical TP logic. Allow all setups.
    "USDJPY": 0,
    # XAUUSD: 0% win rate, avg loss -132 KES (>1R loss each), -114 KES net.
    # Raise to 5 — only allow perfectly aligned XAUUSD setups (A+ only).
    "XAUUSD": 5,
}

# ── Trade Monitor ─────────────────────────────────────────────────
MONITOR_ENABLED             = True
MONITOR_ACT_ON_SIGNALS      = True

# 2 = close when any 2 distinct signal types agree within the confirm window.
# reverse_choch (20% FP alone) + mfe_stall (1.6% FP alone) together
# approach 0% combined FP — a reliable close trigger.
# 3 was too strict: monitor fired 322 events but took 0 close actions.
MONITOR_CLOSE_CONVICTION    = 2

BREAKEVEN_RR_TRIGGER        = 1.5
MONITOR_TRAIL_CONVICTION    = 1
MONITOR_CONFIRM_WINDOW_M5   = 3
MFE_STALL_BARS              = 6     # was 8 → lowered: 75/132 stall events took no action then went to loss
OPPOSING_ZONE_WARN_ATR      = 1.0
MIN_PROFIT_RATIO_TO_CLOSE   = 0.1   # was 0.3 → 75 stall non-actions had MFE below the 0.3R gate; lower to catch them
# Absolute pip floor for mfe_stall exit — fires even when R-ratio not met.
# Without this, tiny-MFE stalls (2-5 pip MFE on 50-pip SL = 0.04-0.1R) are
# ignored and the trade then hits SL.  Any positive MFE ≥ this many pips closes.
MIN_STALL_MFE_PIPS          = 2.0

# Minimum MFE (as a multiple of SL distance) required before a reverse_choch
# signal is allowed to close the trade.  Without this, reverse_choch closes
# trades on 1-pip MFE producing +2/+6 KES outcomes.
# 0.5 = price must have moved at least 0.5R in our favour first.
REVERSE_CHOCH_MIN_MFE_R     = 0.15

# Consecutive loss streak protection: after this many real losses (net-negative)
# on a symbol in a row, skip new entries on that symbol for LOSS_STREAK_PAUSE_HOURS.
# GBPUSD had 3 consecutive losses of -114/-113/-112 in Nov; USDCHF had 4.
MAX_CONSECUTIVE_LOSSES      = 2
LOSS_STREAK_PAUSE_HOURS     = 4

# ── Multi‑level Take Profit ────────────────────────────────────────
TIERED_TP_ENABLED        = True
TIERED_TP1_RATIO         = 1.5   # was 2.0 — losers avg MFE 13.8p, SL ~30-50p → TP1 at 2R unreachable; 1.5R more realistic
# Grade-adaptive TP1 partial-close percentages.
# High-conviction (A+) setups keep 60% running; counter-trend (C) exits 60% defensively.
TIERED_TP1_CLOSE_PCT          = 0.60   # grade C / counter-trend / fallback
TIERED_TP1_CLOSE_PCT_A_PLUS   = 0.40   # A+ grade: close 40%, keep 60% running
TIERED_TP1_CLOSE_PCT_B        = 0.50   # B grade: close 50%, keep 50% running
# TP2 lowered from 3.0 → 2.5.  With TP2 at 3R and MIN_RR=2 there is headroom;
# 2.5R keeps TP2 reachable while still locking meaningful additional profit.
TIERED_TP2_RATIO         = 2.5
TIERED_TP2_CLOSE_PCT     = 0.25
FINAL_RUNNER_PCT         = 0.15

# ── STALL EXIT RULE ───────────────────────────────────────────────
# False = exit on stall whenever profit ≥ MIN_PROFIT_RATIO_TO_CLOSE (1.5R),
# even if TP1 hasn't been hit yet.  249 stall events took no action because
# TP1 was not yet reached — those trades then reversed and hit SL.
STALL_EXIT_REQUIRES_TP1 = False

# ── TP1 PROFIT-LOCK ───────────────────────────────────────────────
# After TP1 hit, SL is moved to entry + (initial_risk × this multiplier).
# 0.3 = lock in 0.3R of profit before letting the runner continue.
TP1_PROFIT_LOCK_R = 0.3

# ── MONITOR TRAILING ──────────────────────────────────────────────
# ATR fraction used as buffer below/above swing pivots for SL trail.
MONITOR_TRAIL_ATR_BUFFER = 0.5

# ── TRADE QUALITY SCORING ─────────────────────────────────────────
TRADE_SCORE_THRESHOLD   = 2
SCORE_HTF_ALIGNMENT       = 2   # max contribution
SCORE_LIQUIDITY_SWEEP     = 1
SCORE_CLEAN_FVG           = 1
SCORE_STRONG_DISPLACEMENT = 1
SCORE_KILL_ZONE           = 0   # outside kill zones perform 2.2× better in backtest

# ── DYNAMIC POSITION SIZING ───────────────────────────────────────
RISK_HIGH_CONVICTION    = 1.5
RISK_MEDIUM_CONVICTION  = 1.0
RISK_LOW_CONVICTION     = 0.5

# ── TIME‑BASED EXIT ───────────────────────────────────────────────
MAX_TRADE_DURATION_MINUTES = 600

# ── SESSION FILTER (KILL ZONES) ───────────────────────────────────
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

# ── Retention Policies ────────────────────────────────────────────
LOG_RETENTION_DAYS   = 3
CACHE_RETENTION_DAYS = 2
ZONE_RETENTION_DAYS  = 5
ZONES_DIR            = "zones"

# ── Checkpoint ────────────────────────────────────────────────────
CHECKPOINT_DIR          = ".checkpoints"
CHECKPOINT_INTERVAL_MIN = 15       # save checkpoint every N minutes

# ── Dashboard ─────────────────────────────────────────────────────
DASHBOARD_ENABLED = True
DASHBOARD_PORT    = 8080

# ── Email / Weekly Backtest ───────────────────────────────────────
EMAIL_ENABLED   = False            # set True + fill fields below to activate
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", 587))
EMAIL_USER      = os.environ.get("EMAIL_USER",      "")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD",  "")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT", "")

WEEKLY_BACKTEST_ENABLED  = True
WEEKLY_BACKTEST_WEEKDAY  = 5      # Saturday
WEEKLY_BACKTEST_HOUR_UTC = 6      # 06:00 UTC

# ── Backtest ──────────────────────────────────────────────────────
NEWS_SLIPPAGE_PIPS   = 5.0
MAX_FORWARD_M1_BARS  = 4_320
# HFM retains up to ~5 months of M1 history via copy_rates_range, BUT only
# if you have first scrolled back on each symbol's M1 chart in the terminal
# (press Home repeatedly until the chart stops loading).  Without that step
# the terminal only serves its local cache (~6 weeks), explaining the 31.5%
# M1 coverage seen in the previous report.
#
# BEFORE running the backtest:
#   Open each symbol's M1 chart in MT5 and press Home until bars stop loading.
#
# After doing that, set this to 4 for a full and accurate simulation.
# Leave at 2 if you have not yet scrolled back (safe default).
BACKTEST_MONTHS_BACK = 6
BACKTEST_START_DATE  = ""
BACKTEST_END_DATE    = ""

# Keep False so the backtest matches live trading exactly.
# Live trading monitors on M1 (SL/TP/MFE precision); the backtest should too.
# MT5 retains M1 for ~60 days, so BACKTEST_MONTHS_BACK is set to 2 below —
# that keeps full M1 coverage for every simulated trade.
BACKTEST_USE_M5_ONLY = False