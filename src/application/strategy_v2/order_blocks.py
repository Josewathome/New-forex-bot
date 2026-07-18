# src/application/strategy_v2/order_blocks.py
#
# Order Block detection, mitigation, invalidation — V4 spec §8, plus the
# OB Fallback Rule and the OB+FVG confluence semantics locked before
# implementation began.
#
#   Bullish OB: last bearish candle (close < open) immediately before a
#     bullish displacement move. Bearish OB mirrors.
#   Boundaries: FULL candle range (high to low) — not body, unlike FVG.
#   Shares the exact displacement gate FVG uses (atr.is_displacement, HTF
#   multiplier 2.0), off the same (c1, c2, atr_at_c2) inputs — OB and FVG
#   can never disagree about whether a displacement event occurred. OB does
#   NOT require an FVG gap to exist; it only needs c2's own direction.
#
#   Fallback Rule: if c1 is the same color as the displacement (not
#   opposite), search backward candle-by-candle up to 20 candles for the
#   nearest opposite-colored candle. None found within 20 -> OB detection
#   fails (FVG, resolved independently, is unaffected). A doji (close ==
#   open) is neither color and is skipped by the search.
#
#   Mitigation: "first touch" — ANY contact with the OB's range, not a
#   midpoint (stricter/earlier than FVG). Bullish OB: candle.low <= ob.high
#   (the near/top edge) already mitigates, and this single test also
#   covers a wick blowing straight through the whole zone.
#   Invalidation: body closes beyond the FAR side (ob.low for bullish,
#   ob.high for bearish) — geometrically implies mitigation already fired
#   on the same or an earlier bar, same relationship proven for FVG.
#   Retest: "maximum 1 mitigation" — mitigated is a one-way flag; once set,
#   the zone is permanently done regardless of what happens afterward.
#
#   OB+FVG confluence (locked decision): INCLUSIVE overlap (`<=`) — a
#   boundary-touch (the common adjacent OB-then-FVG shape, since OB is
#   usually the same candle as FVG's Candle1) counts as valid confluence,
#   but is explicitly tagged and never silently widened into a fake
#   positive-width range.
#
#   Expiry (HTF CHoCH) is deliberately NOT handled here — deferred to the
#   future POI-assembly layer, same scope boundary FVG already established.

from __future__ import annotations
from typing import List, Optional, Sequence, Tuple

from src.domain.entities import Candle, Direction, Timeframe
from src.domain.strategy_v2_entities import OrderBlockZone
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.atr import is_displacement

_EPSILON = 1e-9  # see fvg._EPSILON — same binary floating-point rationale


def _displacement_direction(c2: Candle) -> Optional[Direction]:
    if c2.close > c2.open:
        return Direction.BULLISH
    if c2.close < c2.open:
        return Direction.BEARISH
    return None  # doji; is_displacement's body_size==0 check already excludes this in practice


def _is_opposite_color(candle: Candle, displacement_direction: Direction) -> bool:
    """A doji (close == open) returns False either way — correctly skipped,
    never selected as an OB candle."""
    if displacement_direction == Direction.BULLISH:
        return candle.close < candle.open
    return candle.close > candle.open


def detect_order_block(
    symbol: str, timeframe: Timeframe, candles: Sequence[Candle], c1_index: int,
    c2: Candle, atr_at_c2: Optional[float],
) -> Optional[Tuple[OrderBlockZone, int]]:
    """candles[c1_index] must be the candle immediately before c2 (the
    displacement candle). Searches backward from c1_index for the nearest
    opposite-colored candle, up to config_v2.OB_MAX_SEARCH_CANDLES deep.
    Returns (zone, search_depth) — depth=1 means c1 itself qualified, no
    fallback needed — or None if displacement fails or no qualifying
    candle exists within the search window."""
    if not is_displacement(c2, atr_at_c2, config_v2.DISPLACEMENT_MULTIPLIER_HTF):
        return None

    direction = _displacement_direction(c2)
    if direction is None:
        return None

    max_depth = config_v2.OB_MAX_SEARCH_CANDLES
    for depth in range(1, max_depth + 1):
        idx = c1_index - (depth - 1)
        if idx < 0:
            break
        candle = candles[idx]
        if _is_opposite_color(candle, direction):
            zone = OrderBlockZone(
                symbol=symbol, timeframe=timeframe, direction=direction,
                high=candle.high, low=candle.low, created_at=candle.time,
            )
            return zone, depth
    return None


def update_ob_state(zone: OrderBlockZone, candle: Candle) -> None:
    """Call once per new candle after the zone's creation, in chronological
    order. One-way transitions — neither flag ever resets."""
    if not zone.mitigated:
        if zone.direction == Direction.BULLISH:
            if candle.low <= zone.high + _EPSILON:
                zone.mitigated = True
        else:
            if candle.high >= zone.low - _EPSILON:
                zone.mitigated = True

    if not zone.invalidated:
        if zone.direction == Direction.BULLISH:
            if candle.close < zone.low - _EPSILON:
                zone.invalidated = True
        else:
            if candle.close > zone.high + _EPSILON:
                zone.invalidated = True


def is_tradable(zone: OrderBlockZone) -> bool:
    """Mitigation alone already ends an OB's tradability ('maximum 1
    mitigation') — invalidated is retained for symmetric bookkeeping with
    FVG, but given the geometric relationship in update_ob_state, it can
    never be True while mitigated is False."""
    return not zone.mitigated and not zone.invalidated


ConfluenceKind = str  # "positive_width" | "boundary_touch"


def ob_fvg_confluence(
    ob_low: float, ob_high: float, fvg_low: float, fvg_high: float,
) -> Optional[Tuple[float, float, ConfluenceKind]]:
    """Inclusive overlap test for OB+FVG POI confluence (locked decision).
    Returns None if the two ranges share no price at all. Otherwise
    (lo, hi, kind): kind='positive_width' for a genuine overlapping range,
    kind='boundary_touch' for the common adjacent case (OB usually IS the
    FVG's own Candle1, sharing exactly one boundary price) — never
    silently widened into a fake positive-width range."""
    lo, hi = max(ob_low, fvg_low), min(ob_high, fvg_high)
    if lo < hi - _EPSILON:
        return (lo, hi, "positive_width")
    if abs(lo - hi) <= _EPSILON:
        return (lo, hi, "boundary_touch")
    return None
