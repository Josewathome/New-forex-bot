"""Causal (no-lookahead) trend-strength / regime metrics computed on H1
candles, for testing whether smc_3step's edge concentration (see
tools/backtest_smc_3step_fresh.py, split-half result) is explained by a
detectable "trending vs choppy" regime rather than being un-filterable.

Both metrics use ONLY candles up to and including index i -- safe to
compute once over the full series and index into during a bar-by-bar
backtest exactly like ema_series()/wilder_atr_series() already are.

ADX(14): classic Wilder trend-strength oscillator (0-100, higher =
stronger directional trend, low = choppy/range-bound). Reimplemented here
(not in the codebase elsewhere) following Wilder's original smoothing --
same convention as strategy_v2's wilder_atr_series for TR/smoothing.

Kaufman Efficiency Ratio (ER, period N): re-exported from src.application.
regime_metrics (the production/live module -- ER is the one that actually
got validated and wired into smc_3step_live_runner.py, see that module's
docstring) rather than duplicated here, so backtest and live can never
silently diverge on what "ER" means.
"""
from __future__ import annotations
from typing import List, Optional, Sequence

from src.domain.entities import Candle
from src.application.strategy_v2.atr import true_range
from src.application.regime_metrics import efficiency_ratio_series  # noqa: F401  (re-exported)


def adx_series(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    n = len(candles)
    adx: List[Optional[float]] = [None] * n
    if n < period * 2:
        return adx

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = true_range(candles[i], candles[i - 1].close)

    # Wilder-smoothed running sums, seeded by a simple sum of the first
    # `period` values (standard ADX convention).
    smoothed_tr = sum(tr[1:period + 1])
    smoothed_plus = sum(plus_dm[1:period + 1])
    smoothed_minus = sum(minus_dm[1:period + 1])

    dx_values: List[Optional[float]] = [None] * n
    start = period + 1
    if start >= n:
        return adx

    def dx_at(s_tr, s_plus, s_minus):
        if s_tr <= 0:
            return None
        plus_di = 100.0 * s_plus / s_tr
        minus_di = 100.0 * s_minus / s_tr
        denom = plus_di + minus_di
        if denom <= 0:
            return 0.0
        return 100.0 * abs(plus_di - minus_di) / denom

    dx_values[start] = dx_at(smoothed_tr, smoothed_plus, smoothed_minus)

    for i in range(start + 1, n):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr[i]
        smoothed_plus = smoothed_plus - (smoothed_plus / period) + plus_dm[i]
        smoothed_minus = smoothed_minus - (smoothed_minus / period) + minus_dm[i]
        dx_values[i] = dx_at(smoothed_tr, smoothed_plus, smoothed_minus)

    # ADX = Wilder-smoothed DX, seeded by a simple average of the first
    # `period` DX values, exactly mirroring wilder_atr_series's own seed
    # convention for consistency across this codebase's indicators.
    adx_seed_start = start + period
    if adx_seed_start >= n:
        return adx
    valid_dx = [d for d in dx_values[start:adx_seed_start] if d is not None]
    if len(valid_dx) < period:
        return adx
    running = sum(valid_dx) / period
    adx[adx_seed_start] = running
    for i in range(adx_seed_start + 1, n):
        d = dx_values[i]
        if d is None:
            continue
        running = (running * (period - 1) + d) / period
        adx[i] = running
    return adx
