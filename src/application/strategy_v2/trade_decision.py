# src/application/strategy_v2/trade_decision.py
#
# Trade Decision Engine — the qualification gate between a produced
# entry_confirmation.EntrySignal and an actually-executable trade: entry
# qualification, direction filtering (one trade per symbol), risk
# validation (portfolio-level limits, from config_v2), session validation
# (London/NY window), news validation, with an explicit rejection reason
# for every failing gate. Pure composition + a handful of new, well-
# specified account-state gates -- no structure/FVG/OB/liquidity/premium-
# discount/entry-confirmation logic reimplemented, and no new position-
# sizing math (that is Risk Management's job, Module 6).
#
# DOCUMENTED IMPLEMENTATION DECISIONS:
#
#   T1. News validation is expressed as a generic callable
#       `(symbol, now) -> bool`, not a hard import of the concrete
#       NewsManager class -- production code passes `news_manager.
#       is_blocked` directly (identical signature), tests pass a stub.
#       This keeps strategy_v2 decoupled from the framework's repository
#       wiring, matching this module's existing dependency-injection style
#       elsewhere in the codebase (execution_service.py).
#   T2. Session validation gates ONLY this qualification step (entry),
#       never sweep/POI/structure detection -- matches the plan's already-
#       locked sequencing decision ("the session window gates only the
#       confirmation+entry step").
#   T3. Gate order (first failing gate wins, matching execution_service.py's
#       existing tiered-check style): entry qualification -> direction
#       filtering -> risk validation (concurrent trades, portfolio
#       exposure, daily loss limit, max drawdown) -> session -> news. This
#       is a reporting-order choice (which reason gets surfaced when
#       several would independently fail), not a correctness dependency
#       between the checks.
#   T4. Position sizing itself (how much to risk in %) is Risk
#       Management's responsibility (Module 6) -- this module only checks
#       whether a GIVEN intended risk_pct would breach portfolio-level
#       limits, it does not decide what that risk_pct should be.

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.entry_confirmation import EntrySignal


class RejectionReason(Enum):
    NO_SIGNAL = "no_signal"
    SYMBOL_ALREADY_HAS_POSITION = "symbol_already_has_position"
    MAX_CONCURRENT_TRADES_REACHED = "max_concurrent_trades_reached"
    PORTFOLIO_EXPOSURE_EXCEEDED = "portfolio_exposure_exceeded"
    DAILY_LOSS_LIMIT_HIT = "daily_loss_limit_hit"
    MAX_DRAWDOWN_HIT = "max_drawdown_hit"
    OUTSIDE_TRADING_SESSION = "outside_trading_session"
    NEWS_BLOCKED = "news_blocked"


@dataclass
class AccountState:
    open_trades_count: int             # across all symbols
    open_trades_for_symbol: int        # this signal's symbol only
    portfolio_exposure_pct: float       # sum of currently-open trades' risk %, before this one
    today_realized_pnl_pct: float       # negative = loss, e.g. -3.2 means -3.2% today
    current_drawdown_pct: float         # positive magnitude, e.g. 8.5 means 8.5% drawdown from peak


@dataclass
class TradeDecision:
    qualified: bool
    reason: Optional[RejectionReason]
    signal: Optional[EntrySignal]


def in_trading_session(now: datetime) -> bool:
    """T2 -- London OR New York window, UTC, no DST (config_v2 §13). Same
    tz-normalization convention as liquidity.py's _hour_in."""
    hour = now.astimezone(timezone.utc).hour
    for start, end in (config_v2.LONDON_SESSION_UTC, config_v2.NEWYORK_SESSION_UTC):
        if start <= hour < end:
            return True
    return False


def qualify_trade(
    signal: Optional[EntrySignal],
    symbol: str,
    now: datetime,
    account: AccountState,
    intended_risk_pct: float,
    news_check: Callable[[str, datetime], bool],
) -> TradeDecision:
    """T3 -- first failing gate wins. Returns a TradeDecision with an
    explicit RejectionReason on failure, or qualified=True with the signal
    attached on success."""
    if signal is None:
        return TradeDecision(qualified=False, reason=RejectionReason.NO_SIGNAL, signal=None)

    if account.open_trades_for_symbol >= config_v2.MAX_TRADES_PER_SYMBOL:
        return TradeDecision(qualified=False, reason=RejectionReason.SYMBOL_ALREADY_HAS_POSITION, signal=signal)

    if account.open_trades_count >= config_v2.MAX_CONCURRENT_TRADES:
        return TradeDecision(qualified=False, reason=RejectionReason.MAX_CONCURRENT_TRADES_REACHED, signal=signal)

    if account.portfolio_exposure_pct + intended_risk_pct > config_v2.MAX_PORTFOLIO_EXPOSURE_PCT:
        return TradeDecision(qualified=False, reason=RejectionReason.PORTFOLIO_EXPOSURE_EXCEEDED, signal=signal)

    if account.today_realized_pnl_pct <= -config_v2.DAILY_LOSS_LIMIT_PCT:
        return TradeDecision(qualified=False, reason=RejectionReason.DAILY_LOSS_LIMIT_HIT, signal=signal)

    if account.current_drawdown_pct >= config_v2.MAX_DRAWDOWN_PCT:
        return TradeDecision(qualified=False, reason=RejectionReason.MAX_DRAWDOWN_HIT, signal=signal)

    if not in_trading_session(now):
        return TradeDecision(qualified=False, reason=RejectionReason.OUTSIDE_TRADING_SESSION, signal=signal)

    if news_check(symbol, now):
        return TradeDecision(qualified=False, reason=RejectionReason.NEWS_BLOCKED, signal=signal)

    return TradeDecision(qualified=True, reason=None, signal=signal)
