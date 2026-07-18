# src/application/strategy_v2/risk_management.py
#
# Risk Management — position sizing, fixed risk %, correlated-exposure
# limiting, spread filtering, and slippage handling. Pure composition over
# already-built, reusable value objects (src/domain/value_objects.py's
# RiskParameters.lot_size / PipCalculator / BrokerCost) and the existing
# framework NewsManager.broker_cost_exceeds_atr -- no position-sizing
# formula or spread/cost math is reimplemented here.
#
# DOCUMENTED IMPLEMENTATION DECISIONS:
#
#   R1. Position sizing delegates entirely to RiskParameters.lot_size()
#       (already-built, already-proven formula), then clamps to the
#       framework's src/config.py MAX_LOT_SIZE -- a hard technical/broker
#       ceiling, not a strategy tuning parameter, so it is read from
#       src/config.py rather than duplicated into config_v2.py.
#   R2. Fixed risk % selection: config_v2.RISK_DEFAULT_PCT (1.0%) unless
#       the caller explicitly opts into RISK_SCALED_PCT (2.0%) -- the
#       original spec does not state a trigger condition for scaled risk
#       in the material available to this module, so the conservative
#       default is used unless a caller decides otherwise; this is a
#       flagged decision, not a silent invention of new criteria.
#   R3. Max correlated exposure: counts currently-open trades sharing ANY
#       currency leg (src/config.py's SYMBOL_CURRENCIES, already built)
#       with the candidate symbol; blocks if that count would reach
#       config_v2.MAX_TRADES_PER_CURRENCY. A conservative, documented
#       addition (R# constant in config_v2.py) since correlated-exposure
#       limiting was named as a requirement without a specific numeric
#       threshold elsewhere.
#   R4. Spread filter reuses NewsManager.broker_cost_exceeds_atr exactly
#       (same generic-callable pattern as trade_decision.py's news
#       validation) -- no new cost-vs-ATR formula.
#   R5. Slippage handling: is_slippage_acceptable() gates a fill against a
#       max-pip tolerance (config_v2 has no existing slippage constant, so
#       this takes an explicit `max_slippage_pips` parameter rather than
#       inventing a new locked default); realized_risk_amount()
#       recomputes the ACTUAL monetary risk from the real fill price
#       (which may differ from the intended entry), since slippage changes
#       the realized SL distance even though position size was already
#       fixed at order-placement time.

from __future__ import annotations
from typing import Callable, Dict, List, Optional

from src.application.strategy_v2 import config_v2
from src.domain.value_objects import PipCalculator, RiskParameters


def select_risk_pct(use_scaled: bool = False) -> float:
    """R2 -- RISK_DEFAULT_PCT unless the caller opts into scaled risk."""
    return config_v2.RISK_SCALED_PCT if use_scaled else config_v2.RISK_DEFAULT_PCT


def compute_position_size(
    account_balance: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    symbol: str,
    tick_value: float,
    commission_per_lot: float,
    digits: int,
    max_lot_size: float,
) -> float:
    """R1 -- delegates to RiskParameters.lot_size(), clamped to
    max_lot_size (the framework's hard broker-level ceiling)."""
    pip_calc = PipCalculator(digits=digits)
    sl_pips = pip_calc.price_to_pips(entry_price - stop_loss)
    if sl_pips <= 0:
        return 0.0
    params = RiskParameters(
        account_balance=account_balance, risk_percent=risk_pct,
        commission_per_lot=commission_per_lot, min_rr_ratio=config_v2.MIN_RR,
    )
    lots = params.lot_size(sl_pips=sl_pips, tick_value=tick_value, pip_calculator=pip_calc)
    return min(lots, max_lot_size)


def correlated_open_count(symbol: str, open_symbols: List[str], symbol_currencies: Dict[str, List[str]]) -> int:
    """R3 -- how many currently-open trades share ANY currency leg with
    `symbol` (the candidate itself excluded -- callers pass only ALREADY-
    open trades' symbols, not the candidate)."""
    target_currencies = set(symbol_currencies.get(symbol, []))
    if not target_currencies:
        return 0
    count = 0
    for open_sym in open_symbols:
        if target_currencies & set(symbol_currencies.get(open_sym, [])):
            count += 1
    return count


def correlated_exposure_exceeded(symbol: str, open_symbols: List[str], symbol_currencies: Dict[str, List[str]]) -> bool:
    """R3 -- True if opening a new trade on `symbol` would push the
    shared-currency count to or beyond config_v2.MAX_TRADES_PER_CURRENCY."""
    return correlated_open_count(symbol, open_symbols, symbol_currencies) >= config_v2.MAX_TRADES_PER_CURRENCY


def spread_filter_blocks(
    symbol: str, atr_price: float, broker_cost, pip_calculator: PipCalculator,
    cost_check: Callable[[str, float, object, PipCalculator], bool],
) -> bool:
    """R4 -- delegates to the existing NewsManager.broker_cost_exceeds_atr
    (passed in as `cost_check`, same generic-callable pattern as
    trade_decision.py's news validation) -- no new formula."""
    return cost_check(symbol, atr_price, broker_cost, pip_calculator)


def is_slippage_acceptable(entry_price: float, fill_price: float, symbol: str, max_slippage_pips: float, digits: int) -> bool:
    """R5 -- gates a fill against a max-pip tolerance."""
    pip_calc = PipCalculator(digits=digits)
    slippage_pips = pip_calc.price_to_pips(fill_price - entry_price)
    return slippage_pips <= max_slippage_pips


def realized_risk_amount(
    fill_price: float, stop_loss: float, lot_size: float, tick_value: float, digits: int,
) -> float:
    """R5 -- the ACTUAL monetary risk from the real fill price (which may
    differ from the intended entry due to slippage) -- position size was
    already fixed at order-placement time, so a worse fill changes the
    realized risk even though lot_size itself does not change."""
    pip_calc = PipCalculator(digits=digits)
    sl_pips = pip_calc.price_to_pips(fill_price - stop_loss)
    pip_value = tick_value * 10
    return abs(sl_pips) * pip_value * lot_size
