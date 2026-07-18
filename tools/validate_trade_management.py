"""Regression test for src/application/strategy_v2/trade_management.py
against the test matrix: R-multiple computation, the fixed exit-step
ladder (breakeven/TP1/TP2), runner trailing (one-way), opposite-CHoCH
exit, time exit, emergency exit, and full evaluate_trade() priority
ordering.

HOW TO RUN
    python3 tools/validate_trade_management.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Direction
from src.domain.strategy_v2_entities import TrendState
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.trade_management import (
    ExitStep, EXIT_STEPS, TradeState, ManagementAction, current_r_multiple, breakeven_stop,
    evaluate_fixed_steps, trail_runner_stop, should_exit_on_opposite_choch, should_time_exit,
    should_emergency_exit, evaluate_trade,
)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

def mk_state(direction=Direction.BULLISH, entry=1.1000, risk=0.0020, sl=None, opened=T0):
    return TradeState(direction=direction, entry_price=entry, initial_risk=risk,
                       current_sl=sl if sl is not None else (entry - risk if direction == Direction.BULLISH else entry + risk),
                       opened_at=opened)

print("="*78); print("SECTION 1 — R-MULTIPLE COMPUTATION"); print("="*78)

check("1a BULLISH at +1R", abs(current_r_multiple(Direction.BULLISH, 1.1000, 1.1020, 0.0020) - 1.0) < 1e-9)
check("1b BULLISH at -1R (adverse)", abs(current_r_multiple(Direction.BULLISH, 1.1000, 1.0980, 0.0020) - (-1.0)) < 1e-9)
check("1c BEARISH at +1R", abs(current_r_multiple(Direction.BEARISH, 1.1000, 1.0980, 0.0020) - 1.0) < 1e-9)
check("1d zero initial_risk: returns 0.0 rather than dividing by zero", current_r_multiple(Direction.BULLISH, 1.1000, 1.1050, 0.0) == 0.0)

print(); print("="*78); print("SECTION 2 — BREAKEVEN STOP"); print("="*78)

check("2a BULLISH breakeven = entry + buffer", abs(breakeven_stop(Direction.BULLISH, 1.1000, 0.5, 0.0001) - 1.10005) < 1e-9)
check("2b BEARISH breakeven = entry - buffer", abs(breakeven_stop(Direction.BEARISH, 1.1000, 0.5, 0.0001) - 1.09995) < 1e-9)

print(); print("="*78); print("SECTION 3 — FIXED EXIT-STEP LADDER"); print("="*78)

state = mk_state()
# Price at exactly BREAKEVEN_R (2.0R) -> fires breakeven only.
price_at_2r = state.entry_price + config_v2.BREAKEVEN_R * state.initial_risk
actions = evaluate_fixed_steps(state, price_at_2r, "EURUSD")
check("3a at 2R: exactly one action (breakeven) fires", len(actions) == 1 and actions[0].action == "breakeven")
check("3b current_sl updated to breakeven", abs(state.current_sl - breakeven_stop(Direction.BULLISH, state.entry_price, config_v2.RUNNER_BE_BUFFER_PIPS, config_v2.pip_size("EURUSD"))) < 1e-9)
check("3c remaining_pct unchanged by breakeven alone", state.remaining_pct == 1.0)

# Advance to TP1_R (3.0R).
price_at_3r = state.entry_price + config_v2.TP1_R * state.initial_risk
actions2 = evaluate_fixed_steps(state, price_at_3r, "EURUSD")
check("3d at 3R: breakeven does NOT re-fire (already triggered)", all(a.action != "breakeven" for a in actions2))
check("3e at 3R: partial_close fires with TP1_CLOSE_PCT", len(actions2) == 1 and actions2[0].action == "partial_close" and actions2[0].close_pct == config_v2.TP1_CLOSE_PCT)
check("3f remaining_pct reduced by TP1_CLOSE_PCT", abs(state.remaining_pct - (1.0 - config_v2.TP1_CLOSE_PCT)) < 1e-9)
check("3g not yet runner_trailing (TP2 hasn't fired)", not state.is_runner_trailing)

# Advance to TP2_R (4.0R).
price_at_4r = state.entry_price + config_v2.TP2_R * state.initial_risk
actions3 = evaluate_fixed_steps(state, price_at_4r, "EURUSD")
check("3h at 4R: partial_close fires with TP2_CLOSE_PCT", len(actions3) == 1 and actions3[0].close_pct == config_v2.TP2_CLOSE_PCT)
check("3i remaining_pct fully reduced (1 - TP1 - TP2 = RUNNER_PCT)", abs(state.remaining_pct - config_v2.RUNNER_PCT) < 1e-9, f"{state.remaining_pct}")
check("3j all fixed steps fired: is_runner_trailing now True", state.is_runner_trailing)

print(); print("="*78); print("SECTION 4 — GAP-THROUGH (price jumps past multiple thresholds at once)"); print("="*78)

state_gap = mk_state()
price_gap = state_gap.entry_price + (config_v2.TP2_R + 1.0) * state_gap.initial_risk  # well past all 3 steps
actions_gap = evaluate_fixed_steps(state_gap, price_gap, "EURUSD")
check("4a a single evaluation fires ALL 3 steps when price gaps through them", len(actions_gap) == 3)
check("4b is_runner_trailing True after the gap-through", state_gap.is_runner_trailing)

print(); print("="*78); print("SECTION 5 — RUNNER TRAILING (one-way)"); print("="*78)

state_run = mk_state()
state_run.is_runner_trailing = True
state_run.current_sl = 1.1005
t1 = trail_runner_stop(state_run, current_price=1.1100, atr_value=0.0010)
check("5a trail fires and tightens the SL", t1 is not None and t1.new_sl > 1.1005, f"{t1}")
sl_after_first_trail = state_run.current_sl
t2 = trail_runner_stop(state_run, current_price=1.1080, atr_value=0.0010)  # price pulled back -- must NOT loosen
check("5b price pulling back does not loosen the trail (one-way)", t2 is None, f"{t2}")
check("5c current_sl unchanged after a would-be-loosening trail attempt", state_run.current_sl == sl_after_first_trail)
t3 = trail_runner_stop(state_run, current_price=1.1150, atr_value=0.0010)
check("5d new high enough price tightens the trail further", t3 is not None and t3.new_sl > sl_after_first_trail)
check("5e trail does nothing when not yet in runner-trailing state", trail_runner_stop(mk_state(), 1.1100, 0.0010) is None)
check("5f trail does nothing with atr_value=None", trail_runner_stop(state_run, 1.1200, None) is None)

print(); print("="*78); print("SECTION 6 — OPPOSITE-CHoCH EXIT"); print("="*78)

check("6a BULLISH trade, CHoCH invalidates BULLISH: exit",
      should_exit_on_opposite_choch(Direction.BULLISH, True, TrendState.BULLISH))
check("6b BULLISH trade, CHoCH invalidates BEARISH (unrelated): no exit",
      not should_exit_on_opposite_choch(Direction.BULLISH, True, TrendState.BEARISH))
check("6c no CHoCH fired: no exit", not should_exit_on_opposite_choch(Direction.BULLISH, False, TrendState.BULLISH))
check("6d CHoCH fired but invalidated_trend is None: no exit", not should_exit_on_opposite_choch(Direction.BULLISH, True, None))

print(); print("="*78); print("SECTION 7 — TIME EXIT"); print("="*78)

check("7a well within max duration: no exit", not should_time_exit(T0, T0 + timedelta(hours=10)))
check("7b exactly at max duration: exit", should_time_exit(T0, T0 + timedelta(hours=config_v2.MAX_TRADE_DURATION_HOURS)))
check("7c beyond max duration: exit", should_time_exit(T0, T0 + timedelta(hours=config_v2.MAX_TRADE_DURATION_HOURS + 1)))

print(); print("="*78); print("SECTION 8 — EMERGENCY EXIT"); print("="*78)

check("8a normal adverse move (within tolerance): no emergency exit",
      not should_emergency_exit(Direction.BULLISH, 1.1000, 1.0990, 0.0020))
check("8b extreme adverse move (beyond EMERGENCY_MAX_ADVERSE_R): emergency exit",
      should_emergency_exit(Direction.BULLISH, 1.1000, 1.1000 - (config_v2.EMERGENCY_MAX_ADVERSE_R + 0.5) * 0.0020, 0.0020))
check("8c external emergency flag forces exit regardless of price",
      should_emergency_exit(Direction.BULLISH, 1.1000, 1.1050, 0.0020, external_emergency_flag=True))

print(); print("="*78); print("SECTION 9 — FULL evaluate_trade() PRIORITY ORDERING"); print("="*78)

# Emergency takes priority even if other conditions would also fire.
state9a = mk_state(opened=T0)
result9a = evaluate_trade(state9a, current_price=1.1000 - 10*0.0020, symbol="EURUSD",
                           now=T0 + timedelta(hours=200), atr_value=0.0010,
                           choch_fired=True, invalidated_trend=TrendState.BULLISH)
check("9a emergency exit takes priority over time/CHoCH/ladder", len(result9a) == 1 and result9a[0].action == "full_close" and result9a[0].reason == "emergency")

# Opposite CHoCH takes priority over time exit and the ladder (but not emergency).
state9b = mk_state(opened=T0)
result9b = evaluate_trade(state9b, current_price=1.1020, symbol="EURUSD",
                           now=T0 + timedelta(hours=200), atr_value=0.0010,
                           choch_fired=True, invalidated_trend=TrendState.BULLISH)
check("9b opposite-CHoCH exit takes priority over time exit and the ladder",
      len(result9b) == 1 and result9b[0].action == "full_close" and result9b[0].reason == "opposite_choch")

# Time exit fires when nothing else does.
state9c = mk_state(opened=T0)
result9c = evaluate_trade(state9c, current_price=1.1005, symbol="EURUSD",
                           now=T0 + timedelta(hours=200), atr_value=0.0010)
check("9c time exit fires when no emergency/CHoCH condition is present",
      len(result9c) == 1 and result9c[0].action == "full_close" and result9c[0].reason == "time_exit")

# Normal ladder progression when nothing exceptional is happening.
state9d = mk_state(opened=T0)
price_2r = state9d.entry_price + config_v2.BREAKEVEN_R * state9d.initial_risk
result9d = evaluate_trade(state9d, current_price=price_2r, symbol="EURUSD",
                           now=T0 + timedelta(hours=1), atr_value=0.0010)
check("9d normal path: breakeven fires via the ladder, no full_close", any(a.action == "breakeven" for a in result9d) and not any(a.action == "full_close" for a in result9d))

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
