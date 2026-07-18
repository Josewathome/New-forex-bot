# src/application/strategy_v2/trade_management.py
#
# Trade Management — post-entry lifecycle: break-even, partial close (a
# parameterized list of R-multiple steps, generalized from the fixed
# TP1/TP2 pair), trailing (the runner portion, once past the last fixed
# step), opposite-CHoCH exits, time exits, emergency exits.
#
# Pure functions over a plain TradeState -- no MT5/execution_service
# wiring here (that belongs to the final bot-assembly integration step,
# not this module). Nothing frozen touched or re-litigated.
#
# DOCUMENTED IMPLEMENTATION DECISIONS:
#
#   X1. R-multiple exit steps are read from the already-locked config_v2
#       table (BREAKEVEN_R=2.0, TP1_R=3.0/TP1_CLOSE_PCT=0.25,
#       TP2_R=4.0/TP2_CLOSE_PCT=0.25) as an ordered list of ExitStep, per
#       the plan's "generalize the tiered-exit state from fixed TP1/TP2
#       booleans to an ordered list of (R-multiple, action) steps."
#   X2. Trailing applies ONLY to the runner portion (whatever remains
#       after all fixed partial-close steps have fired) -- an ATR-multiple
#       trail (config_v2.RUNNER_TRAIL_ATR_MULTIPLIER), never tightening
#       past the most recent breakeven/partial-close SL (one-way, same
#       discipline as every other one-way flag in this codebase).
#   X3. Opposite-CHoCH exit reuses the SAME local (M1) StructureEngine
#       concept already established in entry_confirmation.py (E4) -- if a
#       CHoCH invalidates the trend that was running in OUR trade's
#       direction, that is a genuine reason to exit early, not a new
#       structural concept.
#   X4. Time exit and emergency exit thresholds
#       (MAX_TRADE_DURATION_HOURS, EMERGENCY_MAX_ADVERSE_R) are new,
#       conservative, documented additions to config_v2.py -- the original
#       numbered spec material available to this module does not specify
#       exact figures for these, so they are flagged, not silently
#       invented as unlabeled magic numbers.

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Set

from src.domain.entities import Direction
from src.domain.strategy_v2_entities import TrendState
from src.application.strategy_v2 import config_v2


@dataclass(frozen=True)
class ExitStep:
    r_multiple: float
    action: str                       # "breakeven" | "partial_close"
    close_pct: Optional[float] = None  # required for "partial_close"


EXIT_STEPS: List[ExitStep] = [
    ExitStep(r_multiple=config_v2.BREAKEVEN_R, action="breakeven"),
    ExitStep(r_multiple=config_v2.TP1_R, action="partial_close", close_pct=config_v2.TP1_CLOSE_PCT),
    ExitStep(r_multiple=config_v2.TP2_R, action="partial_close", close_pct=config_v2.TP2_CLOSE_PCT),
]


@dataclass
class TradeState:
    direction: Direction
    entry_price: float
    initial_risk: float               # abs(entry - original_sl), in price units, fixed at open
    current_sl: float
    opened_at: datetime
    remaining_pct: float = 1.0
    triggered_r_multiples: Set[float] = field(default_factory=set)
    is_runner_trailing: bool = False  # True once all fixed EXIT_STEPS have fired


@dataclass
class ManagementAction:
    action: str                       # "breakeven" | "partial_close" | "trail_sl" | "full_close"
    new_sl: Optional[float] = None
    close_pct: Optional[float] = None
    reason: Optional[str] = None      # for full_close: "opposite_choch" | "time_exit" | "emergency"


def current_r_multiple(direction: Direction, entry_price: float, current_price: float, initial_risk: float) -> float:
    if initial_risk <= 0:
        return 0.0
    if direction == Direction.BULLISH:
        return (current_price - entry_price) / initial_risk
    return (entry_price - current_price) / initial_risk


def breakeven_stop(direction: Direction, entry_price: float, buffer_pips: float, pip_size: float) -> float:
    buffer = buffer_pips * pip_size
    return entry_price + buffer if direction == Direction.BULLISH else entry_price - buffer


def evaluate_fixed_steps(state: TradeState, current_price: float, symbol: str) -> List[ManagementAction]:
    """X1 -- evaluates the fixed R-multiple ladder (EXIT_STEPS) against
    the current price, firing any not-yet-triggered step whose threshold
    has been reached. Returns actions in ascending R-multiple order (a
    single call can fire more than one step if price gapped through
    several thresholds at once)."""
    r = current_r_multiple(state.direction, state.entry_price, current_price, state.initial_risk)
    actions: List[ManagementAction] = []
    for step in EXIT_STEPS:
        if step.r_multiple in state.triggered_r_multiples:
            continue
        if r < step.r_multiple:
            continue
        state.triggered_r_multiples.add(step.r_multiple)
        if step.action == "breakeven":
            new_sl = breakeven_stop(state.direction, state.entry_price, config_v2.RUNNER_BE_BUFFER_PIPS, config_v2.pip_size(symbol))
            state.current_sl = new_sl
            actions.append(ManagementAction(action="breakeven", new_sl=new_sl))
        elif step.action == "partial_close":
            state.remaining_pct -= step.close_pct
            actions.append(ManagementAction(action="partial_close", close_pct=step.close_pct))
    if all(s.r_multiple in state.triggered_r_multiples for s in EXIT_STEPS):
        state.is_runner_trailing = True
    return actions


def trail_runner_stop(
    state: TradeState, current_price: float, atr_value: Optional[float],
) -> Optional[ManagementAction]:
    """X2 -- ATR-multiple trail for the runner portion, only once all
    fixed steps have fired. One-way: never loosens the SL back past its
    current level."""
    if not state.is_runner_trailing or atr_value is None or atr_value <= 0:
        return None
    distance = atr_value * config_v2.RUNNER_TRAIL_ATR_MULTIPLIER
    if state.direction == Direction.BULLISH:
        candidate = current_price - distance
        if candidate <= state.current_sl:
            return None
    else:
        candidate = current_price + distance
        if candidate >= state.current_sl:
            return None
    state.current_sl = candidate
    return ManagementAction(action="trail_sl", new_sl=candidate)


def should_exit_on_opposite_choch(
    direction: Direction, choch_fired: bool, invalidated_trend: Optional[TrendState],
) -> bool:
    """X3 -- reuses the local (M1) StructureEngine concept from
    entry_confirmation.py's E4: a CHoCH invalidating the trend that was
    running in OUR trade's own direction is a genuine early-exit signal."""
    if not choch_fired or invalidated_trend is None:
        return False
    return invalidated_trend.value == direction.value


def should_time_exit(opened_at: datetime, now: datetime, max_duration_hours: float = config_v2.MAX_TRADE_DURATION_HOURS) -> bool:
    return (now - opened_at) >= timedelta(hours=max_duration_hours)


def should_emergency_exit(
    direction: Direction, entry_price: float, current_price: float, initial_risk: float,
    max_adverse_r: float = config_v2.EMERGENCY_MAX_ADVERSE_R, external_emergency_flag: bool = False,
) -> bool:
    """X4 -- close-at-market if price has moved this many multiples of
    initial risk against the position (abnormal adverse move, e.g. a news
    spike gapping through normal SL logic), OR an externally-signaled
    emergency (circuit breaker / manual kill-switch)."""
    if external_emergency_flag:
        return True
    r = current_r_multiple(direction, entry_price, current_price, initial_risk)
    return r <= -max_adverse_r


def evaluate_trade(
    state: TradeState, current_price: float, symbol: str, now: datetime, atr_value: Optional[float],
    choch_fired: bool = False, invalidated_trend: Optional[TrendState] = None,
    external_emergency_flag: bool = False,
) -> List[ManagementAction]:
    """Orchestrates every check in priority order: emergency exit and
    opposite-CHoCH exit (full close, short-circuits everything else) take
    priority over the fixed R-multiple ladder and runner trailing, since a
    full close makes partial-close/trailing bookkeeping moot."""
    if should_emergency_exit(state.direction, state.entry_price, current_price, state.initial_risk,
                              external_emergency_flag=external_emergency_flag):
        return [ManagementAction(action="full_close", reason="emergency")]

    if should_exit_on_opposite_choch(state.direction, choch_fired, invalidated_trend):
        return [ManagementAction(action="full_close", reason="opposite_choch")]

    if should_time_exit(state.opened_at, now):
        return [ManagementAction(action="full_close", reason="time_exit")]

    actions = evaluate_fixed_steps(state, current_price, symbol)
    trail = trail_runner_stop(state, current_price, atr_value)
    if trail is not None:
        actions.append(trail)
    return actions
