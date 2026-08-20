"""Rolling walk-forward validation of trend_mode='structure_only' (drop the
H1 EMA-slope requirement, keep swing-structure alone) + the ER(20) gate,
against the CURRENT live config (trend_mode='both') -- same methodology as
tools/wf_regime_gate.py (ER threshold re-fit per fold on strictly-prior
quarters via the real engine, frozen and tested on the next quarter
untouched), but comparing two EA variants instead of two gate thresholds.

Motivated by tools/smc_3step_ablation.py's finding: structure_only is
statistically indistinguishable from both (Welch t=-0.02) at full-sample
level while producing ~27% more raw signals -- this checks whether that
holds up fold-by-fold, not just in aggregate (the same discipline that
caught ER(25) and the freshness combo failing on closer inspection).

HOW TO RUN: python3 tools/wf_structure_only.py
"""
import sys, json, math
sys.path.insert(0, ".")

from src.domain.entities import Timeframe
from tools.smc_3step_ablation import to_candles, run_ablation, SYM
from src.application.regime_metrics import efficiency_ratio_series

DATA = "logs/smc3step_backtest_data.json"
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


def fit_and_test(trend_mode, h1_candles, m15_candles, m5_candles, h1_er, bounds, quarters, tag):
    oos_all = []
    for fold in range(1, 4):
        train_m5 = m5_candles[:bounds[fold]]
        test_m5 = quarters[fold]
        best = None
        for th in THRESHOLDS:
            gate = (lambda e, th=th: e is not None and e >= th)
            res = run_ablation(h1_candles, m15_candles, train_m5, trend_mode, "both", "wait",
                                f"{tag}-train-f{fold}-th{th}", regime_gate=gate, h1_er=h1_er)
            s = stats(res["trade_log"])
            if s["n"] < MIN_TRAIN_N:
                continue
            if best is None or s["mean"] > best[1]["mean"]:
                best = (th, s)
        if best is None:
            print(f"  [{tag}] fold{fold}: no threshold cleared min train n -- skipped")
            continue
        chosen_th, train_s = best
        gate = (lambda e, th=chosen_th: e is not None and e >= th)
        test_res = run_ablation(h1_candles, m15_candles, test_m5, trend_mode, "both", "wait",
                                 f"{tag}-test-f{fold}", regime_gate=gate, h1_er=h1_er)
        ts = stats(test_res["trade_log"])
        print(f"  [{tag}] fold{fold}: chosen ER>={chosen_th} (train n={train_s['n']} mean=${train_s['mean']}) "
              f"-> TEST OOS: {ts}")
        oos_all.extend(test_res["trade_log"])
    agg = stats(oos_all)
    print(f"  [{tag}] AGGREGATE OOS: {agg}")
    return agg


def main():
    raw = json.load(open(DATA))["symbols"][SYM]
    h1_candles = to_candles(raw["h1"], Timeframe.H1)
    m15_candles = to_candles(raw["m15"], Timeframe.M15)
    m5_candles = to_candles(raw["m5"], Timeframe.M5)
    h1_er = efficiency_ratio_series(h1_candles, 20)

    n = len(m5_candles)
    bounds = [int(f * n) for f in QUARTER_FRACTIONS]
    quarters = [m5_candles[bounds[i]:bounds[i + 1]] for i in range(4)]

    print("=== BOTH (current live config) + ER, walk-forward ===")
    both_agg = fit_and_test("both", h1_candles, m15_candles, m5_candles, h1_er, bounds, quarters, "both")
    print()
    print("=== STRUCTURE_ONLY (candidate) + ER, walk-forward ===")
    struct_agg = fit_and_test("structure_only", h1_candles, m15_candles, m5_candles, h1_er, bounds, quarters, "struct")
    print()

    if both_agg["n"] > 0 and struct_agg["n"] > 0:
        diff = struct_agg["mean"] - both_agg["mean"]
        se_diff = math.sqrt(both_agg["se"] ** 2 + struct_agg["se"] ** 2)
        t_diff = diff / se_diff if se_diff > 0 else float("nan")
        print(f"STRUCT vs BOTH DIFF: ${diff:.2f}  Welch t={t_diff:.2f}  n_struct={struct_agg['n']} vs n_both={both_agg['n']}")


if __name__ == "__main__":
    main()
