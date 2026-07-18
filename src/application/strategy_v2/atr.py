# src/application/strategy_v2/atr.py
#
# Wilder ATR(10) and the displacement test — V4 spec §7 / Contradiction 3's
# "#9 ATR Calculation" resolution (Wilder smoothing, industry standard,
# matches MT5's native ATR indicator).
#
# True Range: TR[i] = max(High[i]-Low[i], |High[i]-Close[i-1]|, |Low[i]-Close[i-1]|)
# Wilder smoothing: seed = simple average of the first `period` TR values;
# every subsequent value = (prev_atr * (period-1) + TR) / period.
#
# Displacement: body_size = |Close - Open|. Pass if body_size >= multiplier
# * ATR(period), where multiplier is 2.0 for H1/M15 (FVG/OB) and 1.5 for M1
# confirmation (both from config_v2), and ATR is computed on the SAME
# timeframe as the candle being tested.

from __future__ import annotations
from typing import List, Optional, Sequence

from src.domain.entities import Candle


def true_range(candle: Candle, prev_close: float) -> float:
    return max(
        candle.high - candle.low,
        abs(candle.high - prev_close),
        abs(candle.low - prev_close),
    )


def wilder_atr_series(candles: Sequence[Candle], period: int = 10) -> List[Optional[float]]:
    """ATR value at each index, or None until `period` prior closes exist
    (needs `period` True Range values before the first seed is possible)."""
    atr: List[Optional[float]] = [None] * len(candles)
    trs: List[float] = []
    running: Optional[float] = None
    for i in range(1, len(candles)):
        tr = true_range(candles[i], candles[i - 1].close)
        if running is None:
            trs.append(tr)
            if len(trs) == period:
                running = sum(trs) / period
                atr[i] = running
        else:
            running = (running * (period - 1) + tr) / period
            atr[i] = running
    return atr


def body_size(candle: Candle) -> float:
    return abs(candle.close - candle.open)


# See fvg._EPSILON — same binary floating-point rationale: an exact-
# boundary body size (== multiplier * ATR) can land a few ULPs below the
# threshold due to representation error, not a genuine shortfall.
_EPSILON = 1e-9


def is_displacement(candle: Candle, atr_value: Optional[float], multiplier: float) -> bool:
    """Exact pass/fail test: body_size >= multiplier * ATR. False (not
    True) when ATR is unavailable — displacement cannot be evaluated
    without it, so it must not silently pass."""
    if atr_value is None or atr_value <= 0:
        return False
    return body_size(candle) >= multiplier * atr_value - _EPSILON
