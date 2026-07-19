"""Regression test for src/application/strategy_v2/entry_confirmation.py
(CORRECTED per docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md) against
the test matrix: sweep confirmation, POI touch, discount/premium
validation, Absorption (spec §15), the STATELESS M1 CHoCH trigger (spec
§16, no StructureEngine reuse), Entry Zone computation (spec §17,
OB+FVG intersection or deeper-structure fallback), SL from the
confirmation swing (spec §18), TP priority-fallback (spec §19), and full
evaluate_entry() orchestration (positive path + one failure per gate).

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
    EntrySignal, sweep_confirmed, poi_touched, poi_in_favorable_zone, zone_touch_confirmed, absorption_confirmed,
    most_recent_swing, m1_choch_trigger, m1_confirmation, compute_entry_zone, compute_stop_loss,
    compute_rr, select_take_profit, evaluate_entry,
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

def mk_h1_swing(kind, price, idx=0, minute=0):
    return SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=kind, price=price, candle_time=ts(minute), confirmed_at_index=idx)

def mk_m1_swing(kind, price, idx=0, minute=0):
    return SwingPoint(symbol="EURUSD", timeframe=Timeframe.M1, kind=kind, price=price, candle_time=ts(minute), confirmed_at_index=idx)

def mk_pair(low, high, trend):
    return ExternalSwingPair(symbol="EURUSD", trend=trend, low=low, high=high,
                              low_swing=mk_h1_swing(SwingType.LOW, low), high_swing=mk_h1_swing(SwingType.HIGH, high), bos_time=T0)

print("="*78); print("SECTION 1 — SWEEP CONFIRMATION (E1, unchanged)"); print("="*78)

level_reversal = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0950,
                                 primary_timeframe=Timeframe.H1, created_at=ts(0),
                                 swept=True, swept_at=ts(60), reaction="confirmed_reversal")
check("1a confirmed_reversal, within max age: True", sweep_confirmed(level_reversal, ts(120), timedelta(minutes=90)))
check("1b confirmed_reversal, beyond max age: False", not sweep_confirmed(level_reversal, ts(300), timedelta(minutes=90)))
level_breakout = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0950,
                                 primary_timeframe=Timeframe.H1, created_at=ts(0),
                                 swept=True, swept_at=ts(60), reaction="breakout")
check("1c breakout reaction: False", not sweep_confirmed(level_breakout, ts(120), timedelta(minutes=90)))

print(); print("="*78); print("SECTION 2 — POI TOUCH (E2, revised: decoupled from same-bar confirmation) / SECTION 3 — FAVORABLE ZONE (E3, unchanged)"); print("="*78)
print("CORRECTED per docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md's real-data")
print("follow-up: the POI touch and M1 confirmation no longer need to happen on")
print("the exact same candle -- zone_touch_confirmed() checks a bounded recency")
print("window instead, mirroring sweep_confirmed()'s own pattern.")

zone_low, zone_high = 1.1000, 1.1010
touch_candle = C(0, 1.1015, 1.1018, 1.1005, 1.1012)
check("2a candle wick reaches into the zone: touched", poi_touched(touch_candle, zone_low, zone_high))
no_touch = C(0, 1.1020, 1.1025, 1.1015, 1.1022)
check("2b candle entirely above the zone: not touched", not poi_touched(no_touch, zone_low, zone_high))

check("2c touch within max age: confirmed", zone_touch_confirmed(ts(0), ts(60), timedelta(minutes=90)))
check("2d touch beyond max age: not confirmed", not zone_touch_confirmed(ts(0), ts(300), timedelta(minutes=90)))
check("2e never touched (None): not confirmed", not zone_touch_confirmed(None, ts(60), timedelta(minutes=90)))
check("2f touch time in the future relative to now: not confirmed (no look-ahead)",
      not zone_touch_confirmed(ts(100), ts(60), timedelta(minutes=90)))

pair_bull_wide = mk_pair(1.0900, 1.1200, TrendState.BULLISH)  # equilibrium=1.1050
check("3a BULLISH pair, zone in discount half: favorable", poi_in_favorable_zone(pair_bull_wide, 1.0995, 1.1005))
check("3b BULLISH pair, zone in premium half: NOT favorable", not poi_in_favorable_zone(pair_bull_wide, 1.1080, 1.1095))

print(); print("="*78); print("SECTION 4 — ABSORPTION (E4, spec §15)"); print("="*78)
print("CORRECTED per docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md's real-data")
print("follow-up: 'beyond the HTF POI' means overshooting the FAR side in the")
print("direction of travel, NOT simply being outside the zone on the approach")
print("side (which is the normal, expected state before the actual touch).")

atr_val = 0.0010
htf_lo, htf_hi = 1.0990, 1.1010

# strictly decreasing bodies, avg < 50%*ATR, no close beyond HTF POI
c1 = C(0, 1.1000, 1.1006, 1.0997, 1.1003)   # body 0.0003
c2 = C(1, 1.1003, 1.1006, 1.1000, 1.1004)   # body 0.0001
c3 = C(2, 1.1004, 1.1005, 1.1002, 1.10045)  # body 0.00005
check("4a strictly decreasing bodies, small avg, closes within HTF POI: confirmed",
      absorption_confirmed(Direction.BULLISH, [c1, c2, c3], atr_val, htf_lo, htf_hi))

c1_tie = C(0, 1.1000, 1.1005, 1.0998, 1.1002)   # body 0.0002
c2_tie = C(1, 1.1002, 1.1006, 1.1000, 1.1004)   # body 0.0002 (tie -- disqualifies)
c3_tie = C(2, 1.1004, 1.1005, 1.1002, 1.10045)
check("4b tied consecutive bodies (not STRICTLY decreasing): rejected",
      not absorption_confirmed(Direction.BULLISH, [c1_tie, c2_tie, c3_tie], atr_val, htf_lo, htf_hi))

c_big = C(0, 1.1000, 1.1050, 1.0950, 1.1040)  # huge body, avg would exceed 50%*ATR
check("4c large average body (>=50%*ATR): rejected",
      not absorption_confirmed(Direction.BULLISH, [c_big, c2, c3], atr_val, htf_lo, htf_hi))

# KEY REGRESSION TEST: BULLISH setup, all 3 candles still APPROACHING from
# ABOVE the zone (close > htf_hi) -- the normal, expected pre-touch state.
# Must be CONFIRMED, not rejected (this is exactly what the two-sided bug
# got wrong and made Absorption fail on ~100% of real approach sequences).
c1_above = C(0, 1.1020, 1.1026, 1.1017, 1.1023)   # body 0.0003, closes above htf_hi
c2_above = C(1, 1.1023, 1.1026, 1.1020, 1.1024)   # body 0.0001
c3_above = C(2, 1.1024, 1.1025, 1.1022, 1.10245)  # body 0.00005
check("4d BULLISH, approaching from ABOVE the zone (not yet touched): CONFIRMED",
      absorption_confirmed(Direction.BULLISH, [c1_above, c2_above, c3_above], atr_val, htf_lo, htf_hi))

c_undershoot = C(0, 1.1000, 1.1006, 1.0997, 1.0985)  # closes BELOW htf_lo=1.0990 -- overshoot past support
check("4e BULLISH, a candle closing BELOW the zone's far side (undershoot): rejected",
      not absorption_confirmed(Direction.BULLISH, [c_undershoot, c2, c3], atr_val, htf_lo, htf_hi))

# BEARISH mirror: approaching from BELOW is fine; overshooting ABOVE htf_hi is not.
c1_below = C(0, 1.0970, 1.0976, 1.0967, 1.0973)
c2_below = C(1, 1.0973, 1.0976, 1.0970, 1.0974)
c3_below = C(2, 1.0974, 1.0975, 1.0972, 1.09745)
check("4f BEARISH, approaching from BELOW the zone (not yet touched): CONFIRMED",
      absorption_confirmed(Direction.BEARISH, [c1_below, c2_below, c3_below], atr_val, htf_lo, htf_hi))

c_overshoot = C(0, 1.1000, 1.1025, 1.0997, 1.1015)  # closes ABOVE htf_hi=1.1010 -- overshoot past resistance
check("4g BEARISH, a candle closing ABOVE the zone's far side (overshoot): rejected",
      not absorption_confirmed(Direction.BEARISH, [c_overshoot, c2, c3], atr_val, htf_lo, htf_hi))

check("4h wrong candle count (not exactly 3): rejected", not absorption_confirmed(Direction.BULLISH, [c1, c2], atr_val, htf_lo, htf_hi))
check("4i atr_value None: rejected (never silently passes)", not absorption_confirmed(Direction.BULLISH, [c1, c2, c3], None, htf_lo, htf_hi))

print(); print("="*78); print("SECTION 5 — STATELESS M1 CHoCH TRIGGER (E5, spec §16, NEW MECHANISM)"); print("="*78)

m1_swings = [mk_m1_swing(SwingType.HIGH, 1.1005, idx=10, minute=10), mk_m1_swing(SwingType.LOW, 1.0995, idx=15, minute=15)]
check("5a most_recent_swing finds the most recent HIGH", most_recent_swing(m1_swings, SwingType.HIGH).price == 1.1005)
check("5b most_recent_swing finds the most recent LOW", most_recent_swing(m1_swings, SwingType.LOW).price == 0.0995 or most_recent_swing(m1_swings, SwingType.LOW).price == 1.0995)
check("5c most_recent_swing returns None if no swing of that kind exists", most_recent_swing([], SwingType.HIGH) is None)

break_candle = C(20, 1.1000, 1.1010, 1.0998, 1.1008)  # closes above swing HIGH 1.1005
trigger = m1_choch_trigger(Direction.BULLISH, m1_swings, break_candle)
check("5d bullish trigger: body close above the most recent M1 swing HIGH fires, returns that swing",
      trigger is not None and trigger.price == 1.1005)

no_break_candle = C(20, 1.1000, 1.1004, 1.0998, 1.1002)  # closes below 1.1005
check("5e no body close beyond the swing: no trigger", m1_choch_trigger(Direction.BULLISH, m1_swings, no_break_candle) is None)

bear_break_candle = C(20, 1.0998, 1.1000, 1.0990, 1.0992)  # closes below swing LOW 1.0995
trigger_bear = m1_choch_trigger(Direction.BEARISH, m1_swings, bear_break_candle)
check("5f bearish trigger: body close below the most recent M1 swing LOW fires",
      trigger_bear is not None and trigger_bear.price == 1.0995)

check("5g no swing of the needed kind at all: no trigger",
      m1_choch_trigger(Direction.BULLISH, [mk_m1_swing(SwingType.LOW, 1.0995)], break_candle) is None)

# Combined m1_confirmation (Absorption + trigger + displacement)
strong_break_candle = C(20, 1.0993, 1.1012, 1.0992, 1.1010)  # body=0.0017 >= 1.5*0.0010 ATR needed, closes above 1.1005
combined = m1_confirmation(Direction.BULLISH, m1_swings, strong_break_candle, atr_val, [c1, c2, c3], htf_lo, htf_hi)
check("5h m1_confirmation: all three conditions pass together -> returns the confirmation swing",
      combined is not None and combined.price == 1.1005)
weak_displacement_candle = C(20, 1.0998, 1.1006, 1.0997, 1.1006)  # closes above swing but body too small for 1.5x ATR
check("5i m1_confirmation: swing broken but displacement insufficient -> None",
      m1_confirmation(Direction.BULLISH, m1_swings, weak_displacement_candle, atr_val, [c1, c2, c3], htf_lo, htf_hi) is None)
check("5j m1_confirmation: Absorption fails (tied bodies) even with a valid break+displacement -> None",
      m1_confirmation(Direction.BULLISH, m1_swings, strong_break_candle, atr_val, [c1_tie, c2_tie, c3_tie], htf_lo, htf_hi) is None)

print(); print("="*78); print("SECTION 6 — ENTRY ZONE (E6, spec §17)"); print("="*78)
print("CORRECTED per docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md's real-data")
print("follow-up: candidates are now full lists (low, high, created_at); the")
print("function itself selects the zone CLOSEST IN PRICE to the confirmation")
print("swing (ties broken by most-recently-created) -- an ancient, still-")
print("\"active\"-but-far-away zone can no longer be picked just because it's")
print("the most recently created one that hasn't mitigated yet.")

pair_for_zone = mk_pair(1.0900, 1.1200, TrendState.BULLISH)  # equilibrium=1.1050
conf_swing_for_zone = mk_m1_swing(SwingType.HIGH, 1.1005, idx=10, minute=10)  # candle_time = ts(10)

# OB and FVG overlap (positive width), both created AFTER the confirmation swing
m1_obs_overlap = [(1.0995, 1.1005, ts(12))]
m1_fvgs_overlap = [(1.1000, 1.1010, ts(13))]
zone = compute_entry_zone(Direction.BULLISH, pair_for_zone, conf_swing_for_zone, m1_obs_overlap, m1_fvgs_overlap)
check("6a OB+FVG overlap: entry zone = intersection, entry price = its midpoint",
      zone is not None and zone[3] == "ob+fvg" and abs(zone[0] - (1.1000 + 1.1005) / 2.0) < 1e-9, f"{zone}")

# OB and FVG adjacent only (boundary touch, still "overlap" per inclusive semantics)
m1_obs_adj = [(1.0990, 1.1000, ts(12))]
m1_fvgs_adj = [(1.1000, 1.1010, ts(13))]
zone_adj = compute_entry_zone(Direction.BULLISH, pair_for_zone, conf_swing_for_zone, m1_obs_adj, m1_fvgs_adj)
check("6b OB+FVG boundary-touch (adjacent): still treated as overlap, entry = shared price",
      zone_adj is not None and zone_adj[3] == "ob+fvg" and abs(zone_adj[0] - 1.1000) < 1e-9, f"{zone_adj}")

# No overlap at all -- deeper discount/premium structure wins (BULLISH wants lower ratio = deeper discount)
m1_obs_far = [(1.0920, 1.0930, ts(12))]   # deep discount, near range low
m1_fvgs_shallow = [(1.1140, 1.1150, ts(13))]  # shallow, near range high (premium side)
zone_deep = compute_entry_zone(Direction.BULLISH, pair_for_zone, conf_swing_for_zone, m1_obs_far, m1_fvgs_shallow)
check("6c no overlap: BULLISH picks the deeper-discount zone (the OB here), its own midpoint",
      zone_deep is not None and zone_deep[3] == "ob" and abs(zone_deep[0] - (1.0920 + 1.0930) / 2.0) < 1e-9, f"{zone_deep}")

pair_for_zone_bear = mk_pair(1.0900, 1.1200, TrendState.BEARISH)
conf_swing_bear_for_zone = mk_m1_swing(SwingType.LOW, 1.0995, idx=10, minute=10)
zone_deep_bear = compute_entry_zone(Direction.BEARISH, pair_for_zone_bear, conf_swing_bear_for_zone, m1_obs_far, m1_fvgs_shallow)
check("6d no overlap, BEARISH: picks the deeper-premium zone (the FVG here, near range high)",
      zone_deep_bear is not None and zone_deep_bear[3] == "fvg", f"{zone_deep_bear}")

zone_only_ob = compute_entry_zone(Direction.BULLISH, pair_for_zone, conf_swing_for_zone, m1_obs_overlap, [])
check("6e only an OB exists (no FVG): uses the OB's own midpoint",
      zone_only_ob is not None and zone_only_ob[3] == "ob" and abs(zone_only_ob[0] - (1.0995 + 1.1005) / 2.0) < 1e-9)

check("6f neither OB nor FVG exists: no valid entry zone",
      compute_entry_zone(Direction.BULLISH, pair_for_zone, conf_swing_for_zone, [], []) is None)

# KEY REGRESSION TEST: with two candidates, the one CLOSER in price to the
# confirmation swing (1.1005) is picked over a far-away one -- this is
# exactly the real-data bug found (a stale M1 OB 50+ pips from the
# confirmation swing was being selected purely because it was the most
# recently created still-active zone, producing an inverted SL-vs-entry
# ordering on every real case before this fix).
m1_obs_far_away = [(1.1500, 1.1510, ts(5))]   # far from the swing (1.1005), but created earlier
check("6g a lone far-away zone is still returned (no other candidate exists)",
      compute_entry_zone(Direction.BULLISH, pair_for_zone, conf_swing_for_zone, m1_obs_far_away, []) is not None)

m1_obs_mixed = [(1.1500, 1.1510, ts(5)), (1.0995, 1.1005, ts(2))]  # far one created LATER, near one created EARLIER
zone_mixed = compute_entry_zone(Direction.BULLISH, pair_for_zone, conf_swing_for_zone, m1_obs_mixed, [])
check("6h with a near AND a far candidate, the NEAR one wins even though it's the OLDER of the two",
      zone_mixed is not None and abs(zone_mixed[0] - (1.0995 + 1.1005) / 2.0) < 1e-9, f"{zone_mixed}")

m1_obs_tie_break = [(1.0990, 1.1000, ts(2)), (1.1000, 1.1010, ts(8))]  # equidistant from 1.1005 (both 0.0005 away)
zone_tie = compute_entry_zone(Direction.BULLISH, pair_for_zone, conf_swing_for_zone, m1_obs_tie_break, [])
check("6i equidistant candidates: tie broken by most-recently-created",
      zone_tie is not None and abs(zone_tie[0] - (1.1000 + 1.1010) / 2.0) < 1e-9, f"{zone_tie}")

print(); print("="*78); print("SECTION 7 — STOP LOSS FROM CONFIRMATION SWING (E7, spec §18)"); print("="*78)

conf_swing_bull = mk_m1_swing(SwingType.HIGH, 1.1005)
sl_bull = compute_stop_loss(Direction.BULLISH, conf_swing_bull, "EURUSD")
check("7a BULLISH SL = swing.price - buffer (2 pips = 0.0002)", abs(sl_bull - (1.1005 - 0.0002)) < 1e-9, f"{sl_bull}")
conf_swing_bear = mk_m1_swing(SwingType.LOW, 1.0995)
sl_bear = compute_stop_loss(Direction.BEARISH, conf_swing_bear, "EURUSD")
check("7b BEARISH SL = swing.price + buffer", abs(sl_bear - (1.0995 + 0.0002)) < 1e-9, f"{sl_bear}")
sl_xau = compute_stop_loss(Direction.BULLISH, mk_m1_swing(SwingType.HIGH, 4000.00), "XAUUSD")
check("7c XAUUSD SL uses the wider 3-pip buffer ($0.30)", abs(sl_xau - (4000.00 - 0.30)) < 1e-9)

print(); print("="*78); print("SECTION 8 — TP PRIORITY FALLBACK (E8, spec §19)"); print("="*78)

entry_price, stop_loss = 1.1010, 1.0993  # risk = 0.0017
check("8a RR computed correctly", abs(compute_rr(1.1010, 1.1000, 1.1060) - 5.0) < 1e-9)
check("8b zero-risk returns 0.0", compute_rr(1.1000, 1.1000, 1.1100) == 0.0)

htf_clears = 1.1200   # reward=0.019, rr~11.2 -- clears
result = select_take_profit(Direction.BULLISH, entry_price, stop_loss, htf_clears, 1.1050, 1.1030)
check("8c HTF target clears RR>=5: used first, even though other targets exist",
      result is not None and abs(result[0] - htf_clears) < 1e-9 and result[2] == "htf_swing", f"{result}")

htf_fails = 1.1013   # reward=0.0003, rr~0.18 -- fails
eq_clears = 1.1100   # reward=0.009, rr~5.3 -- clears
result2 = select_take_profit(Direction.BULLISH, entry_price, stop_loss, htf_fails, eq_clears, 1.1030)
check("8d HTF fails RR: falls back to equal-level target, which clears",
      result2 is not None and abs(result2[0] - eq_clears) < 1e-9 and result2[2] == "equal_level", f"{result2}")

eq_fails = 1.1012
asia_clears = 1.1120  # reward=0.011, rr~6.47 -- clears
result3 = select_take_profit(Direction.BULLISH, entry_price, stop_loss, htf_fails, eq_fails, asia_clears)
check("8e HTF and equal-level both fail: falls back to opposite Asian range, which clears",
      result3 is not None and abs(result3[0] - asia_clears) < 1e-9 and result3[2] == "opposite_asia", f"{result3}")

result4 = select_take_profit(Direction.BULLISH, entry_price, stop_loss, htf_fails, eq_fails, 1.1011)
check("8f all three fail RR: rejected (None)", result4 is None)

result5 = select_take_profit(Direction.BULLISH, entry_price, stop_loss, None, None, asia_clears)
check("8g missing candidates (None) are skipped, not treated as failures", result5 is not None and result5[2] == "opposite_asia")

print(); print("="*78); print("SECTION 9 — FULL evaluate_entry() ORCHESTRATION"); print("="*78)

def base_args(**overrides):
    args = dict(
        symbol="EURUSD", direction=Direction.BULLISH,
        pair=mk_pair(1.0900, 1.1200, TrendState.BULLISH),  # equilibrium 1.1050
        sweep_level=LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0995,
                                    primary_timeframe=Timeframe.H1, created_at=ts(0),
                                    swept=True, swept_at=ts(15), reaction="confirmed_reversal"),
        poi_kind="ob", zone_low=1.0995, zone_high=1.1005,   # M15 POI, discount half
        # Touch happened at minute 15 -- BEFORE the confirmation candle
        # (minute 20), not on the same bar (see zone_touch_confirmed's
        # docstring for why these are decoupled).
        zone_touched_at=ts(15),
        entry_candle=C(20, 1.0993, 1.1012, 1.0992, 1.1010),  # displacement (body=0.0017), breaks swing
        m1_swings=[mk_m1_swing(SwingType.HIGH, 1.1005, idx=10, minute=10)],
        last_3_m1_candles=[c1, c2, c3],
        m1_atr_at_prev=atr_val,
        # entry zone intersection = (1.1005, 1.1007), midpoint 1.1006 -- above
        # the confirmation-swing-derived SL (1.1005 - 0.0002 = 1.1003), so
        # SL < entry as a real bullish trade requires. created_at=ts(12) is
        # AFTER the confirmation swing (ts(10)), satisfying the "not stale"
        # filter in compute_entry_zone.
        m1_obs=[(1.1003, 1.1007, ts(12))], m1_fvgs=[(1.1005, 1.1012, ts(12))],
        htf_swing_target=1.1200, equal_level_target=None, opposite_asia_target=None,
    )
    args.update(overrides)
    return args

signal = evaluate_entry(**base_args())
check("9a happy path: produces a valid EntrySignal", signal is not None and isinstance(signal, EntrySignal), f"{signal}")
if signal:
    check("9b entry_price is the Entry Zone midpoint (not the candle close)",
          abs(signal.entry_price - (1.1005 + 1.1007) / 2.0) < 1e-9, f"{signal.entry_price}")
    check("9c stop_loss derived from the confirmation swing (1.1005 - buffer)",
          abs(signal.stop_loss - (1.1005 - 0.0002)) < 1e-9, f"{signal.stop_loss}")
    check("9c2 stop_loss is below entry (valid bullish risk ordering)", signal.stop_loss < signal.entry_price)
    check("9d rr >= MIN_RR and take_profit_source is htf_swing", signal.rr >= config_v2.MIN_RR and signal.take_profit_source == "htf_swing")
    check("9e confirmation_swing attached to the signal", signal.confirmation_swing.price == 1.1005)

check("9f stale sweep: rejected", evaluate_entry(**base_args(sweep_level=LiquidityLevel(
    symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0995, primary_timeframe=Timeframe.H1,
    created_at=ts(0), swept=True, swept_at=ts(0) - timedelta(hours=10), reaction="confirmed_reversal"))) is None)

check("9g breakout (not reversal) sweep: rejected", evaluate_entry(**base_args(sweep_level=LiquidityLevel(
    symbol="EURUSD", kind=LiquidityLevelType.ASIA_LOW, price=1.0995, primary_timeframe=Timeframe.H1,
    created_at=ts(0), swept=True, swept_at=ts(15), reaction="breakout"))) is None)

check("9h never touched (zone_touched_at=None): rejected", evaluate_entry(**base_args(zone_touched_at=None)) is None)

check("9h2 touch too stale (beyond max_touch_age): rejected", evaluate_entry(**base_args(
    zone_touched_at=ts(15) - timedelta(hours=10))) is None)

check("9i POI in premium half: rejected", evaluate_entry(**base_args(
    zone_low=1.1080, zone_high=1.1090)) is None)

check("9j Absorption fails: rejected", evaluate_entry(**base_args(last_3_m1_candles=[c1_tie, c2_tie, c3_tie])) is None)

check("9k no M1 swing break: rejected", evaluate_entry(**base_args(
    entry_candle=C(20, 1.0998, 1.1004, 1.0997, 1.1002))) is None)

check("9l no Entry Zone available (neither M1 OB nor FVG): rejected",
      evaluate_entry(**base_args(m1_obs=[], m1_fvgs=[])) is None)

check("9l2 an OLD (by creation time) but PRICE-CLOSE M1 OB/FVG still produces a valid signal "
      "-- only price proximity to the swing matters now, not creation recency",
      evaluate_entry(**base_args(m1_obs=[(1.1003, 1.1007, ts(5))], m1_fvgs=[(1.1005, 1.1012, ts(5))])) is not None)

check("9m RR fails on every TP tier: rejected", evaluate_entry(**base_args(
    htf_swing_target=1.1010, equal_level_target=None, opposite_asia_target=None)) is None)

check("9n Entry Zone/confirmation-swing produce inverted SL-vs-entry ordering: rejected as invalid risk",
      evaluate_entry(**base_args(m1_obs=[(1.0995, 1.1005, ts(12))], m1_fvgs=[(1.1000, 1.1010, ts(12))])) is None)

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
