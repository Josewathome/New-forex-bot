"""Regression test for src/application/strategy_v2/poi_manager.py against
the test matrix: storage (FVG/OB/liquidity), active/expired queries,
mitigation/invalidation propagation via update(), and CHoCH cleanup.

HOW TO RUN
    python3 tools/validate_poi_manager.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Candle, Direction, Timeframe
from src.domain.strategy_v2_entities import FVGZone, OrderBlockZone, LiquidityLevel, LiquidityLevelType, TrendState
from src.application.strategy_v2.poi_manager import POIManager

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
def ts(n): return T0 + timedelta(hours=n)
def C(n, o, h, l, c, sym="EURUSD"):
    return Candle(time=ts(n), open=o, high=h, low=l, close=c, volume=100.0, symbol=sym, timeframe=Timeframe.H1)

print("="*78); print("SECTION 1 — STORAGE"); print("="*78)

mgr = POIManager("EURUSD")
fvg1 = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
               gap_high=1.1010, gap_low=1.1005, created_at=ts(5))
ob1 = OrderBlockZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                      high=1.1005, low=1.1000, created_at=ts(5))
liq1 = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050,
                       primary_timeframe=Timeframe.H1, created_at=ts(6))
mgr.add_fvg(fvg1)
mgr.add_ob(ob1)
mgr.add_liquidity(liq1)
check("1a FVG stored", len(mgr.fvg_records) == 1 and mgr.fvg_records[0].zone is fvg1)
check("1b OB stored", len(mgr.ob_records) == 1 and mgr.ob_records[0].zone is ob1)
check("1c liquidity level stored", len(mgr.liquidity_levels) == 1 and mgr.liquidity_levels[0] is liq1)

print(); print("="*78); print("SECTION 2 — ACTIVE / EXPIRED QUERIES (fresh zones)"); print("="*78)

check("2a fresh FVG is active", fvg1 in mgr.active_fvgs())
check("2b fresh FVG is not expired", fvg1 not in mgr.expired_fvgs())
check("2c fresh OB is active", ob1 in mgr.active_obs())
check("2d fresh liquidity level is active as-of a time after its creation", liq1 in mgr.active_liquidity(ts(10)))
check("2e fresh liquidity level is NOT active as-of a time before its creation", liq1 not in mgr.active_liquidity(ts(3)))

print(); print("="*78); print("SECTION 3 — update() PROPAGATES MITIGATION/INVALIDATION/SWEEP"); print("="*78)

# Mitigate the FVG (midpoint touch), mitigate the OB (near-edge touch), sweep the liquidity level.
mitigate_candle = C(10, 1.1030, 1.1032, 1.10075 - 0.0001, 1.1028)  # wick reaches FVG midpoint (1.10075)
mgr.update(mitigate_candle)
check("3a FVG mitigated after update() with a midpoint-touching candle", fvg1.mitigated is True)
check("3b mitigated FVG no longer in active_fvgs()", fvg1 not in mgr.active_fvgs())
check("3c mitigated FVG now in expired_fvgs()", fvg1 in mgr.expired_fvgs())

ob_touch_candle = C(11, 1.1010, 1.1012, 1.1004, 1.1006)  # low <= ob.high (1.1005): mitigates bullish OB
mgr.update(ob_touch_candle)
check("3d OB mitigated after update()", ob1.mitigated is True)
check("3e mitigated OB no longer in active_obs()", ob1 not in mgr.active_obs())

sweep_candle = C(12, 1.1045, 1.1055, 1.1040, 1.1042)  # wick above 1.1050 by 5 pips, closes back below: sweep
mgr.update(sweep_candle)
check("3f liquidity level swept after update()", liq1.swept is True)
check("3g swept liquidity level no longer in active_liquidity()", liq1 not in mgr.active_liquidity(ts(20)))
check("3h swept liquidity level now in expired_liquidity()", liq1 in mgr.expired_liquidity())

print(); print("="*78); print("SECTION 4 — CHoCH CLEANUP (M4)"); print("="*78)

mgr2 = POIManager("EURUSD")
bull_fvg = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                    gap_high=1.1010, gap_low=1.1005, created_at=ts(5))
bear_fvg = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BEARISH,
                    gap_high=1.0990, gap_low=1.0980, created_at=ts(6))
bull_ob = OrderBlockZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
                          high=1.1005, low=1.1000, created_at=ts(5))
bear_ob = OrderBlockZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BEARISH,
                          high=1.0995, low=1.0990, created_at=ts(6))
liq2 = LiquidityLevel(symbol="EURUSD", kind=LiquidityLevelType.ASIA_HIGH, price=1.1050,
                       primary_timeframe=Timeframe.H1, created_at=ts(6))
for z in (bull_fvg, bear_fvg):
    mgr2.add_fvg(z)
for z in (bull_ob, bear_ob):
    mgr2.add_ob(z)
mgr2.add_liquidity(liq2)

mgr2.on_choch(TrendState.BULLISH)  # a bullish trend just got invalidated
check("4a BULLISH-invalidating CHoCH expires the bullish FVG", bull_fvg not in mgr2.active_fvgs())
check("4b BULLISH-invalidating CHoCH expires the bullish OB", bull_ob not in mgr2.active_obs())
check("4c BULLISH-invalidating CHoCH leaves the bearish FVG untouched (still active)", bear_fvg in mgr2.active_fvgs())
check("4d BULLISH-invalidating CHoCH leaves the bearish OB untouched (still active)", bear_ob in mgr2.active_obs())
check("4e CHoCH cleanup does NOT touch liquidity levels (trend-independent)", liq2 in mgr2.active_liquidity(ts(10)))

# Now invalidate the bearish trend too -- both should now be expired.
mgr2.on_choch(TrendState.BEARISH)
check("4f subsequent BEARISH-invalidating CHoCH also expires the bearish FVG", bear_fvg not in mgr2.active_fvgs())
check("4g subsequent BEARISH-invalidating CHoCH also expires the bearish OB", bear_ob not in mgr2.active_obs())

# NEUTRAL is a no-op, not an error.
mgr3 = POIManager("EURUSD")
fvg3 = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
               gap_high=1.1010, gap_low=1.1005, created_at=ts(5))
mgr3.add_fvg(fvg3)
mgr3.on_choch(TrendState.NEUTRAL)
check("4h on_choch(NEUTRAL) is a safe no-op", fvg3 in mgr3.active_fvgs())

# Already-mitigated zones are not affected by CHoCH cleanup logic crashing on them.
mgr4 = POIManager("EURUSD")
fvg4 = FVGZone(symbol="EURUSD", timeframe=Timeframe.H1, direction=Direction.BULLISH,
               gap_high=1.1010, gap_low=1.1005, created_at=ts(5), mitigated=True)
mgr4.add_fvg(fvg4)
mgr4.on_choch(TrendState.BULLISH)  # should not raise, should simply leave it (already inactive via mitigation)
check("4i CHoCH cleanup on an already-mitigated zone doesn't raise and zone stays inactive",
      fvg4 not in mgr4.active_fvgs())

print(); print("="*78); print("SECTION 5 — EXPIRED ZONES PRESERVED, NOT DELETED (audit trail)"); print("="*78)

check("5a expired FVG is still present in fvg_records (not deleted)",
      any(r.zone is bull_fvg for r in mgr2.fvg_records))
check("5b expired OB is still present in ob_records (not deleted)",
      any(r.zone is bull_ob for r in mgr2.ob_records))

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
