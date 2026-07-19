"""Regression test for src/application/strategy_v2/trade_decision.py
against the test matrix: entry qualification, direction filtering, each
risk-validation limit, session validation, news validation, and gate
ordering (first failing gate wins).

HOW TO RUN
    python3 tools/validate_trade_decision.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timezone
from src.domain.entities import Direction, Timeframe
from src.domain.strategy_v2_entities import SwingPoint, SwingType
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.entry_confirmation import EntrySignal
from src.application.strategy_v2.trade_decision import (
    RejectionReason, AccountState, TradeDecision, in_trading_session, qualify_trade,
)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

def mk_signal():
    dummy_swing = SwingPoint(symbol="EURUSD", timeframe=Timeframe.M1, kind=SwingType.HIGH, price=1.1005,
                              candle_time=datetime(2026, 1, 1, 7, 50, tzinfo=timezone.utc), confirmed_at_index=10)
    return EntrySignal(symbol="EURUSD", direction=Direction.BULLISH, entry_price=1.1010,
                        stop_loss=1.0990, take_profit=1.1200, take_profit_source="htf_swing", rr=9.5,
                        poi_kind="ob", confirmation_swing=dummy_swing,
                        entry_time=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))

def clean_account():
    return AccountState(open_trades_count=0, open_trades_for_symbol=0, portfolio_exposure_pct=0.0,
                         today_realized_pnl_pct=0.0, current_drawdown_pct=0.0)

LONDON_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)   # hour 8 -> London
NY_TIME = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)      # hour 14 -> NY
OFF_HOURS = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)    # hour 12 -> neither
NO_NEWS = lambda symbol, now: False
NEWS_BLOCKED = lambda symbol, now: True

print("="*78); print("SECTION 1 — SESSION WINDOW"); print("="*78)

check("1a hour 8 UTC (London): in session", in_trading_session(LONDON_TIME))
check("1b hour 14 UTC (NY): in session", in_trading_session(NY_TIME))
check("1c hour 12 UTC (between sessions): NOT in session", not in_trading_session(OFF_HOURS))
check("1d hour 10 UTC (London exclusive end): NOT in session", not in_trading_session(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)))
check("1e hour 7 UTC (London inclusive start): in session", in_trading_session(datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc)))

print(); print("="*78); print("SECTION 2 — ENTRY QUALIFICATION (E1)"); print("="*78)

d = qualify_trade(None, "EURUSD", LONDON_TIME, clean_account(), 1.0, NO_NEWS)
check("2a None signal: rejected with NO_SIGNAL", not d.qualified and d.reason == RejectionReason.NO_SIGNAL)

print(); print("="*78); print("SECTION 3 — DIRECTION FILTERING (one trade per symbol)"); print("="*78)

acct = clean_account()
acct.open_trades_for_symbol = 1
d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, acct, 1.0, NO_NEWS)
check("3a symbol already has an open position: rejected", not d.qualified and d.reason == RejectionReason.SYMBOL_ALREADY_HAS_POSITION)

print(); print("="*78); print("SECTION 4 — RISK VALIDATION"); print("="*78)

acct = clean_account()
acct.open_trades_count = config_v2.MAX_CONCURRENT_TRADES
d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, acct, 1.0, NO_NEWS)
check("4a max concurrent trades reached: rejected", not d.qualified and d.reason == RejectionReason.MAX_CONCURRENT_TRADES_REACHED)

acct = clean_account()
acct.portfolio_exposure_pct = config_v2.MAX_PORTFOLIO_EXPOSURE_PCT - 0.5
d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, acct, 1.0, NO_NEWS)
check("4b intended risk would push exposure over the cap: rejected", not d.qualified and d.reason == RejectionReason.PORTFOLIO_EXPOSURE_EXCEEDED)

acct = clean_account()
acct.portfolio_exposure_pct = config_v2.MAX_PORTFOLIO_EXPOSURE_PCT - 1.0
d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, acct, 1.0, NO_NEWS)
check("4c intended risk exactly fills remaining exposure headroom: qualified", d.qualified, f"reason={d.reason}")

acct = clean_account()
acct.today_realized_pnl_pct = -config_v2.DAILY_LOSS_LIMIT_PCT
d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, acct, 1.0, NO_NEWS)
check("4d daily loss limit exactly hit: rejected", not d.qualified and d.reason == RejectionReason.DAILY_LOSS_LIMIT_HIT)

acct = clean_account()
acct.today_realized_pnl_pct = -(config_v2.DAILY_LOSS_LIMIT_PCT - 0.1)
d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, acct, 1.0, NO_NEWS)
check("4e daily loss just under the limit: qualified", d.qualified, f"reason={d.reason}")

acct = clean_account()
acct.current_drawdown_pct = config_v2.MAX_DRAWDOWN_PCT
d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, acct, 1.0, NO_NEWS)
check("4f max drawdown exactly hit: rejected", not d.qualified and d.reason == RejectionReason.MAX_DRAWDOWN_HIT)

print(); print("="*78); print("SECTION 5 — SESSION VALIDATION"); print("="*78)

d = qualify_trade(mk_signal(), "EURUSD", OFF_HOURS, clean_account(), 1.0, NO_NEWS)
check("5a outside London/NY: rejected", not d.qualified and d.reason == RejectionReason.OUTSIDE_TRADING_SESSION)

print(); print("="*78); print("SECTION 6 — NEWS VALIDATION"); print("="*78)

d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, clean_account(), 1.0, NEWS_BLOCKED)
check("6a news check returns True: rejected", not d.qualified and d.reason == RejectionReason.NEWS_BLOCKED)

print(); print("="*78); print("SECTION 7 — HAPPY PATH"); print("="*78)

d = qualify_trade(mk_signal(), "EURUSD", LONDON_TIME, clean_account(), 1.0, NO_NEWS)
check("7a all gates pass: qualified, no rejection reason, signal attached",
      d.qualified and d.reason is None and d.signal is not None)

print(); print("="*78); print("SECTION 8 — GATE ORDERING (first failing gate wins)"); print("="*78)

acct = clean_account()
acct.open_trades_for_symbol = 1              # would fail direction filtering
acct.open_trades_count = config_v2.MAX_CONCURRENT_TRADES  # would ALSO fail risk validation
d = qualify_trade(mk_signal(), "EURUSD", OFF_HOURS, acct, 1.0, NEWS_BLOCKED)  # AND session AND news
check("8a with every gate failing simultaneously, the FIRST one (direction filtering) is reported",
      d.reason == RejectionReason.SYMBOL_ALREADY_HAS_POSITION, f"got {d.reason}")

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
