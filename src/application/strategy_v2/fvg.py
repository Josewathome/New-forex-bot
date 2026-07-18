# src/application/strategy_v2/fvg.py
#
# Fair Value Gap detection, mitigation, and invalidation — V4 spec §6.
#
#   Bullish FVG: Candle1.High < Candle3.Low.  Gap = [Candle1.High, Candle3.Low].
#   Bearish FVG: Candle1.Low > Candle3.High.  Gap = [Candle3.High, Candle1.Low].
#   Displacement gate on Candle2 (atr.is_displacement, HTF multiplier 2.0).
#   Minimum size: 5 pips forex/JPY (0.05 JPY), 50 points/5 pips XAUUSD.
#
#   Mitigation: price WICK reaches the 50% midpoint — no body close
#   required (explicit ruling: "A wick touch is sufficient.").
#   Invalidation: a candle BODY closes beyond the FAR side of the gap
#   (the side deeper from current price — gap_low for bullish, gap_high
#   for bearish). Distinct, one-way flags; neither ever resets.

from __future__ import annotations
from typing import Optional, Tuple

from src.domain.entities import Candle, Direction, Timeframe
from src.domain.strategy_v2_entities import FVGZone
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.atr import is_displacement
from src.application.strategy_v2.numerics import approx_gte, intersect


def min_fvg_size(symbol: str) -> float:
    return config_v2.FVG_MIN_SIZE_PIPS * config_v2.pip_size(symbol)


def detect_fvg(
    symbol: str, timeframe: Timeframe, c1: Candle, c2: Candle, c3: Candle, atr_at_c2: Optional[float],
) -> Optional[FVGZone]:
    """c1, c2, c3 must be three consecutive closed candles (c2 = the
    displacement candle). Returns None if no gap, displacement fails, or
    the gap is smaller than the per-symbol minimum.

    IMPORTANT — atr_at_c2 must be the ATR value computed THROUGH C1 (i.e.
    atr_series[index_of_c1]), NOT through C2. The spec measures
    displacement against "ATR of the PREVIOUS 10 candles" — a value that
    already includes C2's own True Range would be self-referential (C2's
    own volatility would inflate the very baseline used to judge it), and
    would make an exact-boundary test (body == 2.0 x ATR) circular and
    unconstructible. Callers must pass the pre-C2 ATR value."""
    if c1.high < c3.low:
        direction, gap_low, gap_high = Direction.BULLISH, c1.high, c3.low
    elif c1.low > c3.high:
        direction, gap_low, gap_high = Direction.BEARISH, c3.high, c1.low
    else:
        return None

    if not is_displacement(c2, atr_at_c2, config_v2.DISPLACEMENT_MULTIPLIER_HTF):
        return None

    if not approx_gte(gap_high - gap_low, min_fvg_size(symbol)):
        return None

    return FVGZone(
        symbol=symbol, timeframe=timeframe, direction=direction,
        gap_high=gap_high, gap_low=gap_low, created_at=c3.time,
    )


def update_fvg_state(zone: FVGZone, candle: Candle) -> None:
    """Call once per new candle after the zone's creation (chronological
    order matters — these are one-way transitions). Mutates in place."""
    if not zone.mitigated:
        midpoint = zone.midpoint
        if zone.direction == Direction.BULLISH:
            if candle.low <= midpoint:
                zone.mitigated = True
        else:
            if candle.high >= midpoint:
                zone.mitigated = True

    if not zone.invalidated:
        if zone.direction == Direction.BULLISH:
            if candle.close < zone.gap_low:
                zone.invalidated = True
        else:
            if candle.close > zone.gap_high:
                zone.invalidated = True


def is_tradable(zone: FVGZone) -> bool:
    """Mitigated FVGs are no longer tradable (spec). Invalidated ones
    never were valid POIs to begin with once broken."""
    return not zone.mitigated and not zone.invalidated


def overlap_range(a_low: float, a_high: float, b_low: float, b_high: float) -> Optional[Tuple[float, float]]:
    """Intersection of two price ranges, or None if they don't overlap
    (strict positive-width only — no epsilon tolerance here, unchanged
    from the original implementation). Needed later for FVG-vs-FVG
    overlap tracking."""
    lo, hi = intersect(a_low, a_high, b_low, b_high)
    return (lo, hi) if lo < hi else None
