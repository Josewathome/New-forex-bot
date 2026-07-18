# src/application/strategy_v2/premium_discount.py
#
# Premium/Discount classification off the active External Swing Pair.
# Purely geometric: a price or zone's position within [pair.low, pair.high]
# classifies it as discount (below equilibrium), premium (above), or
# exactly equilibrium. No FVG/OB/liquidity coupling here -- this module
# only reads ExternalSwingPair, it does not know about zones' internal
# shape (callers pass a plain (low, high) pair for zone classification).
#
# TREND ALIGNMENT (locked in the original plan synthesis, "Phase 2 —
# poi.py"): a BULLISH pair's tradeable/favorable POI region is DISCOUNT
# (buy low, relative to the up-swing that produced the active BOS); a
# BEARISH pair's favorable region is PREMIUM (sell high). This module
# exposes that directly (favorable_zone()/is_in_favorable_zone()) so every
# future caller doesn't have to re-derive the same trend/zone mapping.
#
# IMPORTANT — ExternalSwingPair.low/.high are NOT guaranteed low <= high.
# They are labels for "the swing-LOW-type member" and "the swing-HIGH-type
# member" of the pair (per structure.py's own docstring: "BULLISH means
# `high` was broken and `low` is the swing that generated the breakout;
# BEARISH mirrors") -- not sorted range bounds. Confirmed on real data
# (EURUSD 2026-06-02, a genuine `_try_continue_bearish` pair): in choppy/
# ranging conditions the swing-low member's PRICE can sit above the
# swing-high member's price, because D2 pairs the most-recently-confirmed
# opposite-type swing chronologically, not the numerically lowest/highest
# one. structure.py is unchanged and behaving exactly as designed and
# proven (see STRATEGY_V2_PHASE2_FREEZE_AUDIT.md) -- this is simply the
# first consumer that needs an ordered Fibonacci range from the pair, so
# every function here anchors to (min(low,high), max(low,high)), never
# raw .low/.high directly.

from __future__ import annotations
from enum import Enum
from typing import Tuple

from src.domain.strategy_v2_entities import ExternalSwingPair, TrendState
from src.application.strategy_v2.numerics import approx_eq


class PriceZone(Enum):
    PREMIUM = "premium"
    DISCOUNT = "discount"
    EQUILIBRIUM = "equilibrium"


def _range(pair: ExternalSwingPair) -> Tuple[float, float]:
    """The pair's ordered Fibonacci range -- see the module-level note on
    why raw pair.low/pair.high cannot be used directly."""
    return (pair.low, pair.high) if pair.low <= pair.high else (pair.high, pair.low)


def equilibrium(pair: ExternalSwingPair) -> float:
    """The 50% midpoint of the active pair's ordered range."""
    lo, hi = _range(pair)
    return (lo + hi) / 2.0


def midpoint(pair: ExternalSwingPair) -> float:
    """Alias for equilibrium(), matching the `.midpoint` naming convention
    already used on FVGZone/OrderBlockZone -- same value, named for the
    audience that thinks in "midpoint" rather than "equilibrium"."""
    return equilibrium(pair)


def fib_level(pair: ExternalSwingPair, ratio: float) -> float:
    """Anchors a Fibonacci ratio to the pair's ordered range: ratio=0.0 ->
    the range's low bound, ratio=1.0 -> the range's high bound, ratio=0.5
    -> equilibrium. `ratio` outside [0, 1] is a valid extension/
    projection, not rejected."""
    lo, hi = _range(pair)
    return lo + ratio * (hi - lo)


def discount_zone(pair: ExternalSwingPair) -> Tuple[float, float]:
    """[range_low, equilibrium] -- the cheaper half of the ordered range."""
    lo, _ = _range(pair)
    return lo, equilibrium(pair)


def premium_zone(pair: ExternalSwingPair) -> Tuple[float, float]:
    """[equilibrium, range_high] -- the more expensive half of the ordered range."""
    _, hi = _range(pair)
    return equilibrium(pair), hi


def classify_price(pair: ExternalSwingPair, price: float) -> PriceZone:
    """Classifies a single price relative to the pair's range. Exactly at
    equilibrium (epsilon-tolerant, via numerics.approx_eq) is its own
    distinct classification -- neither zone is silently preferred at the
    boundary."""
    eq = equilibrium(pair)
    if approx_eq(price, eq):
        return PriceZone.EQUILIBRIUM
    return PriceZone.PREMIUM if price > eq else PriceZone.DISCOUNT


def classify_zone(pair: ExternalSwingPair, zone_low: float, zone_high: float) -> PriceZone:
    """Classifies a price RANGE (e.g. an FVG/OB/liquidity level) by its own
    midpoint's position relative to the pair's equilibrium -- the surface
    the future POI Manager uses to decide whether a zone sits in the
    favorable half."""
    return classify_price(pair, (zone_low + zone_high) / 2.0)


def favorable_zone(pair: ExternalSwingPair) -> PriceZone:
    """The trend-aligned tradeable half: DISCOUNT for a BULLISH pair (buy
    low), PREMIUM for a BEARISH pair (sell high). ExternalSwingPair.trend
    is always BULLISH or BEARISH by construction (structure.py never
    creates a NEUTRAL active pair) -- a NEUTRAL trend here indicates a
    caller passed a pair that shouldn't exist, not a valid classification,
    so it raises rather than silently guessing."""
    if pair.trend == TrendState.BULLISH:
        return PriceZone.DISCOUNT
    if pair.trend == TrendState.BEARISH:
        return PriceZone.PREMIUM
    raise ValueError(f"ExternalSwingPair.trend must be BULLISH or BEARISH, got {pair.trend!r}")


def is_in_favorable_zone(pair: ExternalSwingPair, zone_low: float, zone_high: float) -> bool:
    """True if the given zone's own classification (by midpoint) matches
    the pair's trend-aligned favorable half."""
    return classify_zone(pair, zone_low, zone_high) == favorable_zone(pair)
