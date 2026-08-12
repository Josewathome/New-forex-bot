"""Screening pass (not final validation): tags the same ungated trade
population with ER computed at several H1 lookback periods, then does
cheap post-hoc threshold-grid analysis to see which (period, threshold)
combos look promising -- full-period stats AND split-half directional
consistency (does it help BOTH halves, not just one). This is a SCREEN
only: post-hoc filtering of an ungated run understates the real effect
(see tools/wf_regime_gate.py's docstring on the cascade issue) -- any
winner here still needs the full rolling-walk-forward + random-gate-
control treatment before being trusted, exactly like ER(20) got.

HOW TO RUN: python3 tools/screen_er_lookback.py
"""
import sys, json, math
from datetime import datetime, timezone
sys.path.insert(0, ".")

from src.domain.entities import Timeframe
from tools.backtest_smc_3step_fresh import to_candles, run_segment, SYM
from src.application.regime_metrics import efficiency_ratio_series

DATA = "logs/smc3step_backtest_data.json"
SPREAD, SLIPPAGE = 6.0, 5.0
PERIODS = [10, 15, 20, 25, 30, 40]
THRESHOLDS = [round(x / 100, 2) for x in range(25, 66, 5)]
SPLIT_DATE = datetime(2025, 3, 14)


def stats(ts):
    n = len(ts)
    if n == 0:
        return {"n": 0, "mean": None, "pf": None}
    mean = sum(t["pnl"] for t in ts) / n
    gains = sum(t["pnl"] for t in ts if t["pnl"] > 0)
    loss_sum = abs(sum(t["pnl"] for t in ts if t["pnl"] < 0))
    pf = gains / loss_sum if loss_sum > 0 else None
    wins = sum(1 for t in ts if t["outcome"] == "WIN")
    return {"n": n, "mean": round(mean, 2), "pf": round(pf, 2) if pf else None,
            "win_rate": round(100 * wins / n, 1)}


def main():
    raw = json.load(open(DATA))["symbols"][SYM]
    h1_candles = to_candles(raw["h1"], Timeframe.H1)
    m15_candles = to_candles(raw["m15"], Timeframe.M15)
    m5_candles = to_candles(raw["m5"], Timeframe.M5)

    for period in PERIODS:
        h1_er = efficiency_ratio_series(h1_candles, period)
        result = run_segment(h1_candles, m15_candles, m5_candles, SPREAD, SLIPPAGE, True,
                              f"ungated-erlb{period}", h1_er=h1_er)
        trades = result["trade_log"]
        print(f"=== ER lookback={period} (ungated population, n={len(trades)}) ===")
        for th in THRESHOLDS:
            kept = [t for t in trades if t["er_at_signal"] is not None and t["er_at_signal"] >= th]
            first = [t for t in kept if datetime.fromisoformat(t["opened_at"]).replace(tzinfo=None) < SPLIT_DATE]
            second = [t for t in kept if datetime.fromisoformat(t["opened_at"]).replace(tzinfo=None) >= SPLIT_DATE]
            s_all, s_1, s_2 = stats(kept), stats(first), stats(second)
            both_positive = (s_1["mean"] is not None and s_1["mean"] > 0 and
                             s_2["mean"] is not None and s_2["mean"] > 0)
            flag = " <-- BOTH HALVES POSITIVE" if both_positive and s_all["n"] >= 30 else ""
            print(f"  th={th:.2f}: all={s_all}  half1={s_1}  half2={s_2}{flag}")
        print()


if __name__ == "__main__":
    main()
