# src/application/regime_metrics.py
#
# Kaufman Efficiency Ratio(20) on H1 -- the smc_3step regime gate. Validated
# 2026-08-12 via rolling walk-forward (3 independent forward-fit folds, real
# cascade-aware backtest engine) plus a random-gate matched-selectivity
# control (ER-gated result beat 27-30/30 seeded random draws of equal
# selectivity in every fold, combined p~0.012 via Fisher's method) --
# tools/wf_regime_gate.py and tools/random_gate_control.py in this repo's
# history hold the validation runs. ADX(14) was also tested and does NOT
# work as a gate (full-sample-looking effect vanished when checked within
# each half independently -- it was just correlating with calendar time).
#
# SINGLE SOURCE for this function: both tools/regime_metrics.py (backtest)
# and src/smc_3step_live_runner.py (live) import it from here, so live and
# backtest can never silently diverge on what "ER" means.

from __future__ import annotations
from typing import List, Optional, Sequence

from src.domain.entities import Candle


def efficiency_ratio_series(candles: Sequence[Candle], period: int = 20) -> List[Optional[float]]:
    """Kaufman Efficiency Ratio at each index: |close[i]-close[i-period]|
    divided by the sum of |close[j]-close[j-1]| over the same window -- 1.0
    means price moved in a straight line (a clean trend), near 0 means it
    round-tripped (chop). Causal: index i uses only candles up to and
    including i, safe to compute once over a full series and index into
    bar-by-bar exactly like smc_3step.ema_series already is. None for the
    first `period` bars (not enough history yet)."""
    n = len(candles)
    er: List[Optional[float]] = [None] * n
    for i in range(period, n):
        net_move = abs(candles[i].close - candles[i - period].close)
        path = sum(abs(candles[j].close - candles[j - 1].close) for j in range(i - period + 1, i + 1))
        er[i] = (net_move / path) if path > 0 else 0.0
    return er
