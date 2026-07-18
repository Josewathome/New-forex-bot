"""Regression test for src/application/strategy_v2/fvg.py against the
test matrix agreed before implementation: positive, negative, boundary,
mitigation, invalidation, multi-FVG overlap, and per-instrument sizing
(XAUUSD / forex / JPY) cases.

HOW TO RUN
    python3 tools/validate_fvg.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Candle, Timeframe, Direction
from src.domain.strategy_v2_entities import FVGZone
from src.application.strategy_v2.fvg import detect_fvg, update_fvg_state, is_tradable, overlap_range, min_fvg_size
from src.application.strategy_v2.atr import wilder_atr_series

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
def ts(n): return T0 + timedelta(hours=n)
def C(n, o, h, l, c, sym="EURUSD"):
    return Candle(time=ts(n), open=o, high=h, low=l, close=c, volume=100.0, symbol=sym, timeframe=Timeframe.H1)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

def flat_atr_candles(n, sym="EURUSD", base=1.1000, rng=0.0010):
    """n filler candles with a stable ~rng true range, to seed a known ATR."""
    out = []
    for i in range(n):
        out.append(C(i, base, base + rng/2, base - rng/2, base, sym))
    return out

print("="*78); print("SECTION 1 — POSITIVE cases"); print("="*78)

# Bullish FVG: gap must be >= min size (5 pips = 0.0005 EURUSD) and displacement >= 2x ATR.
# NOTE on indices: filler occupies list-positions 0-9; c1/c2/c3 are then
# appended at positions 10/11/12 respectively (regardless of what "n" they
# were built with, which only sets their .time field, not their list
# position) -- so the ATR value that applies to c2 is always atr_series[10].
filler = flat_atr_candles(10)  # seeds ATR ~= 0.0010 (true range) after 10 bars
c1 = C(9, 1.1000, 1.1005, 1.0998, 1.1002)
c2 = C(10, 1.1002, 1.1030, 1.1000, 1.1028)  # body=0.0026, well over 2x~0.001 ATR
c3 = C(11, 1.1028, 1.1040, 1.1010, 1.1035)  # c1.high(1.1005) < c3.low(1.1010): gap=0.0005
seq = filler + [c1, c2, c3]
atr_series = wilder_atr_series(seq, 10)
zone = detect_fvg("EURUSD", Timeframe.H1, c1, c2, c3, atr_series[10])
check("1a positive bullish FVG detected with correct bounds",
      zone is not None and zone.direction == Direction.BULLISH
      and abs(zone.gap_low - 1.1005) < 1e-9 and abs(zone.gap_high - 1.1010) < 1e-9,
      f"zone={zone}")

# Bearish mirror
c1b = C(9, 1.1000, 1.1005, 1.0998, 1.0999)
c2b = C(10, 1.0999, 1.1000, 1.0970, 1.0972)  # body=0.0027
c3b = C(11, 1.0972, 1.0990, 1.0960, 1.0975)  # c1.low(1.0998) > c3.high(1.0990): gap=0.0008
seqb = filler + [c1b, c2b, c3b]
atr_b = wilder_atr_series(seqb, 10)
zoneb = detect_fvg("EURUSD", Timeframe.H1, c1b, c2b, c3b, atr_b[10])
check("1b positive bearish FVG detected with correct bounds",
      zoneb is not None and zoneb.direction == Direction.BEARISH
      and abs(zoneb.gap_low - 1.0990) < 1e-9 and abs(zoneb.gap_high - 1.0998) < 1e-9,
      f"zone={zoneb}")

print(); print("="*78); print("SECTION 2 — NEGATIVE cases"); print("="*78)

# 2a. Gap exists but displacement (c2 body) fails.
c2_weak = C(10, 1.1002, 1.1006, 1.1000, 1.1004)  # body=0.0002, well under 2x ATR
seq2 = filler + [c1, c2_weak, c3]
atr2 = wilder_atr_series(seq2, 10)
check("2a gap exists but displacement fails: no FVG",
      detect_fvg("EURUSD", Timeframe.H1, c1, c2_weak, c3, atr2[10]) is None)

# 2b. Displacement passes but no actual gap (c1.high >= c3.low).
c3_no_gap = C(11, 1.1028, 1.1040, 1.1003, 1.1035)  # c3.low=1.1003 <= c1.high=1.1005
seq2b = filler + [c1, c2, c3_no_gap]
atr2b = wilder_atr_series(seq2b, 10)
check("2b displacement passes but no real gap: no FVG",
      detect_fvg("EURUSD", Timeframe.H1, c1, c2, c3_no_gap, atr2b[10]) is None)

# 2c. Gap + displacement pass but gap size below the minimum.
c1_small = C(9, 1.1000, 1.10050, 1.0998, 1.1002)
c3_small = C(11, 1.1028, 1.1040, 1.10052, 1.1035)  # gap = 0.00002, tiny
seq2c = filler + [c1_small, c2, c3_small]
atr2c = wilder_atr_series(seq2c, 10)
check("2c gap below minimum size: no FVG",
      detect_fvg("EURUSD", Timeframe.H1, c1_small, c2, c3_small, atr2c[10]) is None)

# 2d. Neither bullish nor bearish gap condition holds (fully overlapping range).
c1_ovl = C(9, 1.1000, 1.1010, 1.0998, 1.1002)
c3_ovl = C(11, 1.1005, 1.1015, 1.1000, 1.1008)  # overlaps c1 entirely
seq2d = filler + [c1_ovl, c2, c3_ovl]
atr2d = wilder_atr_series(seq2d, 10)
check("2d no gap condition holds at all: no FVG",
      detect_fvg("EURUSD", Timeframe.H1, c1_ovl, c2, c3_ovl, atr2d[10]) is None)

print(); print("="*78); print("SECTION 3 — BOUNDARY cases"); print("="*78)

# 3a. Gap size EXACTLY at the minimum (inclusive, >=).
min_size = min_fvg_size("EURUSD")  # 0.0005
c3_exact = C(11, 1.1028, 1.1040, 1.1005 + min_size, 1.1035)
seq3a = filler + [c1, c2, c3_exact]
atr3a = wilder_atr_series(seq3a, 10)
z3a = detect_fvg("EURUSD", Timeframe.H1, c1, c2, c3_exact, atr3a[10])
check("3a gap size exactly == minimum: FVG created (inclusive boundary)",
      z3a is not None, f"gap={z3a.gap_high-z3a.gap_low if z3a else None} vs min={min_size}")

# 3b. Displacement body EXACTLY == 2.0 x ATR (inclusive).
atr_val = atr3a[10]
exact_body_close = 1.1002 + 2.0 * atr_val
c2_exact = C(10, 1.1002, exact_body_close + 0.0005, 1.1000, exact_body_close)
seq3b = filler + [c1, c2_exact, c3]
atr3b = wilder_atr_series(seq3b, 10)
z3b = detect_fvg("EURUSD", Timeframe.H1, c1, c2_exact, c3, atr3b[10])
check("3b displacement body exactly == 2.0x ATR: passes (inclusive boundary)",
      z3b is not None)

# 3c. ATR unavailable (fewer than 10 preceding candles) -> must not silently pass.
short_seq = [c1, c2, c3]  # no seed history at all
atr_short = wilder_atr_series(short_seq, 10)
check("3c ATR unavailable: no FVG (never silently passes displacement)",
      detect_fvg("EURUSD", Timeframe.H1, c1, c2, c3, atr_short[1] if len(atr_short) > 1 else None) is None)

print(); print("="*78); print("SECTION 4 — MITIGATION (wick-touch, no close required)"); print("="*78)

zone_m = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                  gap_high=1.1010, gap_low=1.1005, created_at=ts(11))
midpoint = zone_m.midpoint  # 1.10075
touch_wick_only = C(12, 1.1030, 1.1032, midpoint - 0.00001, 1.1028)  # wick dips to midpoint, closes well above
update_fvg_state(zone_m, touch_wick_only)
check("4a wick touches midpoint, closes well above: mitigated=True (wick sufficient)",
      zone_m.mitigated is True)

zone_m2 = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                   gap_high=1.1010, gap_low=1.1005, created_at=ts(11))
through_to_far_half = C(12, 1.1030, 1.1032, 1.1006, 1.1028)  # wicks past midpoint into far half
update_fvg_state(zone_m2, through_to_far_half)
check("4b wick passes through midpoint into far half: still mitigated=True",
      zone_m2.mitigated is True)

zone_m3 = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                   gap_high=1.1010, gap_low=1.1005, created_at=ts(11))
never_reaches = C(12, 1.1030, 1.1032, 1.1020, 1.1028)  # low stays well above midpoint
update_fvg_state(zone_m3, never_reaches)
check("4c price never reaches midpoint: mitigated=False",
      zone_m3.mitigated is False)
check("4d is_tradable() reflects mitigation correctly",
      is_tradable(zone_m) is False and is_tradable(zone_m3) is True)

print(); print("="*78); print("SECTION 5 — INVALIDATION (body close beyond far side)"); print("="*78)

zone_i = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                  gap_high=1.1010, gap_low=1.1005, created_at=ts(11))
close_beyond_far = C(12, 1.1006, 1.1007, 1.1000, 1.1002)  # body closes below gap_low (1.1005)
update_fvg_state(zone_i, close_beyond_far)
check("5a body closes beyond far side: invalidated=True",
      zone_i.invalidated is True)

zone_i2 = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                   gap_high=1.1010, gap_low=1.1005, created_at=ts(11))
wick_only_beyond = C(12, 1.1007, 1.1008, 1.1000, 1.1006)  # wick exceeds far side, body closes back inside
update_fvg_state(zone_i2, wick_only_beyond)
check("5b wick alone exceeds far side, body closes back inside: NOT invalidated",
      zone_i2.invalidated is False)
# NOTE: a wick deep enough to reach the far side geometrically must also
# have passed the midpoint (the far side is farther than the midpoint) --
# so this same candle correctly also triggers mitigation. is_tradable is
# False here because of mitigation, not invalidation; that's the correct,
# expected interaction between the two independent one-way flags.
check("5c zone_i2 is not invalidated but IS mitigated (wick passed the midpoint en route to the far side)",
      zone_i2.invalidated is False and zone_i2.mitigated is True and is_tradable(zone_i2) is False)
check("5d zone_i (5a) is untradable via invalidation",
      is_tradable(zone_i) is False)

print(); print("="*78); print("SECTION 6 — MULTI-FVG OVERLAP"); print("="*78)

zone_a = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                  gap_high=1.1020, gap_low=1.1000, created_at=ts(11))
zone_b = FVGZone(symbol="EURUSD", timeframe=Timeframe.M15, direction=Direction.BULLISH,
                  gap_high=1.1015, gap_low=1.1005, created_at=ts(20))
ov = overlap_range(zone_a.gap_low, zone_a.gap_high, zone_b.gap_low, zone_b.gap_high)
check("6a overlapping H1+M15 FVGs: both tracked independently, intersection computed correctly",
      ov == (1.1005, 1.1015), f"overlap={ov}")

zone_c = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BEARISH,
                  gap_high=1.0990, gap_low=1.0980, created_at=ts(30))
zone_d = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BEARISH,
                  gap_high=1.0970, gap_low=1.0960, created_at=ts(31))
ov2 = overlap_range(zone_c.gap_low, zone_c.gap_high, zone_d.gap_low, zone_d.gap_high)
check("6b non-overlapping FVGs: no intersection (None)", ov2 is None)

zone_e_low, zone_e_high = 1.1000, 1.1030
zone_f_low, zone_f_high = 1.1005, 1.1015  # fully contained inside e
ov3 = overlap_range(zone_e_low, zone_e_high, zone_f_low, zone_f_high)
check("6c newer FVG fully contained in an older one: both remain trackable, overlap = the inner range",
      ov3 == (1.1005, 1.1015))

print(); print("="*78); print("SECTION 7 — PER-INSTRUMENT SIZING (XAUUSD / forex / JPY)"); print("="*78)

check("7a XAUUSD min FVG size = 50 points = 0.50 (5 pips at 0.10/pip)",
      abs(min_fvg_size("XAUUSD") - 0.50) < 1e-9, f"got {min_fvg_size('XAUUSD')}")
check("7b forex (GBPUSD/EURUSD/AUDUSD) min FVG size = 5 pips = 0.0005",
      abs(min_fvg_size("EURUSD") - 0.0005) < 1e-9 and abs(min_fvg_size("GBPUSD") - 0.0005) < 1e-9)
check("7c USDJPY min FVG size = 5 pips = 0.05 (at 0.01/pip)",
      abs(min_fvg_size("USDJPY") - 0.05) < 1e-9, f"got {min_fvg_size('USDJPY')}")

xau_filler = flat_atr_candles(10, sym="XAUUSD", base=4000.00, rng=2.0)
c1x = C(9, 4000.00, 4001.00, 3999.00, 4000.50, "XAUUSD")
c2x = C(10, 4000.50, 4010.00, 4000.00, 4009.00, "XAUUSD")  # body 8.5, well over 2x~2.0 ATR
c3x_exact = C(11, 4009.00, 4012.00, 4001.50, 4011.00, "XAUUSD")  # gap = 4001.50-4001.00 = 0.50 exact
seqx = xau_filler + [c1x, c2x, c3x_exact]
atrx = wilder_atr_series(seqx, 10)
zx = detect_fvg("XAUUSD", Timeframe.H1, c1x, c2x, c3x_exact, atrx[10])
check("7d XAUUSD gap exactly at $0.50 minimum: FVG created",
      zx is not None, f"zone={zx}")
c3x_below = C(11, 4009.00, 4012.00, 4001.49, 4011.00, "XAUUSD")  # gap = 0.49, below min
seqx2 = xau_filler + [c1x, c2x, c3x_below]
atrx2 = wilder_atr_series(seqx2, 10)
check("7e XAUUSD gap at $0.49 (below minimum): no FVG",
      detect_fvg("XAUUSD", Timeframe.H1, c1x, c2x, c3x_below, atrx2[10]) is None)

jpy_filler = flat_atr_candles(10, sym="USDJPY", base=150.000, rng=0.100)
c1j = C(9, 150.000, 150.050, 149.900, 150.020, "USDJPY")
c2j = C(10, 150.020, 150.300, 150.000, 150.280, "USDJPY")  # body 0.26, over 2x~0.1 ATR
c3j_exact = C(11, 150.280, 150.350, 150.100, 150.320, "USDJPY")  # gap = 150.100-150.050 = 0.050 exact
seqj = jpy_filler + [c1j, c2j, c3j_exact]
atrj = wilder_atr_series(seqj, 10)
zj = detect_fvg("USDJPY", Timeframe.H1, c1j, c2j, c3j_exact, atrj[10])
check("7f USDJPY gap exactly at 0.05 (5 pips) minimum: FVG created",
      zj is not None, f"zone={zj}")

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
