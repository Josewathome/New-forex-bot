"""Regression test for src/application/strategy_v2/numerics.py — the
shared epsilon-tolerant comparison / range-intersection helpers extracted
from atr.py/fvg.py/order_blocks.py in the Phase 2B cleanup.

Deterministic, hand-built fixtures (matching this repo's existing
tools/validate_*.py convention) covering: exact-boundary behavior at
+/-EPSILON, reflexivity/symmetry/monotonicity at representative values,
and the specific formulas this module replaced (is_displacement's
tolerance, the FVG min-size gate, OB mitigation/invalidation, ob_fvg_
confluence classification) so a future edit to numerics.py that silently
changes any of these semantics is caught immediately.

A one-time, stronger property-based equivalence audit (thousands of
randomized trials, including direct comparison against the pre-refactor
inline formulas extracted from git commit a666e49) was run separately to
certify this extraction was behavior-preserving — see
docs/STRATEGY_V2_PHASE2_FREEZE_AUDIT.md. This suite is the permanent,
fast, dependency-free regression check kept going forward.

HOW TO RUN
    python3 tools/validate_numerics.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from src.application.strategy_v2.numerics import EPSILON, approx_gte, approx_lte, approx_eq, intersect

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

print("="*78); print("SECTION 1 — EXACT BOUNDARY BEHAVIOR (a == b)"); print("="*78)

check("approx_gte(1.0, 1.0) is True (exact equality)", approx_gte(1.0, 1.0) is True)
check("approx_lte(1.0, 1.0) is True (exact equality)", approx_lte(1.0, 1.0) is True)
check("approx_eq(1.0, 1.0) is True (exact equality)", approx_eq(1.0, 1.0) is True)

print(); print("="*78); print("SECTION 2 — WITHIN EPSILON (must be treated as equal)"); print("="*78)

check("approx_gte(1.0, 1.0 + EPSILON/2) is True (a slightly < b, within tolerance)",
      approx_gte(1.0, 1.0 + EPSILON / 2) is True)
check("approx_lte(1.0, 1.0 - EPSILON/2) is True (a slightly > b, within tolerance)",
      approx_lte(1.0, 1.0 - EPSILON / 2) is True)
check("approx_eq(1.0, 1.0 + EPSILON/2) is True", approx_eq(1.0, 1.0 + EPSILON / 2) is True)
check("approx_gte(1.0, 1.0 + EPSILON) is True (exactly at the tolerance edge, inclusive)",
      approx_gte(1.0, 1.0 + EPSILON) is True)

print(); print("="*78); print("SECTION 3 — BEYOND EPSILON (must NOT be treated as equal)"); print("="*78)

check("approx_gte(1.0, 1.0 + 2*EPSILON) is False (clearly less than b)",
      approx_gte(1.0, 1.0 + 2 * EPSILON) is False)
check("approx_lte(1.0, 1.0 - 2*EPSILON) is False (clearly greater than b)",
      approx_lte(1.0, 1.0 - 2 * EPSILON) is False)
check("approx_eq(1.0, 1.0 + 2*EPSILON) is False", approx_eq(1.0, 1.0 + 2 * EPSILON) is False)
check("approx_gte(0.9999, 1.0) is False (far below)", approx_gte(0.9999, 1.0) is False)

print(); print("="*78); print("SECTION 4 — DECIMAL-FRACTION FLOATING-POINT CASE (the original bug motivating EPSILON)"); print("="*78)

# 1.1010 - 1.1005 == 0.0004999999999999449 in raw binary float arithmetic,
# not 0.0005 -- a naive `>=` would wrongly reject a genuine exact-boundary
# case. This is the literal motivating example from
# docs/STRATEGY_V2_ARCHITECTURE.md invariant #7.
raw_diff = 1.1010 - 1.1005
check("raw float subtraction is NOT exactly 0.0005 (confirms the motivating bug still reproduces)",
      raw_diff != 0.0005, f"raw_diff={raw_diff!r}")
check("approx_gte(raw_diff, 0.0005) is True (epsilon tolerance rescues the exact-boundary case)",
      approx_gte(raw_diff, 0.0005) is True)

print(); print("="*78); print("SECTION 5 — GENERAL PROPERTIES (reflexivity, symmetry, monotonicity)"); print("="*78)

for val in [0.0, 1.0, -1.0, 1.1005, 4000.00, 150.000, 1e6]:
    check(f"approx_gte({val}, {val}) reflexive", approx_gte(val, val) is True)
    check(f"approx_lte({val}, {val}) reflexive", approx_lte(val, val) is True)
    check(f"approx_eq({val}, {val}) reflexive", approx_eq(val, val) is True)

check("intersect(1,5,3,8) == intersect(3,8,1,5) (symmetric)",
      intersect(1, 5, 3, 8) == intersect(3, 8, 1, 5))
check("intersect(1,5,3,8) == (3,5) (correct max-low/min-high)",
      intersect(1, 5, 3, 8) == (3, 5))
check("intersect(1,2,5,6) == (5,2) (disjoint ranges: max(1,5)=5, min(2,6)=2, non-overlapping signature preserved)",
      intersect(1, 2, 5, 6) == (5, 2))

# monotonicity: if approx_gte(a,b) holds, increasing a can only keep it True
check("monotonicity: approx_gte(5.0, 3.0) True, approx_gte(10.0, 3.0) also True",
      approx_gte(5.0, 3.0) and approx_gte(10.0, 3.0))

print(); print("="*78); print("SECTION 6 — SPECIFIC FORMULAS THIS MODULE REPLACED"); print("="*78)

# is_displacement's tolerance: body_size >= multiplier*atr, epsilon-inclusive
body, multiplier, atr_val = 2.0, 2.0, 1.0000000005  # multiplier*atr = 2.000000001, body slightly below
check("is_displacement-style boundary: approx_gte(2.0, 2.0*1.0000000005) is True (within EPSILON)",
      approx_gte(body, multiplier * atr_val) is True)

# FVG min-size gate: approx_gte(gap, min_size) at exact minimum
min_size = 0.0005
check("FVG-gate-style: approx_gte(min_size, min_size) is True (gap exactly at minimum: created)",
      approx_gte(min_size, min_size) is True)
check("FVG-gate-style: approx_gte(min_size - 2*EPSILON, min_size) is False (gap clearly below minimum: rejected)",
      approx_gte(min_size - 2 * EPSILON, min_size) is False)

# OB mitigation: approx_lte(candle.low, zone.high) — inclusive touch at the near edge
zone_high = 1.1010
check("OB-mitigation-style: approx_lte(zone_high, zone_high) is True (exact touch: mitigated)",
      approx_lte(zone_high, zone_high) is True)

# ob_fvg_confluence classification thresholds (positive_width vs boundary_touch vs none)
lo, hi = intersect(1.1000, 1.1010, 1.1010, 1.1020)  # OB and FVG share exactly one boundary price
check("ob_fvg_confluence-style: adjacent ranges sharing exactly one price -> intersect gives lo==hi (boundary_touch case)",
      lo == hi == 1.1010)
lo2, hi2 = intersect(1.1000, 1.1010, 1.1005, 1.1020)  # genuine overlap
check("ob_fvg_confluence-style: genuinely overlapping ranges -> intersect gives lo<hi (positive_width case)",
      lo2 < hi2 and (lo2, hi2) == (1.1005, 1.1010))
lo3, hi3 = intersect(1.1000, 1.1010, 1.1020, 1.1030)  # no shared price at all
check("ob_fvg_confluence-style: disjoint ranges -> intersect gives lo>hi (none case)",
      lo3 > hi3)

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
