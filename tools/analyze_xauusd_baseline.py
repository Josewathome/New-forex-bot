"""RESEARCH: full statistical validation of the XAUUSD V1 (3-Step EA)
baseline, with realistic costs already applied (tools/backtest_smc_3step.py
apply_costs=True). Answers the user's Priority 1 research question: "Can
XAUUSD become consistently profitable after realistic trading costs?"

Computes: PF, expectancy, win rate, max drawdown, max consecutive losses,
trade duration, MAE/MFE, Sharpe/Sortino/MAR (from a daily equity curve),
walk-forward window consistency, in-sample/out-of-sample split, Monte
Carlo trade-order reshuffle (equity/drawdown distribution), and a
bootstrap confidence interval on PF/expectancy.

HOW TO RUN
    python3 tools/analyze_xauusd_baseline.py
"""
import sys, random, math
from datetime import datetime, timezone
sys.path.insert(0, ".")

from tools.backtest_smc_3step import run_backtest, INITIAL_BALANCE

random.seed(1234)   # reproducible Monte Carlo / bootstrap


def parse_ts(s):
    return datetime.fromisoformat(s)


def max_drawdown(equity_curve):
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        dd = (peak - e) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def max_consecutive_losses(trades):
    worst = cur = 0
    for t in trades:
        if t["pnl"] < 0:
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0
    return worst


def basic_stats(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gain = sum(t["pnl"] for t in wins)
    loss = abs(sum(t["pnl"] for t in losses))
    pf = gain / loss if loss > 0 else None
    total_pnl = sum(t["pnl"] for t in trades)
    return {
        "n": n, "win_rate_pct": round(100.0 * len(wins) / n, 1),
        "total_pnl": round(total_pnl, 2), "profit_factor": round(pf, 3) if pf else None,
        "expectancy": round(total_pnl / n, 2),
    }


def build_daily_equity(trades, initial_balance):
    """Marks equity to market at each trade CLOSE, forward-filled daily."""
    if not trades:
        return []
    events = sorted(trades, key=lambda t: parse_ts(t["closed_at"]))
    start_day = parse_ts(events[0]["opened_at"]).date()
    end_day = parse_ts(events[-1]["closed_at"]).date()
    daily = {}
    equity = initial_balance
    idx = 0
    day = start_day
    from datetime import timedelta
    while day <= end_day:
        while idx < len(events) and parse_ts(events[idx]["closed_at"]).date() <= day:
            equity += events[idx]["pnl"]
            idx += 1
        daily[day] = equity
        day += timedelta(days=1)
    return list(daily.values())


def sharpe_sortino_mar(daily_equity):
    if len(daily_equity) < 3:
        return {"sharpe": None, "sortino": None, "mar": None, "cagr_pct": None}
    rets = [(daily_equity[i] / daily_equity[i-1]) - 1.0 for i in range(1, len(daily_equity))
            if daily_equity[i-1] > 0]
    if not rets:
        return {"sharpe": None, "sortino": None, "mar": None, "cagr_pct": None}
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    downside = [r for r in rets if r < 0]
    down_var = sum(r ** 2 for r in downside) / len(rets) if downside else 0.0
    down_std = math.sqrt(down_var)
    sharpe = (mean_r / std) * math.sqrt(252) if std > 0 else None
    sortino = (mean_r / down_std) * math.sqrt(252) if down_std > 0 else None
    n_years = len(daily_equity) / 252.0
    total_return = daily_equity[-1] / daily_equity[0]
    cagr = total_return ** (1.0 / n_years) - 1.0 if n_years > 0 and total_return > 0 else None
    mdd = max_drawdown(daily_equity)
    mar = cagr / mdd if (cagr is not None and mdd > 0) else None
    return {
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "mar": round(mar, 2) if mar is not None else None,
        "cagr_pct": round(cagr * 100, 1) if cagr is not None else None,
    }


def walk_forward(trades, n_windows=4):
    trades_sorted = sorted(trades, key=lambda t: parse_ts(t["opened_at"]))
    n = len(trades_sorted)
    size = n // n_windows
    windows = []
    for w in range(n_windows):
        start = w * size
        end = n if w == n_windows - 1 else (w + 1) * size
        chunk = trades_sorted[start:end]
        stats = basic_stats(chunk)
        if chunk:
            stats["period"] = f"{parse_ts(chunk[0]['opened_at']).date()} to {parse_ts(chunk[-1]['closed_at']).date()}"
        windows.append(stats)
    return windows


def out_of_sample_split(trades, in_sample_frac=0.7):
    trades_sorted = sorted(trades, key=lambda t: parse_ts(t["opened_at"]))
    cut = int(len(trades_sorted) * in_sample_frac)
    return basic_stats(trades_sorted[:cut]), basic_stats(trades_sorted[cut:])


def monte_carlo_reshuffle(trades, initial_balance, n_sims=2000):
    """Reorders the SAME set of realized trade PnLs (drawn from the
    original, order-dependent equity-based position sizing -- not
    re-simulated per shuffle). Final equity is therefore mathematically
    invariant (sum of the same numbers regardless of order) and NOT
    reported as a distribution -- only path-dependent quantities
    (max drawdown, longest losing streak) actually vary with sequence,
    which is what this technique is for: isolating "did we get a lucky
    ORDER" from "did we get a lucky TOTAL.\""""
    pnls = [t["pnl"] for t in trades]
    max_dds, worst_streaks = [], []
    for _ in range(n_sims):
        shuffled = pnls[:]
        random.shuffle(shuffled)
        equity = initial_balance
        curve = [equity]
        streak = worst_streak = 0
        for p in shuffled:
            equity += p
            curve.append(equity)
            streak = streak + 1 if p < 0 else 0
            worst_streak = max(worst_streak, streak)
        max_dds.append(max_drawdown(curve))
        worst_streaks.append(worst_streak)
    max_dds.sort()
    worst_streaks.sort()

    def pct(arr, p):
        idx = int(len(arr) * p)
        return arr[min(idx, len(arr) - 1)]

    return {
        "final_equity_invariant": round(initial_balance + sum(pnls), 2),
        "max_drawdown_p50_pct": round(pct(max_dds, 0.50) * 100, 1),
        "max_drawdown_p95_pct": round(pct(max_dds, 0.95) * 100, 1),
        "max_consecutive_losses_p50": pct(worst_streaks, 0.50),
        "max_consecutive_losses_p95": pct(worst_streaks, 0.95),
    }


def bootstrap_ci(trades, n_resamples=5000):
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    pfs, expectancies = [], []
    for _ in range(n_resamples):
        sample = [pnls[random.randrange(n)] for _ in range(n)]
        gain = sum(p for p in sample if p > 0)
        loss = abs(sum(p for p in sample if p <= 0))
        pfs.append(gain / loss if loss > 0 else float("inf"))
        expectancies.append(sum(sample) / n)
    pfs.sort()
    expectancies.sort()

    def pct(arr, p):
        idx = int(len(arr) * p)
        return arr[min(idx, len(arr) - 1)]

    finite_pfs = [p for p in pfs if p != float("inf")]
    return {
        "pf_ci_2.5": round(pct(finite_pfs, 0.025), 3) if finite_pfs else None,
        "pf_ci_50": round(pct(finite_pfs, 0.50), 3) if finite_pfs else None,
        "pf_ci_97.5": round(pct(finite_pfs, 0.975), 3) if finite_pfs else None,
        "expectancy_ci_2.5": round(pct(expectancies, 0.025), 2),
        "expectancy_ci_50": round(pct(expectancies, 0.50), 2),
        "expectancy_ci_97.5": round(pct(expectancies, 0.975), 2),
        "pct_resamples_expectancy_positive": round(100.0 * sum(1 for e in expectancies if e > 0) / n_resamples, 1),
    }


if __name__ == "__main__":
    print("Running XAUUSD backtest with realistic costs...")
    trade_log, per_symbol_summary, final_equity = run_backtest(apply_costs=True, symbols=["XAUUSD"])
    print(f"Loaded {len(trade_log)} trades.\n")

    print("="*78); print("1. BASIC STATS (with realistic costs)"); print("="*78)
    stats = basic_stats(trade_log)
    for k, v in stats.items():
        print(f"  {k}: {v}")

    durations_hr = [(parse_ts(t["closed_at"]) - parse_ts(t["opened_at"])).total_seconds() / 3600.0 for t in trade_log]
    durations_hr.sort()
    print(f"  trade_duration_hours: min={durations_hr[0]:.2f} median={durations_hr[len(durations_hr)//2]:.2f} "
          f"mean={sum(durations_hr)/len(durations_hr):.2f} max={durations_hr[-1]:.2f}")
    print(f"  max_consecutive_losses: {max_consecutive_losses(sorted(trade_log, key=lambda t: parse_ts(t['opened_at'])))}")

    equity_curve = [INITIAL_BALANCE]
    running = INITIAL_BALANCE
    for t in sorted(trade_log, key=lambda t: parse_ts(t["closed_at"])):
        running += t["pnl"]
        equity_curve.append(running)
    print(f"  max_drawdown_pct: {max_drawdown(equity_curve)*100:.2f}")

    mfe_vals = [t["mfe_r"] for t in trade_log if t["mfe_r"] is not None]
    mae_vals = [t["mae_r"] for t in trade_log if t["mae_r"] is not None]
    print(f"  mfe_r: mean={sum(mfe_vals)/len(mfe_vals):.2f} max={max(mfe_vals):.2f}")
    print(f"  mae_r: mean={sum(mae_vals)/len(mae_vals):.2f} max={max(mae_vals):.2f}")

    print(); print("="*78); print("2. RISK-ADJUSTED METRICS (daily equity curve)"); print("="*78)
    daily_eq = build_daily_equity(trade_log, INITIAL_BALANCE)
    print(f"  trading days spanned: {len(daily_eq)}")
    for k, v in sharpe_sortino_mar(daily_eq).items():
        print(f"  {k}: {v}")

    print(); print("="*78); print("3. WALK-FORWARD (4 sequential windows, no reoptimization)"); print("="*78)
    for i, w in enumerate(walk_forward(trade_log, 4)):
        print(f"  window {i+1}: {w}")

    print(); print("="*78); print("4. OUT-OF-SAMPLE SPLIT (70% in-sample / 30% out-of-sample, chronological)"); print("="*78)
    in_sample, out_sample = out_of_sample_split(trade_log, 0.7)
    print(f"  in-sample:      {in_sample}")
    print(f"  out-of-sample:  {out_sample}")

    print(); print("="*78); print("5. MONTE CARLO (2000 trade-order reshuffles)"); print("="*78)
    for k, v in monte_carlo_reshuffle(trade_log, INITIAL_BALANCE, 2000).items():
        print(f"  {k}: {v}")

    print(); print("="*78); print("6. BOOTSTRAP CONFIDENCE INTERVAL (5000 resamples with replacement)"); print("="*78)
    for k, v in bootstrap_ci(trade_log, 5000).items():
        print(f"  {k}: {v}")
