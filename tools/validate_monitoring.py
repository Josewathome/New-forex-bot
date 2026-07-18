"""Regression test for src/application/strategy_v2/monitoring.py against
the test matrix: profit factor, expectancy/R-distribution, Sharpe,
drawdown/recovery factor, equity curve, monthly returns, trade duration,
and daily/weekly report bundling. Trade records match TradeJournal's
exact shape (src/infrastructure/trade_journal.py).

HOW TO RUN
    python3 tools/validate_monitoring.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from src.application.strategy_v2.monitoring import (
    profit_factor, expectancy_r, r_multiple_distribution, sharpe_ratio, annualize_sharpe,
    max_drawdown_amount, recovery_factor, equity_curve, monthly_returns, trade_duration_stats,
    performance_report, daily_report, weekly_report,
)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

def T(pnl, risk=None, outcome=None, opened="2026-01-01T00:00:00+00:00", closed="2026-01-01T02:00:00+00:00", symbol="EURUSD"):
    return {"ticket": 1, "symbol": symbol, "direction": "BUY", "entry": 1.1, "sl": 1.09, "tp": 1.12,
            "lot_size": 0.1, "grade": "A", "pnl": pnl, "outcome": outcome or ("WIN_FULL" if pnl > 0 else "LOSS"),
            "opened_at": opened, "closed_at": closed, "risk_amount": risk}

print("="*78); print("SECTION 1 — PROFIT FACTOR (M1)"); print("="*78)

trades_pf = [T(100), T(-50), T(200), T(-25)]
check("1a profit factor = gains/abs(losses)", abs(profit_factor(trades_pf) - (300/75)) < 1e-9, f"{profit_factor(trades_pf)}")
check("1b no losing trades: None (undefined)", profit_factor([T(100), T(50)]) is None)
check("1c no closed trades: None", profit_factor([]) is None)
pf_with_open = profit_factor([T(100), T(-50), {**T(-9999), "outcome": "OPEN", "pnl": None}])
check("1d an OPEN trade (huge hypothetical pnl, excluded) does not affect the result",
      pf_with_open is not None and abs(pf_with_open - 2.0) < 1e-9, f"{pf_with_open}")

print(); print("="*78); print("SECTION 2 — EXPECTANCY / R-MULTIPLE DISTRIBUTION (M2)"); print("="*78)

trades_r = [T(200, risk=100), T(-50, risk=100), T(150, risk=50)]
check("2a expectancy_r averages pnl/risk_amount", abs(expectancy_r(trades_r) - ((2.0 + -0.5 + 3.0)/3)) < 1e-9, f"{expectancy_r(trades_r)}")
check("2b trades missing risk_amount are skipped, not treated as 0", expectancy_r([T(100, risk=None)]) is None)
dist = r_multiple_distribution(trades_r)
check("2c r_multiple_distribution has correct count", dist["count"] == 3)
check("2d r_multiple_distribution max/min correct", abs(dist["max"] - 3.0) < 1e-9 and abs(dist["min"] - (-0.5)) < 1e-9)

print(); print("="*78); print("SECTION 3 — SHARPE RATIO (M3)"); print("="*78)

check("3a sharpe computed for a varying series", sharpe_ratio([10, -5, 20, -10, 15]) is not None)
check("3b single-trade series: None (need >=2 for stdev)", sharpe_ratio([10]) is None)
check("3c zero-variance series: None (would divide by zero)", sharpe_ratio([10, 10, 10]) is None)
check("3d annualize_sharpe scales by sqrt(periods_per_year)", abs(annualize_sharpe(1.0, 252) - (252 ** 0.5)) < 1e-9)

print(); print("="*78); print("SECTION 4 — DRAWDOWN / RECOVERY FACTOR"); print("="*78)

# Equity path: 10000 -> 10200 (peak) -> 10000 (dd=200) -> 10300 (new peak, recovered)
trades_dd = [T(200, closed="2026-01-01T01:00:00+00:00"), T(-200, closed="2026-01-01T02:00:00+00:00"),
             T(300, closed="2026-01-01T03:00:00+00:00")]
dd = max_drawdown_amount(trades_dd, 10000)
check("4a max drawdown amount computed correctly (200)", abs(dd - 200) < 1e-9, f"{dd}")
rf = recovery_factor(trades_dd, 10000)
check("4b recovery factor = net_profit / max_dd", abs(rf - (300/200)) < 1e-9, f"{rf}")
check("4c zero drawdown: recovery_factor is None", recovery_factor([T(100), T(50)], 10000) is None)
check("4d no closed trades: recovery_factor is None", recovery_factor([], 10000) is None)

print(); print("="*78); print("SECTION 5 — EQUITY CURVE"); print("="*78)

curve = equity_curve(trades_dd, 10000)
check("5a equity curve starts at initial_balance", curve[0]["equity"] == 10000)
check("5b equity curve ends at the correct final balance", abs(curve[-1]["equity"] - 10300) < 1e-9, f"{curve[-1]}")
check("5c equity curve has one entry per trade plus the starting point", len(curve) == len(trades_dd) + 1)

print(); print("="*78); print("SECTION 6 — MONTHLY RETURNS"); print("="*78)

trades_monthly = [
    T(100, closed="2026-01-15T00:00:00+00:00"), T(-50, closed="2026-01-20T00:00:00+00:00"),
    T(200, closed="2026-02-05T00:00:00+00:00"),
]
mr = monthly_returns(trades_monthly, 10000)
check("6a two distinct months present", set(mr.keys()) == {"2026-01", "2026-02"})
check("6b January return computed against the starting-of-January equity",
      abs(mr["2026-01"] - (50/10000*100)) < 1e-6, f"{mr}")
check("6c February return computed against equity AFTER January's net effect",
      abs(mr["2026-02"] - (200/10050*100)) < 1e-4, f"{mr}")

print(); print("="*78); print("SECTION 7 — TRADE DURATION"); print("="*78)

trades_dur = [T(100, opened="2026-01-01T00:00:00+00:00", closed="2026-01-01T02:00:00+00:00"),
              T(50, opened="2026-01-01T00:00:00+00:00", closed="2026-01-02T00:00:00+00:00")]
dur = trade_duration_stats(trades_dur)
check("7a avg duration computed correctly ((2+24)/2=13h)", abs(dur["avg_hours"] - 13.0) < 1e-9, f"{dur}")
check("7b max duration correct (24h)", abs(dur["max_hours"] - 24.0) < 1e-9)
check("7c OPEN trades (no closed_at) excluded", trade_duration_stats([{**T(100), "outcome": "OPEN", "closed_at": None}]) is None)

print(); print("="*78); print("SECTION 8 — PERFORMANCE REPORT / DAILY / WEEKLY BUNDLING"); print("="*78)

report = performance_report(trades_pf, 10000)
check("8a performance_report includes total_trades/win_rate/profit_factor", report["total_trades"] == 4 and "profit_factor" in report)

daily = daily_report(trades_monthly, "2026-01-15", 10000)
check("8b daily_report filters to exactly the matching date", daily["total_trades"] == 1 and daily["date"] == "2026-01-15")

weekly = weekly_report(trades_monthly, "2026-01-14", "2026-01-21", 10000)
check("8c weekly_report filters to the inclusive date range (2 trades in mid-January)", weekly["total_trades"] == 2)

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
