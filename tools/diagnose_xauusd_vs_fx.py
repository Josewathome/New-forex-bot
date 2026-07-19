"""RESEARCH Priority 2: why does XAUUSD outperform the FX pairs?
Diagnostic only -- compares trade characteristics across symbols
(with realistic costs already applied) to find the mechanical driver.

HOW TO RUN
    python3 tools/diagnose_xauusd_vs_fx.py
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime

from tools.backtest_smc_3step import run_backtest


def parse_ts(s):
    return datetime.fromisoformat(s)


def segment_stats(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    gain = sum(t["pnl"] for t in wins)
    loss = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
    pf = round(gain / loss, 2) if loss > 0 else None
    return {"n": n, "win_rate_pct": round(100.0 * len(wins) / n, 1), "pf": pf,
            "expectancy": round(sum(t["pnl"] for t in trades) / n, 2)}


if __name__ == "__main__":
    print("Running all 4 symbols with realistic costs...")
    trade_log, summary, final_equity = run_backtest(apply_costs=True)
    print()

    by_symbol = {}
    for t in trade_log:
        by_symbol.setdefault(t["symbol"], []).append(t)

    print("="*78); print("1. ZONE TYPE (Order Block vs Fair Value Gap)"); print("="*78)
    for sym, trades in by_symbol.items():
        ob_trades = [t for t in trades if t["zone_is_ob"]]
        fvg_trades = [t for t in trades if not t["zone_is_ob"]]
        print(f"  {sym}: OB={segment_stats(ob_trades)}  FVG={segment_stats(fvg_trades)}")

    print(); print("="*78); print("2. DIRECTION (bullish vs bearish)"); print("="*78)
    for sym, trades in by_symbol.items():
        bull = [t for t in trades if t["direction"] == "bullish"]
        bear = [t for t in trades if t["direction"] == "bearish"]
        print(f"  {sym}: bullish={segment_stats(bull)}  bearish={segment_stats(bear)}")

    print(); print("="*78); print("3. ENTRY SESSION (Asian 00-06 / London 06-13 / NY 13-21 / Other, UTC)"); print("="*78)
    def session_of(hour):
        if 0 <= hour < 6:
            return "asian"
        if 6 <= hour < 13:
            return "london"
        if 13 <= hour < 21:
            return "newyork"
        return "other"
    for sym, trades in by_symbol.items():
        print(f"  {sym}:")
        by_session = {}
        for t in trades:
            by_session.setdefault(session_of(t["entry_hour_utc"]), []).append(t)
        for sess in ["asian", "london", "newyork", "other"]:
            if sess in by_session:
                print(f"    {sess}: {segment_stats(by_session[sess])}")

    print(); print("="*78); print("4. MAE/MFE (excursion in R-multiples)"); print("="*78)
    for sym, trades in by_symbol.items():
        mfe = [t["mfe_r"] for t in trades if t["mfe_r"] is not None]
        mae = [t["mae_r"] for t in trades if t["mae_r"] is not None]
        print(f"  {sym}: mfe_mean={sum(mfe)/len(mfe):.2f} mfe_median={sorted(mfe)[len(mfe)//2]:.2f} "
              f"mae_mean={sum(mae)/len(mae):.2f} mae_median={sorted(mae)[len(mae)//2]:.2f}")

    print(); print("="*78); print("5. TRADE DURATION (hours)"); print("="*78)
    for sym, trades in by_symbol.items():
        durs = sorted((parse_ts(t["closed_at"]) - parse_ts(t["opened_at"])).total_seconds() / 3600.0 for t in trades)
        print(f"  {sym}: median={durs[len(durs)//2]:.2f} mean={sum(durs)/len(durs):.2f} max={durs[-1]:.2f}")

    print(); print("="*78); print("6. WINNERS vs LOSERS: how far in R did winners run vs where losers reversed"); print("="*78)
    for sym, trades in by_symbol.items():
        wins = [t for t in trades if t["pnl"] > 0 and t["mfe_r"] is not None]
        losses = [t for t in trades if t["pnl"] <= 0 and t["mae_r"] is not None]
        w_mfe = sum(t["mfe_r"] for t in wins) / len(wins) if wins else None
        l_mae = sum(t["mae_r"] for t in losses) / len(losses) if losses else None
        print(f"  {sym}: winners_avg_mfe_r={w_mfe:.2f}  losers_avg_mae_r={l_mae:.2f}" if wins and losses else f"  {sym}: insufficient data")
