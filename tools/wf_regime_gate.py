"""Rolling walk-forward validation of the ER(20)>=threshold regime gate for
smc_3step, using the REAL cascade-aware backtest engine at every step (not
post-hoc filtering of an already-generated trade list -- rejecting a signal
changes which bars the EA is free to re-evaluate on, so the candidate-signal
set under a gate genuinely differs from the ungated run's; a naive filter
of the ungated trade list understates/misstates the effect).

For each fold: grid-search the ER threshold using ONLY real-engine runs on
the training range (all quarters strictly before the test quarter), pick
the expectancy-maximizing threshold with a minimum trade-count floor, freeze
it, then run the real engine on the test quarter alone and report both the
gated and ungated result for direct comparison.

HOW TO RUN: python3 tools/wf_regime_gate.py
"""
import sys, json, math
from datetime import datetime, timezone
sys.path.insert(0, ".")

from src.domain.entities import Timeframe
from tools.backtest_smc_3step_fresh import to_candles, run_segment, SYM
from tools.regime_metrics import adx_series, efficiency_ratio_series

DATA = "logs/smc3step_backtest_data.json"
SPREAD, SLIPPAGE = 6.0, 5.0

# Same quarter boundaries used throughout this investigation (index-based
# quartering of the 200k M5 bars, dates confirmed by the engine's own
# range= printout in earlier runs).
QUARTER_FRACTIONS = [0.0, 0.25, 0.5, 0.75, 1.0]
THRESHOLDS = [round(x / 100, 2) for x in range(30, 66, 5)]
MIN_TRAIN_N = 15


def stats(trade_log):
    pnls = [t["pnl"] for t in trade_log]
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 0 else 0.0
    wins = sum(1 for t in trade_log if t["outcome"] == "WIN")
    gains = sum(p for p in pnls if p > 0)
    loss_sum = abs(sum(p for p in pnls if p < 0))
    pf = gains / loss_sum if loss_sum > 0 else None
    return {"n": n, "mean": round(mean, 2), "se": round(se, 2),
            "t": round(mean / se, 2) if se > 0 else None,
            "win_rate": round(100 * wins / n, 1), "pf": round(pf, 2) if pf else None}


def main():
    raw = json.load(open(DATA))["symbols"][SYM]
    h1_candles = to_candles(raw["h1"], Timeframe.H1)
    m15_candles = to_candles(raw["m15"], Timeframe.M15)
    m5_candles = to_candles(raw["m5"], Timeframe.M5)
    h1_adx = adx_series(h1_candles, 14)
    h1_er = efficiency_ratio_series(h1_candles, 20)

    n = len(m5_candles)
    bounds = [int(f * n) for f in QUARTER_FRACTIONS]
    quarters = [m5_candles[bounds[i]:bounds[i + 1]] for i in range(4)]
    for i, q in enumerate(quarters):
        print(f"Q{i+1}: {q[0].time.date()}..{q[-1].time.date()} ({len(q)} bars)")
    print()

    oos_gated_all, oos_ungated_all = [], []

    for fold in range(1, 4):   # test Q2, Q3, Q4 -- each fit on strictly-prior quarters
        train_m5 = m5_candles[:bounds[fold]]
        test_m5 = quarters[fold]
        print(f"=== FOLD {fold}: train=Q1..Q{fold} ({len(train_m5)} bars), test=Q{fold+1} ({len(test_m5)} bars) ===")

        best = None
        for th in THRESHOLDS:
            gate = (lambda a, e, th=th: e is not None and e >= th)
            res = run_segment(h1_candles, m15_candles, train_m5, SPREAD, SLIPPAGE, True,
                               f"train-fold{fold}-th{th}", regime_gate=gate, h1_adx=h1_adx, h1_er=h1_er)
            s = stats(res["trade_log"])
            if s["n"] < MIN_TRAIN_N:
                continue
            print(f"    train th={th}: n={s['n']} mean=${s['mean']} pf={s['pf']}")
            if best is None or s["mean"] > best[1]["mean"]:
                best = (th, s)

        if best is None:
            print("  No threshold cleared the minimum train trade count -- skipping fold.")
            continue
        chosen_th, train_stats = best
        print(f"  CHOSEN (in-sample): ER>={chosen_th}  train_n={train_stats['n']} train_mean=${train_stats['mean']}")

        gate = (lambda a, e, th=chosen_th: e is not None and e >= th)
        test_gated = run_segment(h1_candles, m15_candles, test_m5, SPREAD, SLIPPAGE, True,
                                  f"test-fold{fold}-gated", regime_gate=gate, h1_adx=h1_adx, h1_er=h1_er)
        test_ungated = run_segment(h1_candles, m15_candles, test_m5, SPREAD, SLIPPAGE, True,
                                    f"test-fold{fold}-ungated", h1_adx=h1_adx, h1_er=h1_er)

        gs, us = stats(test_gated["trade_log"]), stats(test_ungated["trade_log"])
        print(f"  TEST (OOS) gated:   {gs}")
        print(f"  TEST (OOS) ungated: {us}")
        print()
        oos_gated_all.extend(test_gated["trade_log"])
        oos_ungated_all.extend(test_ungated["trade_log"])

    print("=== AGGREGATE ACROSS ALL 3 OOS FOLDS (Q2,Q3,Q4, each honestly forward-fit) ===")
    gs_all, us_all = stats(oos_gated_all), stats(oos_ungated_all)
    print("GATED  :", gs_all)
    print("UNGATED:", us_all)
    if gs_all["n"] > 0 and us_all["n"] > 0:
        diff = gs_all["mean"] - us_all["mean"]
        se_diff = math.sqrt(gs_all["se"] ** 2 + us_all["se"] ** 2)
        t_diff = diff / se_diff if se_diff > 0 else float("nan")
        print(f"DIFF: ${diff:.2f}  Welch t={t_diff:.2f}  95%CI=[${diff-1.96*se_diff:.2f}, ${diff+1.96*se_diff:.2f}]")


if __name__ == "__main__":
    main()
