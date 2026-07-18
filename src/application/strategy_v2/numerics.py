# src/application/strategy_v2/numerics.py
#
# Shared floating-point-tolerant comparison helpers and range-intersection
# math, factored out of atr.py/fvg.py/order_blocks.py (Phase 2B technical
# debt cleanup — pure extraction, no behavior change; every call site's
# existing comparison semantics are preserved exactly).
#
# INVARIANT: binary floating-point cannot represent most decimal price
# fractions exactly (e.g. 1.1010 - 1.1005 evaluates to
# 0.0004999999999999449, not 0.0005) — an exact-boundary comparison would
# otherwise be wrongly rejected by a raw `<`/`>`/`==`. EPSILON is far below
# any real tick size (smallest in this system is XAUUSD's 0.01) so it only
# ever absorbs float-representation noise, never a genuine sub-tolerance
# difference. This tolerance is required at every boundary comparison
# throughout strategy_v2 — not just where it happens to be used today.

from __future__ import annotations
from typing import Tuple

EPSILON = 1e-9


def approx_gte(a: float, b: float, epsilon: float = EPSILON) -> bool:
    """a >= b, tolerant of floating-point representation noise."""
    return a >= b - epsilon


def approx_lte(a: float, b: float, epsilon: float = EPSILON) -> bool:
    """a <= b, tolerant of floating-point representation noise."""
    return a <= b + epsilon


def approx_eq(a: float, b: float, epsilon: float = EPSILON) -> bool:
    """a == b, tolerant of floating-point representation noise."""
    return abs(a - b) <= epsilon


def intersect(a_low: float, a_high: float, b_low: float, b_high: float) -> Tuple[float, float]:
    """Raw intersection bounds of two ranges: (max of lows, min of highs).
    Always returns a pair — callers decide what counts as "overlapping"
    (strict positive-width, inclusive/boundary-touch, or epsilon-tolerant)
    since that semantic genuinely differs by call site and must not be
    unified away."""
    return max(a_low, b_low), min(a_high, b_high)
