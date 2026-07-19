# src/application/strategy_v2/liquidity.py
#
# Liquidity Sweep detection — V4 spec §11-12. Resting liquidity pools
# (Asian range, equal highs/lows, previous-day range) and their sweep
# lifecycle: penetration, confirmation (stop-hunt-then-reverse) vs
# invalidation (genuine breakout, not a grab), and multi-level tracking.
#
#   Buy-side liquidity: pools ABOVE price (buy-stop orders resting above a
#     high, from trapped short sellers) -- ASIA_HIGH, EQUAL_HIGH,
#     PREV_DAY_HIGH. Swept when price pushes UP through them.
#   Sell-side liquidity: pools BELOW price (sell-stop orders resting below
#     a low, from trapped long buyers) -- ASIA_LOW, EQUAL_LOW,
#     PREV_DAY_LOW. Swept when price pushes DOWN through them.
#
#   Sweep test: a wick must penetrate beyond the level by at least
#   config_v2.SWEEP_MIN_PENETRATION_PIPS (epsilon-tolerant at the exact
#   threshold, via numerics.approx_gte) -- this excludes negligible
#   touches from counting as a liquidity grab.
#
#   Reaction classification (recorded once, at the first qualifying
#   candle only -- "swept" is a one-way flag, mirrors FVG/OB mitigation):
#     'confirmed_reversal' -- body closes back on the resting side of the
#       level (the classic stop-hunt-then-reverse liquidity sweep).
#     'breakout' -- body also closes beyond the level -- a genuine
#       continuation, not a liquidity grab. Recorded, not discarded: a
#       POI/entry layer built later can require 'confirmed_reversal' and
#       treat 'breakout' as an invalidated sweep candidate.
#
#   DOCUMENTED IMPLEMENTATION DECISIONS (not explicit in a locked written
#   spec text kept in this repo -- reconstructed from the domain model
#   already scaffolded in strategy_v2_entities.py, this strategy's config_v2
#   constants, and standard SMC usage; flagged here per this module's own
#   comment discipline, not re-litigated as a pause):
#
#   L1. Asian-range levels (ASIA_HIGH/ASIA_LOW) are created ONCE per UTC
#       calendar day, at the first candle AFTER that day's Asian session
#       (config_v2.ASIAN_SESSION_UTC) has closed -- never mid-session,
#       which would look ahead at a session not yet complete.
#   L2. Previous-day levels (PREV_DAY_HIGH/PREV_DAY_LOW) are the FULLY
#       completed prior UTC day's high/low, created at the first candle of
#       the following day.
#   L3. Equal highs/lows are detected over CONFIRMED SWINGS (swings.py),
#       not raw candle extremes -- two same-kind confirmed swings within
#       config_v2.EQUAL_LEVEL_TOLERANCE_PIPS of each other and within
#       config_v2.EQUAL_LEVEL_LOOKBACK_DAYS (in H1 bars) of each other's
#       confirmation index. The level anchors at the MORE EXTREME of the
#       cluster (max price for highs, min for lows) -- liquidity actually
#       rests just beyond the true extreme, not at an averaged price. This
#       is an independent, standalone liquidity-POI concept, distinct from
#       (and not a reintroduction of) the REJECTED equal-hl swing-selection
#       tie-break in docs/STRATEGY_V2_REJECTED_RESEARCH.md #4 -- that idea
#       used equal-hl to change which swing the STRUCTURE ENGINE selects;
#       this uses it only to mark a liquidity target, with zero effect on
#       structure/BOS/CHoCH.
#   L4. "London sweep" is not a separate detection mechanism -- it is the
#       same general sweep test, with a helper to classify whether a given
#       sweep happened during config_v2.LONDON_SESSION_UTC. General sweep
#       detection is NOT restricted to London hours: the session window
#       gates confirmation/entry only, per the already-locked sequencing
#       decision (a sweep may occur during the Asian session that defines
#       the range in the first place, or at any other hour).
#   L5. Multi-sweep handling: many distinct LiquidityLevel instances (one
#       per day per type, plus however many equal-level clusters form) are
#       tracked concurrently and independently -- each with its own one-way
#       swept flag. active_unswept_levels() is the pool-management surface
#       a POI layer will query.

from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from src.domain.entities import Candle, Timeframe
from src.domain.strategy_v2_entities import LiquidityLevel, LiquidityLevelType, SwingPoint, SwingType
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.numerics import approx_gte, approx_lte

BUY_SIDE_TYPES = frozenset({
    LiquidityLevelType.ASIA_HIGH, LiquidityLevelType.EQUAL_HIGH, LiquidityLevelType.PREV_DAY_HIGH,
})
SELL_SIDE_TYPES = frozenset({
    LiquidityLevelType.ASIA_LOW, LiquidityLevelType.EQUAL_LOW, LiquidityLevelType.PREV_DAY_LOW,
})


def is_buy_side(kind: LiquidityLevelType) -> bool:
    return kind in BUY_SIDE_TYPES


def is_sell_side(kind: LiquidityLevelType) -> bool:
    return kind in SELL_SIDE_TYPES


def equal_level_tolerance(symbol: str) -> float:
    pips = (config_v2.EQUAL_LEVEL_TOLERANCE_PIPS_XAUUSD if symbol == "XAUUSD"
            else config_v2.EQUAL_LEVEL_TOLERANCE_PIPS)
    return pips * config_v2.pip_size(symbol)


def min_sweep_penetration(symbol: str) -> float:
    return config_v2.SWEEP_MIN_PENETRATION_PIPS * config_v2.pip_size(symbol)


def _utc_day(dt: datetime):
    return dt.astimezone(timezone.utc).date()


def _hour_in(dt: datetime, session: Tuple[int, int]) -> bool:
    start, end = session
    return start <= dt.astimezone(timezone.utc).hour < end


def is_asian_session(dt: datetime) -> bool:
    return _hour_in(dt, config_v2.ASIAN_SESSION_UTC)


def is_london_session(dt: datetime) -> bool:
    return _hour_in(dt, config_v2.LONDON_SESSION_UTC)


def detect_asia_levels(
    candles: Sequence[Candle], symbol: str, timeframe: Timeframe = Timeframe.H1,
) -> Tuple[List[LiquidityLevel], List[LiquidityLevel]]:
    """One ASIA_HIGH/ASIA_LOW pair per UTC calendar day, created at the
    first candle after that day's Asian session has closed (L1 -- no
    look-ahead into a session not yet complete). `timeframe` tags which
    candle series produced this level (spec §11: detected on M15 AND H1)."""
    highs: List[LiquidityLevel] = []
    lows: List[LiquidityLevel] = []
    current_day = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    for c in candles:
        day = _utc_day(c.time)
        if day != current_day:
            current_day = day
            day_high = day_low = None
        if is_asian_session(c.time):
            day_high = c.high if day_high is None else max(day_high, c.high)
            day_low = c.low if day_low is None else min(day_low, c.low)
        elif day_high is not None:
            highs.append(LiquidityLevel(symbol=symbol, kind=LiquidityLevelType.ASIA_HIGH, price=day_high,
                                         primary_timeframe=timeframe, created_at=c.time))
            lows.append(LiquidityLevel(symbol=symbol, kind=LiquidityLevelType.ASIA_LOW, price=day_low,
                                        primary_timeframe=timeframe, created_at=c.time))
            day_high = day_low = None  # emit once per day only
    return highs, lows


def detect_prev_day_levels(
    candles: Sequence[Candle], symbol: str, timeframe: Timeframe = Timeframe.H1,
) -> Tuple[List[LiquidityLevel], List[LiquidityLevel]]:
    """PREV_DAY_HIGH/PREV_DAY_LOW: the fully completed prior UTC day's
    high/low, created at the first candle of the following day (L2)."""
    highs: List[LiquidityLevel] = []
    lows: List[LiquidityLevel] = []
    current_day = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    prev_completed: Optional[Tuple[float, float]] = None
    for c in candles:
        day = _utc_day(c.time)
        if day != current_day:
            if current_day is not None and day_high is not None:
                prev_completed = (day_high, day_low)
            current_day = day
            day_high = day_low = None
            if prev_completed is not None:
                highs.append(LiquidityLevel(symbol=symbol, kind=LiquidityLevelType.PREV_DAY_HIGH,
                                             price=prev_completed[0], primary_timeframe=timeframe,
                                             created_at=c.time))
                lows.append(LiquidityLevel(symbol=symbol, kind=LiquidityLevelType.PREV_DAY_LOW,
                                            price=prev_completed[1], primary_timeframe=timeframe,
                                            created_at=c.time))
        day_high = c.high if day_high is None else max(day_high, c.high)
        day_low = c.low if day_low is None else min(day_low, c.low)
    return highs, lows


def detect_equal_level(
    symbol: str, swing_log: Sequence[SwingPoint], new_swing: SwingPoint, tolerance: float, lookback_bars: int,
) -> Optional[LiquidityLevel]:
    """Call once per newly-confirmed swing (same calling convention as
    swing_diagnostics.compute_swing_metadata). Returns an EQUAL_HIGH/
    EQUAL_LOW LiquidityLevel if a same-kind confirmed swing exists within
    `tolerance` price and `lookback_bars` of this swing (L3), else None.
    Zero effect on structure/BOS/CHoCH -- purely a liquidity-target concept.
    Tags primary_timeframe from new_swing.timeframe (whatever timeframe the
    swing itself was confirmed on -- H1 or M15, per spec §11)."""
    candidates = [
        s for s in swing_log
        if s.kind == new_swing.kind and s is not new_swing
        and 0 < new_swing.confirmed_at_index - s.confirmed_at_index <= lookback_bars
        and abs(s.price - new_swing.price) <= tolerance
    ]
    if not candidates:
        return None
    prices = [new_swing.price] + [c.price for c in candidates]
    if new_swing.kind == SwingType.HIGH:
        extreme = max(prices)
        kind = LiquidityLevelType.EQUAL_HIGH
    else:
        extreme = min(prices)
        kind = LiquidityLevelType.EQUAL_LOW
    return LiquidityLevel(symbol=symbol, kind=kind, price=extreme,
                           primary_timeframe=new_swing.timeframe, created_at=new_swing.candle_time)


def merge_equal_levels(
    h1_levels: Sequence[LiquidityLevel], m15_levels: Sequence[LiquidityLevel], tolerance: float,
) -> List[LiquidityLevel]:
    """Spec §11 + clarification #5: if an Equal High/Low is detected on
    both M15 and H1 (same kind, within `tolerance` price), merge into a
    single pool entry -- H1 retained as primary_timeframe, M15 stamped as
    secondary_timeframe. A liquidity pool may only exist once in the
    database. H1-only or M15-only levels (no cross-timeframe match) pass
    through unmerged, still valid resting liquidity on their own."""
    merged: List[LiquidityLevel] = []
    used_m15_ids = set()
    for h1_lv in h1_levels:
        match = next(
            (m15_lv for m15_lv in m15_levels
             if id(m15_lv) not in used_m15_ids and m15_lv.kind == h1_lv.kind
             and abs(m15_lv.price - h1_lv.price) <= tolerance),
            None,
        )
        if match is not None:
            used_m15_ids.add(id(match))
            merged.append(LiquidityLevel(
                symbol=h1_lv.symbol, kind=h1_lv.kind, price=h1_lv.price,
                primary_timeframe=Timeframe.H1, secondary_timeframe=Timeframe.M15,
                created_at=h1_lv.created_at,
            ))
        else:
            merged.append(h1_lv)
    merged.extend(m15_lv for m15_lv in m15_levels if id(m15_lv) not in used_m15_ids)
    return merged


def classify_sweep_reaction(level: LiquidityLevel, candle: Candle, min_pen: float) -> Optional[str]:
    """Pure, look-ahead-free classification of ONE candle's relationship to
    a still-unswept level. Returns 'confirmed_reversal', 'breakout', or
    None (no qualifying penetration this candle)."""
    if is_buy_side(level.kind):
        penetration = candle.high - level.price
        if not approx_gte(penetration, min_pen):
            return None
        return "breakout" if approx_gte(candle.close, level.price) else "confirmed_reversal"
    else:
        penetration = level.price - candle.low
        if not approx_gte(penetration, min_pen):
            return None
        return "breakout" if approx_lte(candle.close, level.price) else "confirmed_reversal"


def update_sweep_state(level: LiquidityLevel, candle: Candle, min_pen: float) -> None:
    """Mutates `level` in place. One-way flag -- once swept, never resets
    (mirrors fvg.update_fvg_state / order_blocks.update_ob_state). Records
    swept_at, sweep_penetration, and the reaction classification from the
    FIRST qualifying candle only."""
    if level.swept:
        return
    reaction = classify_sweep_reaction(level, candle, min_pen)
    if reaction is None:
        return
    level.swept = True
    level.swept_at = candle.time
    level.sweep_penetration = (candle.high - level.price) if is_buy_side(level.kind) else (level.price - candle.low)
    level.reaction = reaction


def swept_during_london(level: LiquidityLevel) -> bool:
    """L4 -- classifies an already-swept level's sweep time against the
    London session window. Informational only; general sweep detection is
    never gated by session."""
    return level.swept and level.swept_at is not None and is_london_session(level.swept_at)


def active_unswept_levels(levels: Sequence[LiquidityLevel], as_of_time: datetime) -> List[LiquidityLevel]:
    """L5 -- the multi-sweep-handling surface: every level created at-or-
    before `as_of_time` that has not yet been swept, across all types and
    all days, tracked independently."""
    return [lv for lv in levels if lv.created_at is not None and lv.created_at <= as_of_time and not lv.swept]
