# src/application/strategy_v2/config_v2.py
#
# Strategy-specific constants for the V4 SMC engine. Framework config
# (credentials, symbols-to-trade at the execution layer, position-cap
# enforcement, margin safety, dashboard/journal/checkpoint settings) stays
# in src/config.py, untouched. This module holds only the numeric rules
# specific to this strategy's signal logic, so the two can't collide.

from __future__ import annotations

# Instrument universe for this strategy (V4 spec Part 1).
SYMBOLS_V2 = ["XAUUSD", "GBPUSD", "EURUSD", "USDJPY", "AUDUSD"]

# ── Instrument unit conventions — canonical, single source (Contradiction 3) ──
# "No alternative Gold unit conventions may exist anywhere else in the spec."
# Confirmed against this account's actual MT5 symbol_info during Phase 1.5:
# XAUUSD digits=2/point=0.01, EURUSD/GBPUSD digits=5/point=0.00001,
# USDJPY digits=3/point=0.001 — all consistent with the table below.
PIP = {
    "XAUUSD": 0.10,
    "USDJPY": 0.01,
}
PIP_DEFAULT = 0.0001   # standard forex: GBPUSD, EURUSD, AUDUSD

POINT = {
    "XAUUSD": 0.01,
    "USDJPY": 0.001,
}
POINT_DEFAULT = 0.00001


def pip_size(symbol: str) -> float:
    return PIP.get(symbol, PIP_DEFAULT)


def point_size(symbol: str) -> float:
    return POINT.get(symbol, POINT_DEFAULT)


# ── Swing confirmation (Contradiction 1, locked) ──
SWING_CONFIRM_CANDLES = 3

# ── Displacement (§7) ──
ATR_PERIOD = 10
DISPLACEMENT_MULTIPLIER_HTF = 2.0   # H1 / M15 FVG & OB displacement
DISPLACEMENT_MULTIPLIER_M1  = 1.5   # M1 confirmation displacement

# ── Fair Value Gap (§6) ──
FVG_MIN_SIZE_PIPS = 5.0   # 5 pips forex/JPY = 0.05 JPY; 5 pips XAUUSD = 50 points = $0.50

# ── Order Block (§8, OB Fallback Rule) ──
OB_MAX_SEARCH_CANDLES = 20

# ── Stop Loss buffer (§18) ──
SL_BUFFER_PIPS = 2.0
SL_BUFFER_PIPS_XAUUSD = 3.0   # 30 points = 3 pips

# ── Liquidity (§11-12) ──
EQUAL_LEVEL_TOLERANCE_PIPS = 3.0
EQUAL_LEVEL_TOLERANCE_PIPS_XAUUSD = 10.0   # 100 points = $1.00
EQUAL_LEVEL_LOOKBACK_DAYS = 3
SWEEP_MIN_PENETRATION_PIPS = 1.0            # 1 pip forex/JPY; 10 points = 1 pip XAUUSD

# ── Sessions (§13) — fixed UTC, no DST ──
ASIAN_SESSION_UTC   = (0, 6)
LONDON_SESSION_UTC  = (7, 10)
NEWYORK_SESSION_UTC = (13, 16)

# ── Contraction exit (§21) ──
CONTRACTION_TOUCH_TOLERANCE_PIPS = 1.0
CONTRACTION_MIN_TOUCHES = 3

# ── Risk / RR (§16, §19, §23-24) ──
MIN_RR = 5.0
RISK_DEFAULT_PCT = 1.0
RISK_SCALED_PCT  = 2.0
MAX_CONCURRENT_TRADES = 3
MAX_TRADES_PER_SYMBOL = 1
MAX_PORTFOLIO_EXPOSURE_PCT = 6.0
DAILY_LOSS_LIMIT_PCT = 5.0
MAX_DRAWDOWN_PCT = 10.0

# ── Trade management R-multiples (§20) ──
BREAKEVEN_R = 2.0
TP1_R = 3.0
TP1_CLOSE_PCT = 0.25
TP2_R = 4.0
TP2_CLOSE_PCT = 0.25
RUNNER_PCT = 0.50
RUNNER_BE_BUFFER_PIPS = 0.5

# ── Re-entry (§22) ──
MAX_REENTRIES = 1

# ── Correlated exposure (Risk Management module) ──
# Max simultaneous open trades sharing a common currency leg (e.g. EURUSD +
# GBPUSD both carry USD exposure) -- not in the original numbered spec;
# a conservative, documented risk-management decision (see risk_management.py).
MAX_TRADES_PER_CURRENCY = 2

# ── Trade Management additions (beyond the locked R-multiple table above) ──
# Not present in the original numbered spec material available to this
# module -- conservative, documented decisions (see trade_management.py).
RUNNER_TRAIL_ATR_MULTIPLIER = 1.5   # H1 ATR multiple used to trail the runner portion once past TP2
MAX_TRADE_DURATION_HOURS = 72.0     # time-stop: close if still open this long (3 days, H1/M1 swing strategy)
EMERGENCY_MAX_ADVERSE_R = 3.0       # close-at-market if price moves this many multiples of initial risk against the position
