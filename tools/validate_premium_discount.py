"""Regression test for src/application/strategy_v2/premium_discount.py
against the test matrix: equilibrium/midpoint, Fib anchoring, premium/
discount zone bounds, price/zone classification (incl. exact-boundary),
and trend-aligned favorable-zone mapping.

HOW TO RUN
    python3 tools/validate_premium_discount.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timezone
from src.domain.entities import Timeframe
from src.domain.strategy_v2_entities import ExternalSwingPair, SwingPoint, SwingType, TrendState
from src.application.strategy_v2.premium_discount import (
    PriceZone, equilibrium, midpoint, fib_level, discount_zone, premium_zone,
    classify_price, classify_zone, favorable_zone, is_in_favorable_zone,
)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

def mk_swing(kind, price, idx=0):
    return SwingPoint(symbol="EURUSD", timeframe=Timeframe.H1, kind=kind, price=price, candle_time=T0, confirmed_at_index=idx)

def mk_pair(low, high, trend):
    return ExternalSwingPair(
        symbol="EURUSD", trend=trend, low=low, high=high,
        low_swing=mk_swing(SwingType.LOW, low), high_swing=mk_swing(SwingType.HIGH, high), bos_time=T0,
    )

print("="*78); print("SECTION 1 — EQUILIBRIUM / MIDPOINT"); print("="*78)

pair_bull = mk_pair(1.1000, 1.1100, TrendState.BULLISH)
check("1a equilibrium is the exact midpoint", abs(equilibrium(pair_bull) - 1.1050) < 1e-9, f"{equilibrium(pair_bull)}")
check("1b midpoint() is an alias for equilibrium()", midpoint(pair_bull) == equilibrium(pair_bull))

print(); print("="*78); print("SECTION 2 — FIB ANCHORING"); print("="*78)

check("2a fib_level(0.0) == pair.low", abs(fib_level(pair_bull, 0.0) - 1.1000) < 1e-9)
check("2b fib_level(1.0) == pair.high", abs(fib_level(pair_bull, 1.0) - 1.1100) < 1e-9)
check("2c fib_level(0.5) == equilibrium", abs(fib_level(pair_bull, 0.5) - equilibrium(pair_bull)) < 1e-9)
check("2d fib_level(0.618) is between low and high, correctly proportioned",
      abs(fib_level(pair_bull, 0.618) - (1.1000 + 0.618 * 0.0100)) < 1e-9, f"{fib_level(pair_bull, 0.618)}")

print(); print("="*78); print("SECTION 3 — PREMIUM / DISCOUNT ZONE BOUNDS"); print("="*78)

check("3a discount_zone == (low, equilibrium)", discount_zone(pair_bull) == (1.1000, 1.1050))
check("3b premium_zone == (equilibrium, high)", premium_zone(pair_bull) == (1.1050, 1.1100))
d_lo, d_hi = discount_zone(pair_bull)
p_lo, p_hi = premium_zone(pair_bull)
check("3c discount and premium zones share exactly the equilibrium boundary, no gap/overlap",
      d_hi == p_lo, f"discount_hi={d_hi} premium_lo={p_lo}")

print(); print("="*78); print("SECTION 4 — PRICE CLASSIFICATION"); print("="*78)

check("4a price below equilibrium: DISCOUNT", classify_price(pair_bull, 1.1010) == PriceZone.DISCOUNT)
check("4b price above equilibrium: PREMIUM", classify_price(pair_bull, 1.1090) == PriceZone.PREMIUM)
check("4c price exactly at equilibrium: EQUILIBRIUM", classify_price(pair_bull, 1.1050) == PriceZone.EQUILIBRIUM)
check("4d price at pair.low: DISCOUNT", classify_price(pair_bull, 1.1000) == PriceZone.DISCOUNT)
check("4e price at pair.high: PREMIUM", classify_price(pair_bull, 1.1100) == PriceZone.PREMIUM)
check("4f price within EPSILON of equilibrium: EQUILIBRIUM (epsilon-tolerant boundary)",
      classify_price(pair_bull, 1.1050 + 1e-10) == PriceZone.EQUILIBRIUM)
check("4g price clearly (2*EPSILON) above equilibrium: PREMIUM, not EQUILIBRIUM",
      classify_price(pair_bull, 1.1050 + 2e-9) == PriceZone.PREMIUM)

print(); print("="*78); print("SECTION 5 — ZONE CLASSIFICATION (by midpoint)"); print("="*78)

check("5a zone entirely in discount half: DISCOUNT", classify_zone(pair_bull, 1.1005, 1.1015) == PriceZone.DISCOUNT)
check("5b zone entirely in premium half: PREMIUM", classify_zone(pair_bull, 1.1080, 1.1095) == PriceZone.PREMIUM)
check("5c zone straddling equilibrium symmetrically: EQUILIBRIUM (midpoint lands exactly on it)",
      classify_zone(pair_bull, 1.1040, 1.1060) == PriceZone.EQUILIBRIUM)
check("5d zone straddling but midpoint tilted into premium: PREMIUM",
      classify_zone(pair_bull, 1.1045, 1.1090) == PriceZone.PREMIUM)

print(); print("="*78); print("SECTION 6 — TREND-ALIGNED FAVORABLE ZONE"); print("="*78)

check("6a BULLISH pair: favorable zone is DISCOUNT (buy low)", favorable_zone(pair_bull) == PriceZone.DISCOUNT)
pair_bear = mk_pair(1.1000, 1.1100, TrendState.BEARISH)
check("6b BEARISH pair: favorable zone is PREMIUM (sell high)", favorable_zone(pair_bear) == PriceZone.PREMIUM)

try:
    pair_neutral = ExternalSwingPair(symbol="EURUSD", trend=TrendState.NEUTRAL, low=1.1000, high=1.1100,
                                      low_swing=mk_swing(SwingType.LOW, 1.1000), high_swing=mk_swing(SwingType.HIGH, 1.1100),
                                      bos_time=T0)
    favorable_zone(pair_neutral)
    check("6c NEUTRAL trend raises ValueError rather than silently guessing", False, "no exception raised")
except ValueError:
    check("6c NEUTRAL trend raises ValueError rather than silently guessing", True)

print(); print("="*78); print("SECTION 7 — is_in_favorable_zone INTEGRATION"); print("="*78)

check("7a BULLISH pair, zone in discount half: favorable", is_in_favorable_zone(pair_bull, 1.1005, 1.1015))
check("7b BULLISH pair, zone in premium half: NOT favorable", not is_in_favorable_zone(pair_bull, 1.1080, 1.1095))
check("7c BEARISH pair, zone in premium half: favorable", is_in_favorable_zone(pair_bear, 1.1080, 1.1095))
check("7d BEARISH pair, zone in discount half: NOT favorable", not is_in_favorable_zone(pair_bear, 1.1005, 1.1015))
check("7e BULLISH pair, zone exactly at equilibrium: NOT favorable (EQUILIBRIUM != DISCOUNT)",
      not is_in_favorable_zone(pair_bull, 1.1040, 1.1060))

print(); print("="*78); print("SECTION 8 — WIDE-SCALE RANGES (XAUUSD)"); print("="*78)

pair_wide = mk_pair(4000.00, 4050.00, TrendState.BULLISH)  # XAUUSD-scale range
check("8a XAUUSD-scale equilibrium computed correctly", abs(equilibrium(pair_wide) - 4025.00) < 1e-9)
check("8b XAUUSD-scale discount zone correct", discount_zone(pair_wide) == (4000.00, 4025.00))

print(); print("="*78); print("SECTION 9 — INVERTED PAIR (pair.low > pair.high numerically)"); print("="*78)
# ExternalSwingPair.low/.high are labels for "swing-low-type member" /
# "swing-high-type member" of the pair, NOT sorted range bounds -- confirmed
# on real data (see the module docstring). A BEARISH continuation pair in
# choppy conditions can legitimately have low > high numerically.

pair_inverted = mk_pair(1.16384, 1.16379, TrendState.BEARISH)  # low field > high field
check("9a equilibrium still lands strictly between the true min/max of the two prices",
      min(1.16379, 1.16384) <= equilibrium(pair_inverted) <= max(1.16379, 1.16384),
      f"eq={equilibrium(pair_inverted)}")
check("9b equilibrium is the simple average regardless of which field is which",
      abs(equilibrium(pair_inverted) - (1.16384 + 1.16379) / 2.0) < 1e-9)
check("9c fib_level(0.0) == the numerically LOWER of the two prices, not raw pair.low",
      abs(fib_level(pair_inverted, 0.0) - 1.16379) < 1e-9, f"{fib_level(pair_inverted, 0.0)}")
check("9d fib_level(1.0) == the numerically HIGHER of the two prices, not raw pair.high",
      abs(fib_level(pair_inverted, 1.0) - 1.16384) < 1e-9, f"{fib_level(pair_inverted, 1.0)}")
d_lo_i, d_hi_i = discount_zone(pair_inverted)
p_lo_i, p_hi_i = premium_zone(pair_inverted)
check("9e discount_zone/premium_zone are still correctly ordered (lo < hi) despite the inverted input fields",
      d_lo_i < d_hi_i and p_lo_i < p_hi_i, f"discount={d_lo_i,d_hi_i} premium={p_lo_i,p_hi_i}")
check("9f BEARISH inverted pair: favorable zone is still PREMIUM (trend-based, unaffected by the field inversion)",
      favorable_zone(pair_inverted) == PriceZone.PREMIUM)
check("9g classify_price still works correctly against the true ordered range",
      classify_price(pair_inverted, 1.16380) == PriceZone.DISCOUNT,
      f"got {classify_price(pair_inverted, 1.16380)}")

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
