# src/application/strategy_v2/swings.py
#
# Universal swing-high / swing-low detection — V4 spec Contradiction 1.
# Identical rule on H1, M15, and M1: no timeframe-specific variation.
#
#   Swing High at index i:
#       High[i] >  High[i-1], High[i-2], High[i-3]   (strict, left)
#       High[i] >= High[i+1], High[i+2], High[i+3]   (non-strict, right)
#   Swing Low mirrors with < / <=.
#
# Confirmation lags 3 bars behind the candidate candle: a swing at index i
# is only knowable once candle i+3 has closed.

from __future__ import annotations
from typing import List, Optional, Sequence

from src.domain.entities import Candle, Timeframe
from src.domain.strategy_v2_entities import SwingPoint, SwingType


def is_swing_high(candles: Sequence[Candle], i: int) -> bool:
    if i < 3 or i > len(candles) - 4:
        return False
    h = candles[i].high
    if not (h > candles[i - 1].high and h > candles[i - 2].high and h > candles[i - 3].high):
        return False
    return h >= candles[i + 1].high and h >= candles[i + 2].high and h >= candles[i + 3].high


def is_swing_low(candles: Sequence[Candle], i: int) -> bool:
    if i < 3 or i > len(candles) - 4:
        return False
    l = candles[i].low
    if not (l < candles[i - 1].low and l < candles[i - 2].low and l < candles[i - 3].low):
        return False
    return l <= candles[i + 1].low and l <= candles[i + 2].low and l <= candles[i + 3].low


def confirmed_swings_at_close(
    symbol: str, timeframe: Timeframe, candles: Sequence[Candle], i: int,
) -> List[SwingPoint]:
    """Swing(s) confirmed AT candle i (candidate = i - 3). Call once per new
    candle close with i = len(candles) - 1. A candle can in principle satisfy
    both the high and low test simultaneously (a wide outlier bar exceeding
    all 6 neighbours on both sides) — both are returned rather than only one,
    since the spec does not say a candle may only be one or the other."""
    candidate = i - 3
    if candidate < 0:
        return []
    out: List[SwingPoint] = []
    if is_swing_high(candles, candidate):
        out.append(SwingPoint(
            symbol=symbol, timeframe=timeframe, kind=SwingType.HIGH,
            price=candles[candidate].high, candle_time=candles[candidate].time,
            confirmed_at_index=i,
        ))
    if is_swing_low(candles, candidate):
        out.append(SwingPoint(
            symbol=symbol, timeframe=timeframe, kind=SwingType.LOW,
            price=candles[candidate].low, candle_time=candles[candidate].time,
            confirmed_at_index=i,
        ))
    return out


def scan_confirmed_swings(
    symbol: str, timeframe: Timeframe, candles: Sequence[Candle],
) -> List[SwingPoint]:
    """Full rescan over a candle window, returning every confirmed swing in
    chronological order. Used to rebuild the swing log from scratch (state
    recovery after a restart, or a backtester priming its history)."""
    swings: List[SwingPoint] = []
    for candidate in range(3, len(candles) - 3):
        if is_swing_high(candles, candidate):
            swings.append(SwingPoint(
                symbol=symbol, timeframe=timeframe, kind=SwingType.HIGH,
                price=candles[candidate].high, candle_time=candles[candidate].time,
                confirmed_at_index=candidate + 3,
            ))
        if is_swing_low(candles, candidate):
            swings.append(SwingPoint(
                symbol=symbol, timeframe=timeframe, kind=SwingType.LOW,
                price=candles[candidate].low, candle_time=candles[candidate].time,
                confirmed_at_index=candidate + 3,
            ))
    swings.sort(key=lambda s: s.confirmed_at_index)
    return swings
