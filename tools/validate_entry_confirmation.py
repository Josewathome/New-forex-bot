"""Regression test for src/application/strategy_v2/entry_confirmation.py
against the test matrix: sweep confirmation (recency + reaction type),
POI touch, discount/premium validation, local structure confirmation,
entry-candle confirmation (displacement + body-close break), SL/TP
computation, RR filter, and full evaluate_entry() orchestration
(positive path + one failure per gate).

HOW TO RUN
    python3 tools/validate_entry_confirmation.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Candle, Direction, Timeframe
from src.domain.strategy_v2_entities import (
    ExternalSwingPair, LiquidityLevel, LiquidityLevelType, SwingPoint, SwingType, TrendState,
)
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.entry_confirmation import (
    EntrySignal, sweep_confirmed, poi_touched, poi_in_favorable_zone, local_structure_confirms,
    entry_candle_confirmed, compute_stop_loss, compute_take_profit, compute_rr, evaluate_entry,
)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
def ts(minutes): return T0 + timedelta(minutes=minutes)
def C(minutes, o, h, l, c, sym="EURUSD"):
    return Candle(time=ts(minutes), open=o, high=h, low=l, close=c, volume=10.0, symbol=sym, timeframe=Timeframe.M1)

def mk_swing(kind, price, idx=0, minute=0):
    return SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=kind, price=price, candle_time=ts(minute), confirmed_at_index=idx)

def mk_pair(low, high, trend):
    return ExternalSwingPair(symbol="EURUSD", trend=trend, low=low, high=high,
                              low_swing=mk_swing(SwingType.LOW, low), high_swing=mk_swing(SwingType.HIGH, high), bos_time=T0)

print("="*78); print("SECTION 1 — SWEEP CONFIRMATION (E1)"); print("="*78)

level_reversal = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0950,
                                 primary_timeframe=Timeframe.H1, created_at=ts(0),
                                 swept=True, swept_at=ts(60), reaction="confirmed_reversal")
check("1a confirmed_reversal, within max age: True", sweep_confirmed(level_reversal, ts(120), timedelta(minutes=90)))
check("1b confirmed_reversal, beyond max age: False", not sweep_confirmed(level_reversal, ts(300), timedelta(minutes=90)))

level_breakout = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0950,
                                 primary_timeframe=Timeframe.H1, created_at=ts(0),
                                 swept=True, swept_at=ts(60), reaction="breakout")
check("1c breakout reaction (never a valid trigger): False", not sweep_confirmed(level_breakout, ts(120), timedelta(minutes=90)))

level_unswept = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0950,
                                primary_timeframe=Timeframe.H1, created_at=ts(0))
check("1d unswept level: False", not sweep_confirmed(level_unswept, ts(120), timedelta(minutes=90)))

print(); print("="*78); print("SECTION 2 — POI TOUCH (E2)"); print("="*78)

zone_low, zone_high = 1.1000, 1.1010
touch_candle = C(0, 1.1015, 1.1018, 1.1005, 1.1012)  # low(1.1005) inside zone
check("2a candle wick reaches into the zone: touched", poi_touched(touch_candle, zone_low, zone_high))
exact_boundary = C(0, 1.1015, 1.1018, 1.1010, 1.1012)  # low exactly == zone_high
check("2b candle low exactly at zone_high: touched (inclusive)", poi_touched(exact_boundary, zone_low, zone_high))
no_touch = C(0, 1.1020, 1.1025, 1.1015, 1.1022)  # never reaches down to 1.1010
check("2c candle entirely above the zone: not touched", not poi_touched(no_touch, zone_low, zone_high))

print(); print("="*78); print("SECTION 3 — DISCOUNT/PREMIUM VALIDATION (E3, delegated)"); print("="*78)

pair_bull = mk_pair(1.1000, 1.1100, TrendState.BULLISH)
check("3a BULLISH pair, zone in discount half: favorable", poi_in_favorable_zone(pair_bull, 1.1005, 1.1015))
check("3b BULLISH pair, zone in premium half: NOT favorable", not poi_in_favorable_zone(pair_bull, 1.1080, 1.1095))

print(); print("="*78); print("SECTION 4 — LOCAL STRUCTURE CONFIRMATION (E4)"); print("="*78)

check("4a BULLISH direction, local trend BULLISH: confirmed", local_structure_confirms(Direction.BULLISH, TrendState.BULLISH))
check("4b BULLISH direction, local trend BEARISH: not confirmed", not local_structure_confirms(Direction.BULLISH, TrendState.BEARISH))
check("4c BULLISH direction, local trend NEUTRAL: not confirmed", not local_structure_confirms(Direction.BULLISH, TrendState.NEUTRAL))

print(); print("="*78); print("SECTION 5 — ENTRY CANDLE CONFIRMATION (E5)"); print("="*78)

atr_val = 0.0004
strong_bull_candle = C(0, 1.1000, 1.1012, 1.0999, 1.1010)  # body=0.0010, 2.5x ATR; closes above swing(1.1005)
check("5a strong bullish displacement candle closing above local swing: confirmed",
      entry_candle_confirmed(strong_bull_candle, 1.1005, Direction.BULLISH, atr_val))
weak_candle = C(0, 1.1000, 1.1006, 1.0999, 1.1002)  # body=0.0002, well under 1.5x ATR
check("5b weak (non-displacement) candle: not confirmed", not entry_candle_confirmed(weak_candle, 1.1000, Direction.BULLISH, atr_val))
no_break_candle = C(0, 1.1000, 1.1012, 1.0999, 1.1002)  # displacement but closes BELOW the local swing(1.1005)
check("5c displacement candle that does NOT break the local swing: not confirmed",
      not entry_candle_confirmed(no_break_candle, 1.1005, Direction.BULLISH, atr_val))
strong_bear_candle = C(0, 1.1010, 1.1011, 1.0998, 1.1000)  # body=0.0010, closes below swing(1.1005)
check("5d bearish mirror: strong displacement, closes below local swing: confirmed",
      entry_candle_confirmed(strong_bear_candle, 1.1005, Direction.BEARISH, atr_val))
check("5e atr_value=None: never confirmed (never silently passes displacement)",
      not entry_candle_confirmed(strong_bull_candle, 1.1005, Direction.BULLISH, None))

print(); print("="*78); print("SECTION 6 — STOP LOSS PLACEMENT (E6)"); print("="*78)

sl_bull = compute_stop_loss(Direction.BULLISH, 1.1000, 1.1010, "EURUSD")
check("6a BULLISH SL = zone_low - buffer (2 pips = 0.0002)", abs(sl_bull - (1.1000 - 0.0002)) < 1e-9, f"{sl_bull}")
sl_bear = compute_stop_loss(Direction.BEARISH, 1.1000, 1.1010, "EURUSD")
check("6b BEARISH SL = zone_high + buffer", abs(sl_bear - (1.1010 + 0.0002)) < 1e-9, f"{sl_bear}")
sl_xau = compute_stop_loss(Direction.BULLISH, 4000.00, 4005.00, "XAUUSD")
check("6c XAUUSD SL uses the wider 3-pip buffer ($0.30)", abs(sl_xau - (4000.00 - 0.30)) < 1e-9, f"{sl_xau}")

print(); print("="*78); print("SECTION 7 — TAKE PROFIT CALCULATION (E7)"); print("="*78)

pair_tp = mk_pair(1.0900, 1.1100, TrendState.BULLISH)  # range_hi=1.1100
entry_price = 1.1010
asia_high_far = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1200,
                                primary_timeframe=Timeframe.H1, created_at=ts(0))
tp_htf_nearer = compute_take_profit(Direction.BULLISH, entry_price, pair_tp, asia_high_far)
check("7a HTF pair extreme (1.1100) nearer than a far Asia high (1.1200): picks HTF target",
      abs(tp_htf_nearer - 1.1100) < 1e-9, f"{tp_htf_nearer}")

asia_high_near = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050,
                                 primary_timeframe=Timeframe.H1, created_at=ts(0))
tp_asia_nearer = compute_take_profit(Direction.BULLISH, entry_price, pair_tp, asia_high_near)
check("7b Asia high (1.1050) nearer than HTF pair extreme (1.1100): picks Asia target",
      abs(tp_asia_nearer - 1.1050) < 1e-9, f"{tp_asia_nearer}")

tp_no_asia = compute_take_profit(Direction.BULLISH, entry_price, pair_tp, None)
check("7c no Asia level available: falls back to HTF target only", abs(tp_no_asia - 1.1100) < 1e-9)

pair_tp_bear = mk_pair(1.0900, 1.1100, TrendState.BEARISH)
tp_bear = compute_take_profit(Direction.BEARISH, 1.0990, pair_tp_bear, None)
check("7d BEARISH direction targets the range LOW, not the high", abs(tp_bear - 1.0900) < 1e-9, f"{tp_bear}")

print(); print("="*78); print("SECTION 8 — RR FILTER (E8)"); print("="*78)

check("8a RR computed correctly", abs(compute_rr(1.1010, 1.1000, 1.1060) - 5.0) < 1e-9)
check("8b zero-risk (entry==SL) returns 0.0 rather than raising", compute_rr(1.1000, 1.1000, 1.1100) == 0.0)

print(); print("="*78); print("SECTION 9 — FULL evaluate_entry() ORCHESTRATION"); print("="*78)

def base_args(**overrides):
    # pair equilibrium = (1.0900+1.1200)/2 = 1.1050 -- the zone (1.0995,
    # 1.1005), midpoint 1.1000, is clearly in the discount half (< 1.1050).
    args = dict(
        symbol="EURUSD",
        direction=Direction.BULLISH,
        pair=mk_pair(1.0900, 1.1200, TrendState.BULLISH),
        sweep_level=LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0995,
                                    primary_timeframe=Timeframe.H1, created_at=ts(0),
                                    swept=True, swept_at=ts(50), reaction="confirmed_reversal"),
        poi_kind="ob",
        zone_low=1.0995, zone_high=1.1005,
        entry_candle=C(60, 1.0998, 1.1012, 1.0997, 1.1010),  # body=0.0012, touches zone, breaks swing, in discount
        local_trend=TrendState.BULLISH,
        local_swing_price=1.1005,
        m1_atr_at_prev=0.0004,
        # nearer than the HTF target (1.1200), still leaves RR well above MIN_RR
        opposite_asia_level=LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1120,
                                            primary_timeframe=Timeframe.H1, created_at=ts(0)),
    )
    args.update(overrides)
    return args

signal = evaluate_entry(**base_args())
check("9a happy path: produces a valid EntrySignal", signal is not None and isinstance(signal, EntrySignal))
if signal:
    check("9b entry_price == candle close", abs(signal.entry_price - 1.1010) < 1e-9)
    check("9c stop_loss below the zone (bullish)", signal.stop_loss < 1.0995)
    check("9d rr >= MIN_RR (5.0)", signal.rr >= config_v2.MIN_RR, f"rr={signal.rr}")

# One failure per gate:
stale_sweep = base_args()
stale_sweep["sweep_level"].swept_at = ts(0)  # far in the past relative to entry_candle at ts(60), default max_age=6h so still fine -- force truly stale:
stale_args = base_args()
stale_args["sweep_level"] = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0995,
                                            primary_timeframe=Timeframe.H1, created_at=ts(0),
                                            swept=True, swept_at=ts(0) - timedelta(hours=10), reaction="confirmed_reversal")
check("9e stale sweep: rejected (None)", evaluate_entry(**stale_args) is None)

breakout_args = base_args()
breakout_args["sweep_level"] = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0995,
                                               primary_timeframe=Timeframe.H1, created_at=ts(0),
                                               swept=True, swept_at=ts(50), reaction="breakout")
check("9f breakout (not a reversal) sweep: rejected (None)", evaluate_entry(**breakout_args) is None)

no_touch_args = base_args(entry_candle=C(60, 1.1030, 1.1035, 1.1025, 1.1032))  # never reaches the zone
check("9g no POI touch: rejected (None)", evaluate_entry(**no_touch_args) is None)

premium_zone_args = base_args(zone_low=1.1080, zone_high=1.1090)  # in premium half, not discount, for a BULLISH pair
premium_zone_args["entry_candle"] = C(60, 1.1082, 1.1095, 1.1078, 1.1092)
check("9h POI in premium half (BULLISH pair wants discount): rejected (None)", evaluate_entry(**premium_zone_args) is None)

wrong_trend_args = base_args(local_trend=TrendState.BEARISH)
check("9i local structure not confirming direction: rejected (None)", evaluate_entry(**wrong_trend_args) is None)

weak_entry_args = base_args(entry_candle=C(60, 1.0998, 1.1002, 1.0997, 1.1000))  # touches zone but no displacement/no break
check("9j weak entry candle (no displacement / no swing break): rejected (None)", evaluate_entry(**weak_entry_args) is None)

low_rr_args = base_args(
    # equilibrium=(1.0900+1.1013)/2=1.10065, still above the zone's midpoint
    # (1.1000) so E1-E5 all still pass -- but the HTF target (1.1013) sits
    # right next to entry (1.1010), forcing RR well below MIN_RR.
    pair=mk_pair(1.0900, 1.1013, TrendState.BULLISH),
    opposite_asia_level=None,
)
check("9k RR below MIN_RR: rejected (None)", evaluate_entry(**low_rr_args) is None)

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
