"""Regression test for src/application/smc_3step.py -- the Python port of
the user-supplied SmartMoney_3Step_EA.mq5. Hand-built fixtures for each
ported function, matching the repo's existing tools/validate_*.py
convention (plain assertions, no pytest).

HOW TO RUN
    python3 tools/validate_smc_3step.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Candle, Timeframe

from src.application.smc_3step import (
    ema_series, find_fractal_swings, get_trend, Zone, is_zone_invalidated,
    find_order_block, find_fair_value_gap, find_pullback_zone, check_choch,
    calculate_lot_size, ThreeStepEA, ThreeStepConfig, build_signal, EAState, SwingKind,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
def ts(n): return T0 + timedelta(minutes=5 * n)
def C(n, o, h, l, c, sym="EURUSD", tf=Timeframe.M15):
    return Candle(time=ts(n), open=o, high=h, low=l, close=c, volume=100.0, symbol=sym, timeframe=tf)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

def build_fixture(n, base_high, base_low, high_peaks=None, low_troughs=None, tf=Timeframe.H1):
    """n candles with a flat high/low baseline (ties never qualify as a
    fractal, since the test requires STRICT inequality) except at the
    given {index: value} peaks/troughs, which are independent of each
    other (separate OHLC fields) so placement doesn't interact."""
    high_peaks = high_peaks or {}
    low_troughs = low_troughs or {}
    out = []
    for i in range(n):
        h = high_peaks.get(i, base_high)
        l = low_troughs.get(i, base_low)
        out.append(C(i, l + 0.3 * (h - l), h, l, l + 0.5 * (h - l), tf=tf))
    return out

print(); print("="*78); print("SECTION 1 — EMA"); print("="*78)

# Hand-computed EMA(3): k=2/4=0.5. Closes: 10,20,30,40,50.
# Seed (bar idx 2) = avg(10,20,30)=20. EMA[3]=40*0.5+20*0.5=30. EMA[4]=50*0.5+30*0.5=40.
closes = [10, 20, 30, 40, 50]
ema = ema_series(closes, 3)
check("1a EMA is None before the seed bar", ema[0] is None and ema[1] is None)
check("1b EMA seed = SMA of first `period` closes", abs(ema[2] - 20.0) < 1e-9, f"{ema[2]}")
check("1c EMA recursive step matches hand computation", abs(ema[3] - 30.0) < 1e-9 and abs(ema[4] - 40.0) < 1e-9, f"{ema}")
check("1d too-short series returns all-None", all(v is None for v in ema_series([1, 2], 3)))

print(); print("="*78); print("SECTION 2 — Fractal swings (symmetric, wing bars each side)"); print("="*78)

# Independent high-peaks and low-troughs (separate OHLC fields, so their
# index placement doesn't interact) on a flat baseline (ties never
# qualify -- strict inequality is required on both sides).
candles = build_fixture(13, base_high=3.0, base_low=1.0,
                         high_peaks={3: 5.0, 9: 6.0}, low_troughs={5: 0.3, 8: 0.1})
highs, lows = find_fractal_swings(candles, wing=2, lookback=20, up_to_index=len(candles)-1)
check("2a finds exactly 2 fractal highs and 2 fractal lows when scanning back from the end",
      len(highs) == 2 and len(lows) == 2,
      f"highs={[h.price for h in highs]} lows={[l.price for l in lows]}")
check("2b fractal highs are index 3 (price 5) and index 9 (price 6), oldest-first",
      highs and abs(highs[0].price - 5.0) < 1e-9 and abs(highs[1].price - 6.0) < 1e-9, f"{[h.price for h in highs]}")
check("2c fractal lows are index 8 (price 0.1) and index 5 (price 0.3), oldest-first",
      lows and abs(lows[0].price - 0.3) < 1e-9 and abs(lows[1].price - 0.1) < 1e-9, f"{[l.price for l in lows]}")

# Fewer than 2 fractals available -> empty lists (mirrors MQL5's `return false`)
short_candles = candles[:5]
highs2, lows2 = find_fractal_swings(short_candles, wing=2, lookback=20, up_to_index=len(short_candles)-1)
check("2d too few fractals -> empty lists, not a crash", highs2 == [] and lows2 == [])

print(); print("="*78); print("SECTION 3 — get_trend (EMA slope AND swing structure must agree)"); print("="*78)

# Uptrend fixture (also reused by Section 10's state-machine tests):
# independent, rising fractal highs/lows (peak1<peak2, trough1<trough2),
# all within the wing=2/swing_lookback=20/up_to_index=29 scan range
# (i=27..8), plus a separately-controlled mildly-rising close series for
# the EMA slope leg. High/low are clamped to stay consistent with the
# close (low <= close <= high) at every bar.
def build_trending_fixture(n, base_high, base_low, high_peaks, low_troughs, close_start, close_step, tf=Timeframe.H1):
    out = []
    for i in range(n):
        close = close_start + close_step * i
        h = max(high_peaks.get(i, base_high), close + 0.01)
        l = min(low_troughs.get(i, base_low), close - 0.01)
        out.append(C(i, close - 0.005, h, l, close, tf=tf))
    return out

up_candles = build_trending_fixture(
    30, base_high=1.10, base_low=1.05,
    high_peaks={12: 1.20, 22: 1.30}, low_troughs={10: 0.90, 20: 0.95},
    close_start=1.02, close_step=0.003,
)
up_closes = [c.close for c in up_candles]
up_ema = ema_series(up_closes, 5)
trend_up = get_trend(up_candles, len(up_candles)-1, up_ema, slope_bars=5, swing_lookback=20, wing=2)
check("3a rising EMA + rising swing structure -> bullish (1)", trend_up == 1, f"{trend_up}")

flat_candles = [C(i, 1.1000, 1.1010, 1.0990, 1.1000, tf=Timeframe.H1) for i in range(30)]
flat_ema = ema_series([c.close for c in flat_candles], 5)
trend_flat = get_trend(flat_candles, len(flat_candles)-1, flat_ema, slope_bars=5, swing_lookback=20, wing=2)
check("3b perfectly flat data -> no clear trend (0)", trend_flat == 0, f"{trend_flat}")

print(); print("="*78); print("SECTION 4 — Order Block (find_order_block)"); print("="*78)

# Bullish OB fixture: a bearish base candle at idx 5, followed by a strong
# bullish impulse candle at idx 6 that closes above the base's high.
m15 = []
for i in range(20):
    m15.append(C(i, 1.1000, 1.1005, 1.0995, 1.1000, tf=Timeframe.M15))
# base (bearish): idx 5
m15[5] = C(5, 1.1000, 1.1002, 1.0980, 1.0985, tf=Timeframe.M15)
# impulse (bullish, body >= 1.3x base body=0.0015 -> need >=0.00195): idx 6
m15[6] = C(6, 1.0985, 1.1030, 1.0985, 1.1025, tf=Timeframe.M15)  # body=0.0040, closes above base.high(1.1002)

z = find_order_block(m15, up_to_index=10, trend=1, lookback=15, impulse_factor=1.3, current_price=1.0990)
check("4a bullish OB found when price is currently inside the zone",
      z is not None and z.direction == 1 and z.is_ob, f"{z}")
check("4b OB zone bounds match the base candle's range", z is not None and abs(z.top - 1.1002) < 1e-9 and abs(z.bottom - 1.0980) < 1e-9, f"{z}")

z_outside = find_order_block(m15, up_to_index=10, trend=1, lookback=15, impulse_factor=1.3, current_price=1.2000)
check("4c same OB NOT returned when current price is outside its band", z_outside is None, f"{z_outside}")

z_weak = find_order_block(m15, up_to_index=10, trend=1, lookback=15, impulse_factor=10.0, current_price=1.0990)
check("4d impulse factor too strict -> no OB found", z_weak is None)

# Invalidated case: a later candle closes below the zone's bottom.
m15_inv = list(m15)
m15_inv[8] = C(8, 1.0990, 1.0995, 1.0970, 1.0975, tf=Timeframe.M15)  # closes below 1.0980
z_inv = find_order_block(m15_inv, up_to_index=10, trend=1, lookback=15, impulse_factor=1.3, current_price=1.0990)
check("4e OB invalidated by a later close beyond its bottom -> not returned", z_inv is None, f"{z_inv}")

print(); print("="*78); print("SECTION 5 — Fair Value Gap (find_fair_value_gap)"); print("="*78)

m15_fvg = []
for i in range(20):
    m15_fvg.append(C(i, 1.1000, 1.1005, 1.0995, 1.1000, tf=Timeframe.M15))
# Bullish FVG: a=idx5 (high=1.1005), b=idx6 (bullish displacement), c=idx7 (low=1.1020 > a.high)
m15_fvg[5] = C(5, 1.0998, 1.1005, 1.0990, 1.1000, tf=Timeframe.M15)
m15_fvg[6] = C(6, 1.1000, 1.1030, 1.1000, 1.1028, tf=Timeframe.M15)
m15_fvg[7] = C(7, 1.1025, 1.1035, 1.1020, 1.1030, tf=Timeframe.M15)
# Candles AFTER formation (idx8-10) must not close below the gap's bottom
# (1.1005) or is_zone_invalidated correctly kills it -- the pre-formation
# filler baseline (close=1.1000) would do exactly that if left unchanged.
for i in range(8, 11):
    m15_fvg[i] = C(i, 1.1010, 1.1015, 1.1008, 1.1012, tf=Timeframe.M15)

fz = find_fair_value_gap(m15_fvg, up_to_index=10, trend=1, lookback=15, current_price=1.1010)
check("5a bullish FVG found, price inside the gap",
      fz is not None and fz.direction == 1 and not fz.is_ob and abs(fz.bottom - 1.1005) < 1e-9 and abs(fz.top - 1.1020) < 1e-9,
      f"{fz}")

fz_outside = find_fair_value_gap(m15_fvg, up_to_index=10, trend=1, lookback=15, current_price=1.5000)
check("5b same FVG not returned when price is outside the gap", fz_outside is None)

print(); print("="*78); print("SECTION 6 — find_pullback_zone (OB tried first, then FVG)"); print("="*78)

z_combo = find_pullback_zone(m15, up_to_index=10, trend=1, lookback=15, impulse_factor=1.3, current_price=1.0990)
check("6a OB preferred over FVG when both would qualify", z_combo is not None and z_combo.is_ob)

print(); print("="*78); print("SECTION 7 — CHoCH (check_choch)"); print("="*78)

# 21 candles (0-20): two independent, reachable fractal highs (idx7, idx14)
# and two fractal lows (idx5, idx12), on a flat baseline; index 20 is the
# confirming candle whose CLOSE must break the newest high (idx14).
m5 = build_fixture(21, base_high=1.1000, base_low=1.0995,
                    high_peaks={7: 1.1010, 14: 1.1012}, low_troughs={5: 1.0980, 12: 1.0985},
                    tf=Timeframe.M5)
m5[20] = C(20, 1.1013, 1.1020, 1.1012, 1.1018, tf=Timeframe.M5)   # close (1.1018) > idx14 high (1.1012)
level = check_choch(m5, up_to_index=20, direction=1, lookback=20, wing=2)
check("7a bullish CHoCH triggers when close breaks the recent swing high", level is not None, f"{level}")

m5_no_break = list(m5)
m5_no_break[20] = C(20, 1.1000, 1.1005, 1.0995, 1.1000, tf=Timeframe.M5)   # close does not break idx14's high
level2 = check_choch(m5_no_break, up_to_index=20, direction=1, lookback=20, wing=2)
check("7b no break -> None", level2 is None)

print(); print("="*78); print("SECTION 8 — Position sizing (calculate_lot_size)"); print("="*78)

lot = calculate_lot_size(balance=10000, risk_percent=1.0, sl_distance=0.0020,
                          tick_value=1.0, tick_size=0.00001, min_lot=0.01, max_lot=100.0, lot_step=0.01)
# risk_money=100, value_per_unit=1/0.00001=100000, loss_per_lot=0.0020*100000=200, lot=100/200=0.5
check("8a lot size matches hand computation", abs(lot - 0.5) < 1e-9, f"{lot}")

lot_zero = calculate_lot_size(balance=10000, risk_percent=1.0, sl_distance=0,
                               tick_value=1.0, tick_size=0.00001, min_lot=0.01, max_lot=100.0, lot_step=0.01)
check("8b zero SL distance -> 0.0 lot (not a crash)", lot_zero == 0.0)

lot_floor = calculate_lot_size(balance=100, risk_percent=1.0, sl_distance=0.0020,
                                tick_value=1.0, tick_size=0.00001, min_lot=0.01, max_lot=100.0, lot_step=0.01)
check("8c below min lot -> floored up to min_lot", lot_floor == 0.01, f"{lot_floor}")

print(); print("="*78); print("SECTION 9 — build_signal (SL/TP pricing)"); print("="*78)

cfg = ThreeStepConfig()
bull_zone = Zone(top=1.1010, bottom=1.1000, form_index=5, form_time=ts(5), direction=1, is_ob=True)
sig = build_signal(cfg, bull_zone, "EURUSD", bid=1.1005, ask=1.1006)
expected_sl = 1.1000 - 50 * 0.00001
expected_risk = 1.1006 - expected_sl
expected_tp = 1.1006 + 2.0 * expected_risk
check("9a bullish signal: entry=ask, SL=zone.bottom-buffer, TP=RR*risk",
      abs(sig.entry_price - 1.1006) < 1e-9 and abs(sig.stop_loss - expected_sl) < 1e-9 and abs(sig.take_profit - expected_tp) < 1e-9,
      f"{sig}")

bear_zone = Zone(top=1.1010, bottom=1.1000, form_index=5, form_time=ts(5), direction=-1, is_ob=True)
sig_bear = build_signal(cfg, bear_zone, "EURUSD", bid=1.1004, ask=1.1005)
expected_sl_bear = 1.1010 + 50 * 0.00001
check("9b bearish signal: entry=bid, SL=zone.top+buffer",
      abs(sig_bear.entry_price - 1.1004) < 1e-9 and abs(sig_bear.stop_loss - expected_sl_bear) < 1e-9, f"{sig_bear}")

print(); print("="*78); print("SECTION 10 — ThreeStepEA state machine"); print("="*78)

# Build a full synthetic 3-timeframe scenario: H1 uptrend, an M15 OB the
# price is currently sitting in, then an M5 CHoCH to trigger entry.
ea_cfg = ThreeStepConfig(swing_lookback_h1=20, zone_lookback_m15=15, choch_lookback_m5=15, fractal_wing=2)
ea = ThreeStepEA(ea_cfg, "EURUSD")
h1_ema_for_ea = ema_series([c.close for c in up_candles], ea_cfg.ema_period if ea_cfg.ema_period <= len(up_candles) else 5)
# use a short EMA period compatible with the fixture length
h1_ema_for_ea = ema_series([c.close for c in up_candles], 5)

sig1 = ea.on_m5_bar(up_candles, len(up_candles)-1, h1_ema_for_ea,
                     m15, 10, m5, len(m5)-2, current_price_bid=1.0989, current_price_ask=1.0991)
check("10a first bar with a valid zone in range -> transitions to WAIT_CHOCH, no signal yet",
      sig1 is None and ea.state == EAState.WAIT_CHOCH, f"state={ea.state}")

sig2 = ea.on_m5_bar(up_candles, len(up_candles)-1, h1_ema_for_ea,
                     m15, 10, m5, len(m5)-1, current_price_bid=1.1017, current_price_ask=1.1019)
check("10b CHoCH bar produces a signal and resets to WAIT_SETUP",
      sig2 is not None and ea.state == EAState.WAIT_SETUP, f"sig={sig2} state={ea.state}")

# Timeout path: zone found, then CHoCH never comes within zone_timeout_m5_bars.
ea2 = ThreeStepEA(ThreeStepConfig(swing_lookback_h1=20, zone_lookback_m15=15, choch_lookback_m5=15,
                                   fractal_wing=2, zone_timeout_m5_bars=2), "EURUSD")
flat_m5 = [C(i, 1.1000, 1.1003, 1.0997, 1.1000, tf=Timeframe.M5) for i in range(10)]
ea2.on_m5_bar(up_candles, len(up_candles)-1, h1_ema_for_ea, m15, 10, flat_m5, 3,
              current_price_bid=1.0989, current_price_ask=1.0991)
check("10c zone acquired -> WAIT_CHOCH", ea2.state == EAState.WAIT_CHOCH)
ea2.on_m5_bar(up_candles, len(up_candles)-1, h1_ema_for_ea, m15, 10, flat_m5, 4, 1.0989, 1.0991)
ea2.on_m5_bar(up_candles, len(up_candles)-1, h1_ema_for_ea, m15, 10, flat_m5, 5, 1.0989, 1.0991)
sig3 = ea2.on_m5_bar(up_candles, len(up_candles)-1, h1_ema_for_ea, m15, 10, flat_m5, 6, 1.0989, 1.0991)
check("10d no CHoCH within timeout -> resets to WAIT_SETUP, no signal", sig3 is None and ea2.state == EAState.WAIT_SETUP)

# Trend-loss reset path.
ea3 = ThreeStepEA(ThreeStepConfig(swing_lookback_h1=20, zone_lookback_m15=15, choch_lookback_m5=15, fractal_wing=2), "EURUSD")
ea3.on_m5_bar(up_candles, len(up_candles)-1, h1_ema_for_ea, m15, 10, flat_m5, 3, 1.0989, 1.0991)
check("10e zone acquired before trend loss", ea3.state == EAState.WAIT_CHOCH)
ea3.on_m5_bar(flat_candles, len(flat_candles)-1, flat_ema, m15, 10, flat_m5, 4, 1.0989, 1.0991)
check("10f trend lost (flat H1) -> resets to WAIT_SETUP", ea3.state == EAState.WAIT_SETUP)

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
