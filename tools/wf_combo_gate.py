"""Rolling walk-forward validation of the ER(20)>=er_th AND zone_age<=age_th
combo gate -- the lead surfaced by tools/screen_er_lookback.py's sibling
post-hoc analysis (fresh zones within the ER-accepted population did
better in BOTH halves). Same methodology as tools/wf_regime_gate.py (real
cascade-aware engine, threshold grid re-fit on strictly-prior quarters
only, then frozen and tested on the next quarter untouched) but over a 2D
(er_threshold x age_threshold) grid instead of 1D.

age_th=999 is included in the grid as a stand-in for "no freshness filter"
-- this nests the pure-ER gate inside the search space, so if the combo
never beats age_th=999 at the best ER threshold, that IS the honest
finding (freshness adds nothing once tested fairly), not a foregone
conclusion.

HOW TO RUN: python3 tools/wf_combo_gate.py
"""
import sys, json, math
from datetime import datetime, timezone
sys.path.insert(0, ".")

from src.domain.entities import Timeframe
from tools.backtest_smc_3step_fresh import to_candles, run_segment, SYM
from tools.regime_metrics import adx_series, efficiency_ratio_series

DATA = "logs/smc3step_backtest_data.json"
SPREAD, SLIPPAGE = 6.0, 5.0
QUARTER_FRACTIONS = [0.0, 0.25, 0.5, 0.75, 1.0]
ER_THRESHOLDS = [0.35, 0.40, 0.45, 0.50, 0.55]
AGE_THRESHOLDS = [4, 6, 8, 10, 15, 999]   # 999 = no freshness filter (nests pure-ER)
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


def make_gate(er_th, age_th):
    def gate(adx_val, er_val, zone_age=None):
        if er_val is None or er_val < er_th:
            return False
        if age_th < 999 and (zone_age is None or zone_age > age_th):
            return False
        return True
    return gate


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

    oos_combo_all, oos_pure_er_all, oos_ungated_all = [], [], []

    for fold in range(1, 4):
        train_m5 = m5_candles[:bounds[fold]]
        test_m5 = quarters[fold]
        print(f"=== FOLD {fold}: train=Q1..Q{fold} ({len(train_m5)} bars), test=Q{fold+1} ({len(test_m5)} bars) ===")

        best = None
        best_pure_er_at_same_erth = {}
        for er_th in ER_THRESHOLDS:
            for age_th in AGE_THRESHOLDS:
                gate = make_gate(er_th, age_th)
                res = run_segment(h1_candles, m15_candles, train_m5, SPREAD, SLIPPAGE, True,
                                   f"train-f{fold}-er{er_th}-age{age_th}", regime_gate=gate,
                                   h1_adx=h1_adx, h1_er=h1_er)
                s = stats(res["trade_log"])
                if age_th == 999:
                    best_pure_er_at_same_erth[er_th] = s
                if s["n"] < MIN_TRAIN_N:
                    continue
                print(f"    train er>={er_th} age<={age_th}: n={s['n']} mean=${s['mean']} pf={s['pf']}")
                if best is None or s["mean"] > best[2]["mean"]:
                    best = (er_th, age_th, s)

        if best is None:
            print("  No combo cleared the minimum train trade count -- skipping fold.")
            continue
        chosen_er, chosen_age, train_stats = best
        pure_er_comparable = best_pure_er_at_same_erth.get(chosen_er)
        print(f"  CHOSEN (in-sample): ER>={chosen_er} age<={chosen_age}  train_n={train_stats['n']} "
              f"train_mean=${train_stats['mean']}  (pure-ER at same threshold: {pure_er_comparable})")

        combo_gate = make_gate(chosen_er, chosen_age)
        pure_er_gate = make_gate(chosen_er, 999)
        test_combo = run_segment(h1_candles, m15_candles, test_m5, SPREAD, SLIPPAGE, True,
                                  f"test-f{fold}-combo", regime_gate=combo_gate, h1_adx=h1_adx, h1_er=h1_er)
        test_pure_er = run_segment(h1_candles, m15_candles, test_m5, SPREAD, SLIPPAGE, True,
                                    f"test-f{fold}-pureER", regime_gate=pure_er_gate, h1_adx=h1_adx, h1_er=h1_er)
        test_ungated = run_segment(h1_candles, m15_candles, test_m5, SPREAD, SLIPPAGE, True,
                                    f"test-f{fold}-ungated", h1_adx=h1_adx, h1_er=h1_er)

        cs, ps, us = stats(test_combo["trade_log"]), stats(test_pure_er["trade_log"]), stats(test_ungated["trade_log"])
        print(f"  TEST (OOS) combo (ER>={chosen_er} age<={chosen_age}): {cs}")
        print(f"  TEST (OOS) pure-ER (ER>={chosen_er} only):          {ps}")
        print(f"  TEST (OOS) ungated:                                  {us}")
        print()
        oos_combo_all.extend(test_combo["trade_log"])
        oos_pure_er_all.extend(test_pure_er["trade_log"])
        oos_ungated_all.extend(test_ungated["trade_log"])

    print("=== AGGREGATE ACROSS ALL 3 OOS FOLDS ===")
    cs_all, ps_all, us_all = stats(oos_combo_all), stats(oos_pure_er_all), stats(oos_ungated_all)
    print("COMBO (ER+freshness):", cs_all)
    print("PURE-ER (same fold-chosen ER threshold, no freshness):", ps_all)
    print("UNGATED:", us_all)
    if cs_all["n"] > 0 and ps_all["n"] > 0:
        diff = cs_all["mean"] - ps_all["mean"]
        se_diff = math.sqrt(cs_all["se"] ** 2 + ps_all["se"] ** 2)
        t_diff = diff / se_diff if se_diff > 0 else float("nan")
        print(f"COMBO vs PURE-ER DIFF: ${diff:.2f}  Welch t={t_diff:.2f}  "
              f"95%CI=[${diff-1.96*se_diff:.2f}, ${diff+1.96*se_diff:.2f}]")


if __name__ == "__main__":
    main()
