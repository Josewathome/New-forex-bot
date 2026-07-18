# src/application/strategy_v2/monitoring.py
#
# Monitoring — statistics and reporting on top of the existing
# TradeJournal record shape ({ticket, symbol, direction, entry, sl, tp,
# lot_size, grade, pnl, outcome, opened_at, closed_at}, per
# src/infrastructure/trade_journal.py, untouched). Adds the metrics
# TradeJournal.get_stats/get_drawdown/get_stats_by_symbol do NOT already
# compute (profit factor, expectancy in R, Sharpe-style ratio, recovery
# factor, R-multiple distribution, equity curve, monthly returns, trade
# duration) plus daily/weekly report bundling. The existing dashboard
# (src/api/server.py's DashboardServer) and journal persistence are
# reused as-is -- this module is pure statistics functions over trade
# records, no new persistence/serving layer.
#
# These same functions are reused, unmodified, for the final strategy
# evaluation (multi-year backtest / walk-forward / Monte Carlo report) --
# built once here, not duplicated later.
#
# DOCUMENTED IMPLEMENTATION DECISIONS:
#
#   M1. profit_factor uses PnL SIGN only (sum of positive PnL / abs(sum
#       of negative PnL)) -- the standard definition, not the outcome-
#       string categories (WIN_FULL/WIN_PARTIAL/LOSS/MONITOR_CLOSE),
#       since a MONITOR_CLOSE could be either a small win or a small loss
#       and PnL sign is the unambiguous source of truth.
#   M2. R-multiple stats (expectancy_r, r_multiple_distribution) require
#       each trade dict to carry a `risk_amount` key (the ORIGINAL
#       monetary risk at open, e.g. from risk_management.py's lot_size *
#       SL distance) -- TradeJournal's existing record shape does not
#       store this, so these functions accept it as an optional per-trade
#       field and simply skip trades that lack it (documented, not a
#       silent assumption).
#   M3. sharpe_ratio computes the RAW per-trade-sequence ratio
#       (mean/stdev of the PnL-or-return series) -- annualization requires
#       a periods-per-year assumption this module cannot know (it depends
#       on trade frequency, which is a business decision), so
#       annualize_sharpe() takes that as an explicit parameter rather than
#       guessing a trading-day convention.
#   M4. Trade duration requires both opened_at and closed_at (ISO
#       strings, matching TradeJournal's own format) -- OPEN trades
#       (closed_at is None) are excluded.

from __future__ import annotations
import statistics as _stats
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _closed(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [t for t in trades if t.get("outcome") != "OPEN" and t.get("pnl") is not None]


def profit_factor(trades: List[Dict[str, Any]]) -> Optional[float]:
    """M1 -- sum(positive PnL) / abs(sum(negative PnL)). None if there are
    no losing trades (undefined/infinite) or no closed trades at all."""
    closed = _closed(trades)
    if not closed:
        return None
    gains = sum(t["pnl"] for t in closed if t["pnl"] > 0)
    losses = sum(t["pnl"] for t in closed if t["pnl"] < 0)
    if losses == 0:
        return None
    return gains / abs(losses)


def expectancy_r(trades: List[Dict[str, Any]]) -> Optional[float]:
    """M2 -- average R-multiple (pnl / risk_amount) across trades that
    carry a risk_amount field. None if no trade has one."""
    r_values = [t["pnl"] / t["risk_amount"] for t in _closed(trades)
                if t.get("risk_amount") not in (None, 0)]
    if not r_values:
        return None
    return _stats.mean(r_values)


def r_multiple_distribution(trades: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """M2 -- summary stats of the R-multiple distribution."""
    r_values = [t["pnl"] / t["risk_amount"] for t in _closed(trades)
                if t.get("risk_amount") not in (None, 0)]
    if not r_values:
        return None
    return {
        "count": len(r_values), "mean": _stats.mean(r_values),
        "median": _stats.median(r_values), "min": min(r_values), "max": max(r_values),
        "stdev": _stats.stdev(r_values) if len(r_values) > 1 else 0.0,
    }


def sharpe_ratio(returns: List[float]) -> Optional[float]:
    """M3 -- raw (non-annualized) mean/stdev ratio over a return series
    (e.g. per-trade PnL or per-trade R-multiples)."""
    if len(returns) < 2:
        return None
    sd = _stats.stdev(returns)
    if sd == 0:
        return None
    return _stats.mean(returns) / sd


def annualize_sharpe(raw_sharpe: float, periods_per_year: float) -> float:
    """M3 -- caller supplies the periods-per-year assumption explicitly."""
    return raw_sharpe * (periods_per_year ** 0.5)


def max_drawdown_amount(trades: List[Dict[str, Any]], initial_balance: float) -> float:
    """Max drawdown in CURRENCY (not %) -- needed for recovery_factor.
    Chronological order assumed to match closed_at ascending; caller
    should pre-sort if trades aren't already in that order."""
    equity = initial_balance
    peak = equity
    max_dd = 0.0
    for t in sorted(_closed(trades), key=lambda t: t.get("closed_at") or ""):
        equity += t["pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def recovery_factor(trades: List[Dict[str, Any]], initial_balance: float) -> Optional[float]:
    """Net profit / max drawdown (currency). None if there was no drawdown
    (undefined) or no closed trades."""
    closed = _closed(trades)
    if not closed:
        return None
    net_profit = sum(t["pnl"] for t in closed)
    dd = max_drawdown_amount(trades, initial_balance)
    if dd == 0:
        return None
    return net_profit / dd


def equity_curve(trades: List[Dict[str, Any]], initial_balance: float) -> List[Dict[str, Any]]:
    """Running equity after each closed trade, chronological order."""
    equity = initial_balance
    curve = [{"time": None, "equity": equity}]
    for t in sorted(_closed(trades), key=lambda t: t.get("closed_at") or ""):
        equity += t["pnl"]
        curve.append({"time": t.get("closed_at"), "equity": equity})
    return curve


def monthly_returns(trades: List[Dict[str, Any]], initial_balance: float) -> Dict[str, float]:
    """% return per calendar month (YYYY-MM keys), computed against the
    equity level at the START of that month."""
    closed = sorted(_closed(trades), key=lambda t: t.get("closed_at") or "")
    monthly_pnl: Dict[str, float] = defaultdict(float)
    for t in closed:
        closed_at = t.get("closed_at")
        if not closed_at:
            continue
        month_key = closed_at[:7]  # "YYYY-MM" from an ISO timestamp
        monthly_pnl[month_key] += t["pnl"]

    result = {}
    equity = initial_balance
    for month in sorted(monthly_pnl):
        start_equity = equity
        equity += monthly_pnl[month]
        result[month] = round((monthly_pnl[month] / start_equity) * 100, 3) if start_equity != 0 else 0.0
    return result


def trade_duration_stats(trades: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """M4 -- avg/median/max trade duration in hours, requires both
    opened_at and closed_at."""
    durations = []
    for t in _closed(trades):
        opened_at, closed_at = t.get("opened_at"), t.get("closed_at")
        if not opened_at or not closed_at:
            continue
        d_open = datetime.fromisoformat(opened_at)
        d_close = datetime.fromisoformat(closed_at)
        durations.append((d_close - d_open).total_seconds() / 3600.0)
    if not durations:
        return None
    return {
        "count": len(durations), "avg_hours": _stats.mean(durations),
        "median_hours": _stats.median(durations), "max_hours": max(durations),
    }


def performance_report(trades: List[Dict[str, Any]], initial_balance: float) -> Dict[str, Any]:
    """Bundles every metric above into one report -- the surface a
    daily/weekly report or the final evaluation calls. Callers pre-filter
    `trades` to the desired period (day/week/all-time) before calling."""
    closed = _closed(trades)
    total_pnl = sum(t["pnl"] for t in closed) if closed else 0.0
    wins = [t for t in closed if t["pnl"] > 0]
    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl": round(total_pnl, 2),
        "profit_factor": profit_factor(trades),
        "expectancy_r": expectancy_r(trades),
        "r_multiple_distribution": r_multiple_distribution(trades),
        "max_drawdown_amount": max_drawdown_amount(trades, initial_balance),
        "recovery_factor": recovery_factor(trades, initial_balance),
        "sharpe_raw": sharpe_ratio([t["pnl"] for t in closed]) if closed else None,
        "trade_duration": trade_duration_stats(trades),
    }


def daily_report(trades: List[Dict[str, Any]], date: str, initial_balance: float) -> Dict[str, Any]:
    """`date` is a "YYYY-MM-DD" string; filters to trades closed that day."""
    day_trades = [t for t in trades if (t.get("closed_at") or "")[:10] == date]
    return {"date": date, **performance_report(day_trades, initial_balance)}


def weekly_report(trades: List[Dict[str, Any]], week_start: str, week_end: str, initial_balance: float) -> Dict[str, Any]:
    """`week_start`/`week_end` are "YYYY-MM-DD" strings, inclusive."""
    week_trades = [t for t in trades if week_start <= (t.get("closed_at") or "")[:10] <= week_end]
    return {"week_start": week_start, "week_end": week_end, **performance_report(week_trades, initial_balance)}
