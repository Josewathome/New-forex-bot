"""Control experiment for the ER(20) regime gate: does the apparent
improvement come from ER carrying real information, or just from trading
less (fewer trades -> different sample, which can look better by pure
variance even with zero information)?

For each of the 3 walk-forward OOS test quarters (see tools/
wf_regime_gate.py), builds a RANDOM gate that accepts each raw signal
independently with the SAME acceptance rate the real ER gate achieved in
that exact quarter (opened/signals, so the random control experiences the
identical cascade dynamics -- rejecting a signal frees up bars for a new
setup exactly like the real gate does), run through the real engine with
many seeds. If the real ER-gated mean doesn't clearly beat the
distribution of matched-selectivity random-gate means, the apparent edge
is not distinguishable from "trading less helps by chance."

HOW TO RUN: python3 tools/random_gate_control.py
"""
import sys, json, math, random
sys.path.insert(0, ".")

from src.domain.entities import Timeframe
from tools.backtest_smc_3step_fresh import to_candles, run_segment, SYM
from tools.regime_metrics import adx_series, efficiency_ratio_series

DATA = "logs/smc3step_backtest_data.json"
SPREAD, SLIPPAGE = 6.0, 5.0
N_SEEDS = 30

# (test-quarter fraction bounds, chosen ER threshold, real ER-gated result)
# from tools/wf_regime_gate.py's actual run -- hardcoded here rather than
# re-deriving, so this script is a pure control/comparison, not a re-fit.
FOLDS = [
    {"label": "FOLD1 test=Q2", "start_frac": 0.25, "end_frac": 0.50, "er_threshold": 0.45,
     "real_gated_mean": 8.77, "real_gated_n": 69},
    {"label": "FOLD2 test=Q3", "start_frac": 0.50, "end_frac": 0.75, "er_threshold": 0.45,
     "real_gated_mean": 130.56, "real_gated_n": 65},
    {"label": "FOLD3 test=Q4", "start_frac": 0.75, "end_frac": 1.00, "er_threshold": 0.45,
     "real_gated_mean": 75.64, "real_gated_n": 61},
]


def stats(trade_log):
    pnls = [t["pnl"] for t in trade_log]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "mean": None}
    mean = sum(pnls) / n
    return {"n": n, "mean": round(mean, 2)}


def main():
    raw = json.load(open(DATA))["symbols"][SYM]
    h1_candles = to_candles(raw["h1"], Timeframe.H1)
    m15_candles = to_candles(raw["m15"], Timeframe.M15)
    m5_candles = to_candles(raw["m5"], Timeframe.M5)
    h1_adx = adx_series(h1_candles, 14)
    h1_er = efficiency_ratio_series(h1_candles, 20)
    n_total = len(m5_candles)

    for fold in FOLDS:
        start = int(fold["start_frac"] * n_total)
        end = int(fold["end_frac"] * n_total)
        test_m5 = m5_candles[start:end]

        er_gate = (lambda a, e, z=None, th=fold["er_threshold"]: e is not None and e >= th)
        er_result = run_segment(h1_candles, m15_candles, test_m5, SPREAD, SLIPPAGE, True,
                                 f"{fold['label']}-ERgate", regime_gate=er_gate, h1_adx=h1_adx, h1_er=h1_er)
        er_stats = stats(er_result["trade_log"])

        # First pass: how many raw signals occur with NO gate, to measure
        # the true acceptance rate the ER gate achieved (opened/signals),
        # so the random control is matched on selectivity, not on outcome.
        ungated_result = run_segment(h1_candles, m15_candles, test_m5, SPREAD, SLIPPAGE, True,
                                      f"{fold['label']}-ungated", h1_adx=h1_adx, h1_er=h1_er)
        accept_rate = er_result["opened"] / er_result["signals"] if er_result["signals"] > 0 else 0

        print(f"=== {fold['label']} ===")
        print(f"  ER-gated (real):    n={er_stats['n']} mean=${er_stats['mean']}  "
              f"(signals={er_result['signals']}, opened={er_result['opened']}, accept_rate={accept_rate:.3f})")
        print(f"  Ungated (reference): n={ungated_result['opened']} mean=${round(ungated_result['total_pnl']/ungated_result['opened'],2) if ungated_result['opened'] else None}")

        random_means = []
        for seed in range(N_SEEDS):
            rng = random.Random(seed * 1000 + hash(fold["label"]) % 1000)
            random_gate = (lambda a, e, z=None, rng=rng, rate=accept_rate: rng.random() < rate)
            res = run_segment(h1_candles, m15_candles, test_m5, SPREAD, SLIPPAGE, True,
                               f"{fold['label']}-rand{seed}", regime_gate=random_gate, h1_adx=h1_adx, h1_er=h1_er)
            s = stats(res["trade_log"])
            if s["n"] > 0:
                random_means.append(s["mean"])

        random_means.sort()
        rm_n = len(random_means)
        rm_avg = sum(random_means) / rm_n if rm_n else None
        beat_count = sum(1 for m in random_means if er_stats["mean"] is not None and er_stats["mean"] > m)
        pctile = 100 * beat_count / rm_n if rm_n else None
        print(f"  RANDOM-gate control ({rm_n} seeds, same accept_rate): "
              f"mean-of-means=${round(rm_avg,2) if rm_avg else None}  "
              f"min=${random_means[0] if random_means else None}  max=${random_means[-1] if random_means else None}")
        print(f"  ER-gated result beats {beat_count}/{rm_n} random draws "
              f"({pctile:.0f}th percentile of the random-selectivity distribution)")
        print()


if __name__ == "__main__":
    main()
