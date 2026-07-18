"""Regression test for src/application/strategy_v2/liquidity.py against the
test matrix: Asian range levels, previous-day levels, equal highs/lows,
buy/sell-side classification, sweep confirmation vs invalidation
(breakout), London-session classification, and multi-sweep handling.

HOW TO RUN
    python3 tools/validate_liquidity.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Candle, Timeframe
from src.domain.strategy_v2_entities import LiquidityLevel, LiquidityLevelType, SwingPoint, SwingType
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.liquidity import (
    is_buy_side, is_sell_side, equal_level_tolerance, min_sweep_penetration,
    is_asian_session, is_london_session, detect_asia_levels, detect_prev_day_levels,
    detect_equal_level, classify_sweep_reaction, update_sweep_state,
    swept_during_london, active_unswept_levels,
)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)  # a Thursday
def ts(hour_offset): return T0 + timedelta(hours=hour_offset)
def C(hour_offset, o, h, l, c, sym="EURUSD"):
    return Candle(time=ts(hour_offset), open=o, high=h, low=l, close=c, volume=100.0, symbol=sym, timeframe=Timeframe.H1)

print("="*78); print("SECTION 1 — BUY-SIDE / SELL-SIDE CLASSIFICATION"); print("="*78)

check("1a ASIA_HIGH is buy-side", is_buy_side(LiquidityLevelType.ASIA_HIGH))
check("1b EQUAL_HIGH is buy-side", is_buy_side(LiquidityLevelType.EQUAL_HIGH))
check("1c PREV_DAY_HIGH is buy-side", is_buy_side(LiquidityLevelType.PREV_DAY_HIGH))
check("1d ASIA_LOW is sell-side", is_sell_side(LiquidityLevelType.ASIA_LOW))
check("1e EQUAL_LOW is sell-side", is_sell_side(LiquidityLevelType.EQUAL_LOW))
check("1f PREV_DAY_LOW is sell-side", is_sell_side(LiquidityLevelType.PREV_DAY_LOW))
check("1g highs are never sell-side", not is_sell_side(LiquidityLevelType.ASIA_HIGH))
check("1h lows are never buy-side", not is_buy_side(LiquidityLevelType.ASIA_LOW))

print(); print("="*78); print("SECTION 2 — SESSION WINDOWS"); print("="*78)

check("2a hour 3 UTC is Asian session", is_asian_session(ts(3)))
check("2b hour 6 UTC is NOT Asian session (exclusive end)", not is_asian_session(ts(6)))
check("2c hour 0 UTC is Asian session (inclusive start)", is_asian_session(ts(0)))
check("2d hour 8 UTC is London session", is_london_session(ts(8)))
check("2e hour 6 UTC is NOT London session", not is_london_session(ts(6)))
check("2f hour 10 UTC is NOT London session (exclusive end)", not is_london_session(ts(10)))

print(); print("="*78); print("SECTION 3 — ASIAN RANGE LEVEL DETECTION"); print("="*78)

# Day 1: Asia session hours 0-5, then non-Asia hours 6-23. Day boundary at hour 24.
day1 = [C(h, 1.1000, 1.1000 + 0.0010*h, 1.1000 - 0.0005*h, 1.1000, "EURUSD") for h in range(0, 6)]
# Make hour 2 the day's Asia high (1.1050) and hour 4 the day's Asia low.
day1[2] = C(2, 1.1000, 1.1050, 1.0990, 1.1010, "EURUSD")
day1[4] = C(4, 1.1000, 1.1005, 1.0940, 1.0995, "EURUSD")
post_asia_day1 = [C(h, 1.1000, 1.1010, 1.0990, 1.1000, "EURUSD") for h in range(6, 24)]
candles_asia = day1 + post_asia_day1

asia_highs, asia_lows = detect_asia_levels(candles_asia, "EURUSD")
check("3a exactly one ASIA_HIGH emitted for the single day", len(asia_highs) == 1, f"got {len(asia_highs)}")
check("3b ASIA_HIGH price is the max high within the Asian session", asia_highs and abs(asia_highs[0].price - 1.1050) < 1e-9,
      f"price={asia_highs[0].price if asia_highs else None}")
check("3c ASIA_LOW price is the min low within the Asian session", asia_lows and abs(asia_lows[0].price - 1.0940) < 1e-9,
      f"price={asia_lows[0].price if asia_lows else None}")
check("3d ASIA_HIGH created_at is the FIRST non-Asian candle (hour 6), not mid-session (no look-ahead)",
      asia_highs and asia_highs[0].created_at == ts(6), f"created_at={asia_highs[0].created_at if asia_highs else None}")

print(); print("="*78); print("SECTION 4 — PREVIOUS-DAY LEVEL DETECTION"); print("="*78)

# Two full days of data: day 1 hours 0-23, day 2 hours 24-47.
day2 = [C(h, 1.1000, 1.1010, 1.0990, 1.1000, "EURUSD") for h in range(24, 48)]
candles_2day = candles_asia + day2  # candles_asia already spans hours 0-23 (one full day)
prev_highs, prev_lows = detect_prev_day_levels(candles_2day, "EURUSD")
check("4a no PREV_DAY level exists on day 1 itself (no completed prior day yet)",
      all(h.created_at is None or h.created_at >= ts(24) for h in prev_highs))
check("4b exactly one PREV_DAY_HIGH created at the first candle of day 2 (hour 24)",
      len(prev_highs) == 1 and prev_highs[0].created_at == ts(24), f"{prev_highs}")
check("4c PREV_DAY_HIGH price equals day 1's actual max high (1.1050)",
      abs(prev_highs[0].price - 1.1050) < 1e-9, f"price={prev_highs[0].price}")
check("4d PREV_DAY_LOW price equals day 1's actual min low (1.0940)",
      abs(prev_lows[0].price - 1.0940) < 1e-9, f"price={prev_lows[0].price}")

print(); print("="*78); print("SECTION 5 — EQUAL HIGHS / EQUAL LOWS"); print("="*78)

swing_log = []
def mk_swing(kind, price, idx, hour):
    s = SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=kind, price=price,
                   candle_time=ts(hour), confirmed_at_index=idx)
    swing_log.append(s)
    return s

tol = equal_level_tolerance("EURUSD")  # 3 pips = 0.0003
s1 = mk_swing(SwingType.HIGH, 1.1050, 10, 10)
s2 = mk_swing(SwingType.HIGH, 1.1052, 40, 40)  # within tolerance (0.0002 < 0.0003), within 72-bar default lookback
lvl = detect_equal_level("EURUSD", swing_log, s2, tol, lookback_bars=72)
check("5a two same-kind swings within tolerance -> EQUAL_HIGH detected", lvl is not None and lvl.kind == LiquidityLevelType.EQUAL_HIGH)
check("5b EQUAL_HIGH anchors at the MORE EXTREME price (1.1052, not an average)",
      lvl is not None and abs(lvl.price - 1.1052) < 1e-9, f"price={lvl.price if lvl else None}")
check("5c EQUAL_HIGH created_at is the NEWER swing's candle_time (no look-ahead)",
      lvl is not None and lvl.created_at == ts(40))

swing_log2 = []
s3 = mk_swing_2 = SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=SwingType.HIGH, price=1.1050,
                              candle_time=ts(10), confirmed_at_index=10)
swing_log2.append(s3)
s4 = SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=SwingType.HIGH, price=1.1080,  # 30 pips away, well outside tolerance
                candle_time=ts(40), confirmed_at_index=40)
lvl_none = detect_equal_level("EURUSD", swing_log2, s4, tol, lookback_bars=72)
check("5d swings outside tolerance: no equal level", lvl_none is None)

swing_log3 = []
s5 = SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=SwingType.HIGH, price=1.1050,
                 candle_time=ts(10), confirmed_at_index=10)
swing_log3.append(s5)
s6 = SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=SwingType.HIGH, price=1.1051,
                 candle_time=ts(200), confirmed_at_index=200)  # within tolerance but far outside 72-bar lookback
lvl_far = detect_equal_level("EURUSD", swing_log3, s6, tol, lookback_bars=72)
check("5e swings within tolerance but beyond lookback window: no equal level", lvl_far is None)

swing_log4 = []
sl1 = SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=SwingType.LOW, price=1.0950,
                  candle_time=ts(10), confirmed_at_index=10)
swing_log4.append(sl1)
sl2 = SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=SwingType.LOW, price=1.0948,
                  candle_time=ts(40), confirmed_at_index=40)
lvl_low = detect_equal_level("EURUSD", swing_log4, sl2, tol, lookback_bars=72)
check("5f EQUAL_LOW anchors at the more extreme (lowest) price (1.0948)",
      lvl_low is not None and lvl_low.kind == LiquidityLevelType.EQUAL_LOW and abs(lvl_low.price - 1.0948) < 1e-9,
      f"{lvl_low}")

print(); print("="*78); print("SECTION 6 — SWEEP CONFIRMATION (stop-hunt-then-reverse)"); print("="*78)

min_pen = min_sweep_penetration("EURUSD")  # 1 pip = 0.0001

buy_level = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050,
                            primary_timeframe=Timeframe.H1, created_at=ts(6))
reversal_candle = C(10, 1.1045, 1.1055, 1.1040, 1.1042, "EURUSD")  # wick above 1.1050 by 5 pips, closes back below
reaction = classify_sweep_reaction(buy_level, reversal_candle, min_pen)
check("6a wick penetrates buy-side level, closes back below: confirmed_reversal", reaction == "confirmed_reversal", f"got {reaction}")

sell_level = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0950,
                             primary_timeframe=Timeframe.H1, created_at=ts(6))
reversal_candle_sell = C(10, 1.0955, 1.0958, 1.0940, 1.0960, "EURUSD")  # wick below 1.0950 by 10 pips, closes back above
reaction_sell = classify_sweep_reaction(sell_level, reversal_candle_sell, min_pen)
check("6b wick penetrates sell-side level, closes back above: confirmed_reversal", reaction_sell == "confirmed_reversal", f"got {reaction_sell}")

print(); print("="*78); print("SECTION 7 — SWEEP INVALIDATION (genuine breakout, not a grab)"); print("="*78)

breakout_candle = C(10, 1.1045, 1.1060, 1.1044, 1.1058, "EURUSD")  # penetrates AND closes beyond too
reaction_bo = classify_sweep_reaction(buy_level, breakout_candle, min_pen)
check("7a body closes beyond the level too: breakout (not a valid liquidity grab)", reaction_bo == "breakout", f"got {reaction_bo}")

breakout_candle_sell = C(10, 1.0955, 1.0956, 1.0935, 1.0938, "EURUSD")  # penetrates below AND closes below too
reaction_bo_sell = classify_sweep_reaction(sell_level, breakout_candle_sell, min_pen)
check("7b bearish mirror: body closes beyond the level too: breakout", reaction_bo_sell == "breakout", f"got {reaction_bo_sell}")

print(); print("="*78); print("SECTION 8 — NEGATIVE / BOUNDARY CASES"); print("="*78)

no_touch = C(10, 1.1030, 1.1040, 1.1020, 1.1035, "EURUSD")  # never reaches 1.1050
check("8a no penetration at all: None", classify_sweep_reaction(buy_level, no_touch, min_pen) is None)

tiny_touch = C(10, 1.1045, 1.1050 + min_pen/2, 1.1040, 1.1042, "EURUSD")  # penetrates but below min_pen threshold
check("8b penetration below min_pen threshold: None (not a qualifying sweep)",
      classify_sweep_reaction(buy_level, tiny_touch, min_pen) is None)

exact_touch = C(10, 1.1045, 1.1050 + min_pen, 1.1040, 1.1042, "EURUSD")  # exactly at min_pen (inclusive boundary)
check("8c penetration exactly == min_pen: confirmed_reversal (inclusive boundary via approx_gte)",
      classify_sweep_reaction(buy_level, exact_touch, min_pen) == "confirmed_reversal")

print(); print("="*78); print("SECTION 9 — update_sweep_state: ONE-WAY FLAG, RECORDED FIELDS"); print("="*78)

level_u = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050,
                          primary_timeframe=Timeframe.H1, created_at=ts(6))
update_sweep_state(level_u, no_touch, min_pen)
check("9a no qualifying candle: swept stays False", level_u.swept is False)
update_sweep_state(level_u, reversal_candle, min_pen)
check("9b qualifying candle: swept becomes True", level_u.swept is True)
check("9c swept_at recorded correctly", level_u.swept_at == ts(10))
check("9d sweep_penetration recorded correctly (0.0005)", abs(level_u.sweep_penetration - 0.0005) < 1e-9, f"{level_u.sweep_penetration}")
check("9e reaction recorded as confirmed_reversal", level_u.reaction == "confirmed_reversal")

# One-way: a subsequent, different-looking candle must not change anything.
later_breakout = C(11, 1.1042, 1.1070, 1.1041, 1.1065, "EURUSD")
update_sweep_state(level_u, later_breakout, min_pen)
check("9f one-way flag: swept_at/reaction unchanged by a later candle", level_u.swept_at == ts(10) and level_u.reaction == "confirmed_reversal")

print(); print("="*78); print("SECTION 10 — LONDON-SESSION SWEEP CLASSIFICATION"); print("="*78)

level_london = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050,
                               primary_timeframe=Timeframe.H1, created_at=ts(6))
london_sweep_candle = C(8, 1.1045, 1.1060, 1.1040, 1.1042, "EURUSD")  # hour 8 = London session
update_sweep_state(level_london, london_sweep_candle, min_pen)
check("10a sweep during London session hour: swept_during_london is True", swept_during_london(level_london))

level_non_london = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050,
                                   primary_timeframe=Timeframe.H1, created_at=ts(6))
ny_sweep_candle = C(14, 1.1045, 1.1060, 1.1040, 1.1042, "EURUSD")  # hour 14 = NY session, not London
update_sweep_state(level_non_london, ny_sweep_candle, min_pen)
check("10b sweep outside London session: swept_during_london is False", not swept_during_london(level_non_london))

check("10c unswept level: swept_during_london is False regardless of hour",
      not swept_during_london(LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050,
                                              primary_timeframe=Timeframe.H1, created_at=ts(8))))

print(); print("="*78); print("SECTION 11 — MULTI-SWEEP HANDLING (many concurrent levels)"); print("="*78)

pool = [
    LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050, primary_timeframe=Timeframe.H1, created_at=ts(6)),
    LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0950, primary_timeframe=Timeframe.H1, created_at=ts(6)),
    LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.EQUAL_HIGH, price=1.1052, primary_timeframe=Timeframe.H1, created_at=ts(40)),
    LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.PREV_DAY_HIGH, price=1.1060, primary_timeframe=Timeframe.H1, created_at=ts(24)),
]
# Sweep only the first two independently, at different times.
update_sweep_state(pool[0], reversal_candle, min_pen)   # ts(10)
update_sweep_state(pool[1], reversal_candle_sell, min_pen)  # ts(10)
check("11a two independently-tracked levels both swept without affecting the others",
      pool[0].swept and pool[1].swept and not pool[2].swept and not pool[3].swept)

active_at_20 = active_unswept_levels(pool, ts(20))
check("11b active_unswept_levels at hour 20: empty (both unswept levels -- EQUAL_HIGH@ts40, PREV_DAY_HIGH@ts24 -- "
      "aren't created yet; the two created@ts6 levels are already swept)",
      active_at_20 == [], f"{[lv.kind for lv in active_at_20]}")

active_at_50 = active_unswept_levels(pool, ts(50))
check("11c active_unswept_levels at hour 50: both EQUAL_HIGH and PREV_DAY_HIGH now exist and are unswept",
      set(lv.kind for lv in active_at_50) == {LiquidityLevelType.EQUAL_HIGH, LiquidityLevelType.PREV_DAY_HIGH},
      f"{[lv.kind for lv in active_at_50]}")

print(); print("="*78); print("SECTION 12 — PER-INSTRUMENT SIZING (XAUUSD tolerance/penetration)"); print("="*78)

check("12a XAUUSD equal-level tolerance = 10 pips = $1.00", abs(equal_level_tolerance("XAUUSD") - 1.00) < 1e-9,
      f"got {equal_level_tolerance('XAUUSD')}")
check("12b XAUUSD min sweep penetration = 1 pip = $0.10", abs(min_sweep_penetration("XAUUSD") - 0.10) < 1e-9,
      f"got {min_sweep_penetration('XAUUSD')}")
check("12c EURUSD equal-level tolerance = 3 pips = 0.0003", abs(equal_level_tolerance("EURUSD") - 0.0003) < 1e-9)
check("12d EURUSD min sweep penetration = 1 pip = 0.0001", abs(min_sweep_penetration("EURUSD") - 0.0001) < 1e-9)

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
