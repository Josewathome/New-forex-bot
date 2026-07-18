# src/application/strategy_v2/swing_diagnostics.py
#
# OPTIONAL diagnostics/analytics layer for confirmed swings. NOT imported
# by structure.py, fvg.py, order_blocks.py, or any trading-decision code
# — structure.py has zero awareness this module exists. A caller (backtest
# tooling, dashboards, ad-hoc research) may OPTIONALLY invoke
# compute_swing_metadata() after each StructureEngine.process_new_candle_
# close() call, passing that call's own swing/active_pair snapshot. If
# never called, there is no code path connecting this module to any
# trading decision — not just "no effect if called," but genuinely wired
# in nowhere by default.
#
# Research history (see docs/REJECTED_RESEARCH.md for full detail): three
# independent studies tested whether swing-level metadata like this could
# improve trade selection (via swing-selection tie-breaks, pre-filtering
# candidate swings out of the structure engine, and downstream trade-
# signal filtering). All three found no statistically significant
# improvement. This module exists ONLY as an optional descriptive/
# debugging layer — it must never be wired into any filtering, scoring,
# or selection decision. Every field here is immutable once computed and
# purely informational.
#
# All fields are computed CAUSALLY — using only information available at
# or before the moment the swing confirms (bars through candidate_idx+3,
# the same window confirmation itself already requires, or purely
# backward-looking data). No look-ahead.

from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from src.domain.entities import Candle, Timeframe
from src.domain.strategy_v2_entities import ExternalSwingPair, SwingPoint, SwingType
from src.application.strategy_v2.atr import is_displacement
from src.application.strategy_v2.fvg import detect_fvg
from src.application.strategy_v2.order_blocks import detect_order_block
from src.application.strategy_v2 import config_v2


def _asian_sweep_and_proximity(candles: Sequence[Candle], idx: int, kind: str, tol: float, lookback: int = 48):
    lo = max(0, idx - lookback)
    asian = [c for c in candles[lo:idx + 1] if 0 <= c.time.astimezone(timezone.utc).hour < 6]
    if not asian:
        return False, False
    a_high = max(c.high for c in asian)
    a_low = min(c.low for c in asian)
    level = a_high if kind == "high" else a_low
    proximity = abs((candles[idx].high if kind == "high" else candles[idx].low) - level) <= tol
    swept = False
    for j in range(max(lo, idx - 2), idx + 1):
        c = candles[j]
        if kind == "low" and c.low < a_low and c.close > a_low:
            swept = True
        if kind == "high" and c.high > a_high and c.close < a_high:
            swept = True
    return proximity, swept


def _backward_extremum(candles: Sequence[Candle], idx: int, kind: str, n: int) -> bool:
    lo = max(0, idx - n)
    window = candles[lo:idx]
    if not window:
        return True
    if kind == "high":
        return candles[idx].high >= max(c.high for c in window)
    return candles[idx].low <= min(c.low for c in window)


def compute_swing_metadata(
    symbol: str,
    timeframe: Timeframe,
    candles: Sequence[Candle],
    atr: Sequence[Optional[float]],
    swing_log: List[SwingPoint],
    swing: SwingPoint,
    active_pair: Optional[ExternalSwingPair],
    equal_level_tolerance: float,
) -> Dict[str, object]:
    """Pure function — reads its inputs, mutates nothing, returns a plain
    dict. `swing_log` should be the engine's log at/after this swing was
    appended (used only to look backward for equal-highs/lows and the
    previous opposite-kind swing). `active_pair` should be the engine's
    active_pair AT THE MOMENT this swing confirmed (pass None if the
    trend was NEUTRAL at that time) — callers own capturing that
    snapshot; this function does not read live engine state itself."""
    idx = swing.confirmed_at_index - config_v2.SWING_CONFIRM_CANDLES
    kind = swing.kind.value

    prox, swept = _asian_sweep_and_proximity(candles, idx, kind, equal_level_tolerance)

    equal_hl = any(
        other.kind == swing.kind and other is not swing
        and 0 < idx - (other.confirmed_at_index - config_v2.SWING_CONFIRM_CANDLES) <= 72
        and abs(other.price - swing.price) <= equal_level_tolerance
        for other in swing_log
    )

    disp_mag = 0.0
    for k in range(idx, min(len(candles), idx + config_v2.SWING_CONFIRM_CANDLES + 1)):
        if atr[k] is None or atr[k] <= 0:
            continue
        r = abs(candles[k].close - candles[k].open) / atr[k]
        disp_mag = max(disp_mag, r)

    fvg_origin, ob_origin = False, False
    if idx + 2 < len(candles) and atr[idx] is not None:
        c2, c3 = candles[idx + 1], candles[idx + 2]
        if detect_fvg(symbol, timeframe, candles[idx], c2, c3, atr[idx]) is not None:
            fvg_origin = True
        ob_result = detect_order_block(symbol, timeframe, candles, idx, c2, atr[idx])
        if ob_result is not None and ob_result[1] == 1:
            ob_origin = True

    prior_opposite = [
        o for o in swing_log
        if o.kind != swing.kind and (o.confirmed_at_index - config_v2.SWING_CONFIRM_CANDLES) < idx
    ]
    dist_prev = (idx - (prior_opposite[-1].confirmed_at_index - config_v2.SWING_CONFIRM_CANDLES)) if prior_opposite else None

    htf_prox = None
    if active_pair is not None:
        d_low = abs(swing.price - active_pair.low)
        d_high = abs(swing.price - active_pair.high)
        a = atr[idx] if idx < len(atr) else None
        if a and a > 0:
            htf_prox = min(d_low, d_high) / a

    return {
        "liquidity_sweep": swept,
        "equal_hl_proximity": equal_hl,
        "fvg_origin": fvg_origin,
        "ob_origin": ob_origin,
        "displacement_magnitude": disp_mag,
        "dist_prev_structural_swing": dist_prev,
        "htf_pair_proximity_atr": htf_prox,
        "backward_prominence_10": _backward_extremum(candles, idx, kind, 10),
        "backward_prominence_20": _backward_extremum(candles, idx, kind, 20),
        "backward_prominence_50": _backward_extremum(candles, idx, kind, 50),
    }
