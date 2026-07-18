# src/application/strategy_v2/entry_confirmation.py
#
# Entry Confirmation — the M1 confirmation sequence gating an actual trade
# entry: liquidity sweep -> POI touch -> discount/premium validation ->
# local (M1) CHoCH/structure confirmation -> entry-candle confirmation ->
# Stop Loss placement -> Take Profit calculation -> RR>=MIN_RR filter.
#
# Pure composition over already-frozen/already-built primitives -- this
# module reimplements NONE of structure/FVG/OB/liquidity/premium-discount
# logic. In particular, "local CHoCH confirmation" reuses the frozen
# StructureEngine class itself, run on M1 candles instead of H1 -- the
# engine is already timeframe-generic (it only ever reads Candle.time/
# open/high/low/close and SwingPoint.confirmed_at_index; nothing in it is
# H1-specific), so a second per-symbol StructureEngine instance fed M1
# closes is a legitimate, non-redesigning reuse of the exact same locked
# D1-D6 machinery at a lower timeframe, not a new implementation of CHoCH.
#
# DOCUMENTED IMPLEMENTATION DECISIONS (this module's own scope; no
# structure/FVG/OB/liquidity/premium-discount logic touched or
# re-litigated -- reconstructed from the plan's Part 12-16 descriptions,
# this strategy's already-locked config_v2 constants (DISPLACEMENT_
# MULTIPLIER_M1, SL_BUFFER_PIPS, MIN_RR), and the user's current mandate):
#
#   E1. Sweep confirmation: a LiquidityLevel counts as a valid entry
#       trigger only if swept with reaction == 'confirmed_reversal' (a
#       'breakout' sweep is never a valid entry trigger -- see
#       liquidity.py) and recent (within max_sweep_age of the entry
#       candle's time).
#   E2. POI touch: the current M1 candle's range [low, high] intersects
#       the POI zone's [zone_low, zone_high] -- inclusive ("first touch",
#       mirroring order_blocks.py's own mitigation semantics), via
#       numerics.approx_lte/approx_gte.
#   E3. Discount/Premium validation: delegates entirely to
#       premium_discount.is_in_favorable_zone(pair, zone_low, zone_high) --
#       no reimplementation.
#   E4. Local CHoCH/structure confirmation: the M1 StructureEngine's
#       CURRENT trend must equal the intended trade direction (a M1 BOS
#       establishing that trend, or a M1 CHoCH followed by a same-
#       direction M1 BOS, both naturally produce this). This is the
#       "local structure shift confirms the HTF reversal is genuinely
#       underway" step -- distinct from and does not affect the H1
#       StructureEngine's own trend.
#   E5. Entry candle confirmation (V4 spec Part 12): the M1 candle must
#       (a) close beyond the most recent local M1 swing in the trade's
#       direction (body close, not a wick -- same convention as BOS) and
#       (b) qualify as a displacement candle per atr.is_displacement with
#       config_v2.DISPLACEMENT_MULTIPLIER_M1 (1.5x), using the M1 ATR
#       value computed THROUGH the candle before it (same non-circular
#       convention proven for FVG/OB).
#   E6. Stop Loss (Part 14): placed just beyond the POI zone's FAR edge
#       (the side away from the entry direction) plus the per-instrument
#       buffer (config_v2.SL_BUFFER_PIPS / SL_BUFFER_PIPS_XAUUSD) --
#       BULLISH: zone_low - buffer; BEARISH: zone_high + buffer.
#   E7. Take Profit (Part 15): the nearer (to entry price) of two
#       candidates -- the active H1 pair's opposite extreme ("next HTF
#       external swing") and the opposite-side Asia-range liquidity level
#       ("opposite Asia-range side"), when both are available; if only
#       one candidate is available/valid, that one is used.
#   E8. RR filter (Part 16): entries with RR < config_v2.MIN_RR (5.0) are
#       rejected (evaluate_entry returns None) -- this module does not
#       track WHY a candidate was rejected (that is Module 5's Trade
#       Decision Engine's job, "trade rejection reasons"); it only ever
#       returns a fully-qualified EntrySignal or None.

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from src.domain.entities import Candle, Direction, Timeframe
from src.domain.strategy_v2_entities import ExternalSwingPair, LiquidityLevel, TrendState
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.atr import is_displacement
from src.application.strategy_v2.numerics import approx_gte, approx_lte
from src.application.strategy_v2.premium_discount import is_in_favorable_zone


@dataclass
class EntrySignal:
    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    rr: float
    poi_kind: str          # "fvg" | "ob"
    entry_time: datetime


def sweep_confirmed(level: LiquidityLevel, now: datetime, max_age: timedelta) -> bool:
    """E1 -- a valid entry-triggering sweep: confirmed_reversal (never
    breakout), and recent."""
    if not level.swept or level.reaction != "confirmed_reversal" or level.swept_at is None:
        return False
    return (now - level.swept_at) <= max_age


def poi_touched(candle: Candle, zone_low: float, zone_high: float) -> bool:
    """E2 -- inclusive first-touch test, mirroring order_blocks.py's own
    mitigation convention."""
    return approx_lte(candle.low, zone_high) and approx_gte(candle.high, zone_low)


def poi_in_favorable_zone(pair: ExternalSwingPair, zone_low: float, zone_high: float) -> bool:
    """E3 -- delegates to premium_discount, no reimplementation."""
    return is_in_favorable_zone(pair, zone_low, zone_high)


def local_structure_confirms(direction: Direction, local_trend: TrendState) -> bool:
    """E4 -- the M1 StructureEngine's current trend must match the
    intended trade direction. Direction/TrendState share string values
    ("bullish"/"bearish") by construction."""
    return local_trend.value == direction.value


def entry_candle_confirmed(
    candle: Candle, local_swing_price: float, direction: Direction, atr_value: Optional[float],
) -> bool:
    """E5 -- body-close break of the local M1 swing, AND M1 displacement
    (1.5x ATR(10))."""
    if not is_displacement(candle, atr_value, config_v2.DISPLACEMENT_MULTIPLIER_M1):
        return False
    if direction == Direction.BULLISH:
        return approx_gte(candle.close, local_swing_price) and candle.close > local_swing_price
    return approx_lte(candle.close, local_swing_price) and candle.close < local_swing_price


def compute_stop_loss(direction: Direction, zone_low: float, zone_high: float, symbol: str) -> float:
    """E6 -- beyond the POI's far edge plus the per-instrument buffer."""
    buffer_pips = config_v2.SL_BUFFER_PIPS_XAUUSD if symbol == "XAUUSD" else config_v2.SL_BUFFER_PIPS
    buffer = buffer_pips * config_v2.pip_size(symbol)
    if direction == Direction.BULLISH:
        return zone_low - buffer
    return zone_high + buffer


def compute_take_profit(
    direction: Direction, entry_price: float, pair: ExternalSwingPair,
    opposite_asia_level: Optional[LiquidityLevel],
) -> float:
    """E7 -- nearer of (opposite HTF pair extreme, opposite Asia-range
    level). pair.low/.high are normalized via min/max (see
    premium_discount.py's documented note on ExternalSwingPair.low/.high
    not being guaranteed low<=high)."""
    range_lo, range_hi = min(pair.low, pair.high), max(pair.low, pair.high)
    htf_target = range_hi if direction == Direction.BULLISH else range_lo

    candidates = [htf_target]
    if opposite_asia_level is not None:
        candidates.append(opposite_asia_level.price)

    return min(candidates, key=lambda p: abs(p - entry_price))


def compute_rr(entry_price: float, stop_loss: float, take_profit: float) -> float:
    risk = abs(entry_price - stop_loss)
    if risk == 0:
        return 0.0
    reward = abs(take_profit - entry_price)
    return reward / risk


def evaluate_entry(
    symbol: str,
    direction: Direction,
    pair: ExternalSwingPair,
    sweep_level: LiquidityLevel,
    poi_kind: str,
    zone_low: float,
    zone_high: float,
    entry_candle: Candle,
    local_trend: TrendState,
    local_swing_price: float,
    m1_atr_at_prev: Optional[float],
    opposite_asia_level: Optional[LiquidityLevel],
    max_sweep_age: timedelta = timedelta(hours=6),
) -> Optional[EntrySignal]:
    """Orchestrates E1-E8 in sequence. Returns None at the first failing
    gate; a fully-qualified EntrySignal only if every gate (including
    RR >= config_v2.MIN_RR) passes."""
    if not sweep_confirmed(sweep_level, entry_candle.time, max_sweep_age):
        return None
    if not poi_touched(entry_candle, zone_low, zone_high):
        return None
    if not poi_in_favorable_zone(pair, zone_low, zone_high):
        return None
    if not local_structure_confirms(direction, local_trend):
        return None
    if not entry_candle_confirmed(entry_candle, local_swing_price, direction, m1_atr_at_prev):
        return None

    entry_price = entry_candle.close
    stop_loss = compute_stop_loss(direction, zone_low, zone_high, symbol)
    take_profit = compute_take_profit(direction, entry_price, pair, opposite_asia_level)
    rr = compute_rr(entry_price, stop_loss, take_profit)
    if rr < config_v2.MIN_RR:
        return None

    return EntrySignal(
        symbol=symbol, direction=direction, entry_price=entry_price, stop_loss=stop_loss,
        take_profit=take_profit, rr=rr, poi_kind=poi_kind, entry_time=entry_candle.time,
    )
