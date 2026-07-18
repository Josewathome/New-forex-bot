"""Regression test for src/application/strategy_v2/risk_management.py
against the test matrix: risk % selection, position sizing (cross-checked
against RiskParameters.lot_size's own documented worked example), max-lot
clamping, correlated exposure, spread filter delegation, and slippage
handling.

HOW TO RUN
    python3 tools/validate_risk_management.py   (from repo root; no env vars needed)
"""
import sys
sys.path.insert(0, ".")
from src.application.strategy_v2 import config_v2
from src.domain.value_objects import PipCalculator, BrokerCost
from src.application.strategy_v2.risk_management import (
    select_risk_pct, compute_position_size, correlated_open_count, correlated_exposure_exceeded,
    spread_filter_blocks, is_slippage_acceptable, realized_risk_amount,
)

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

print("="*78); print("SECTION 1 — RISK % SELECTION (R2)"); print("="*78)

check("1a default: RISK_DEFAULT_PCT", select_risk_pct() == config_v2.RISK_DEFAULT_PCT)
check("1b use_scaled=True: RISK_SCALED_PCT", select_risk_pct(use_scaled=True) == config_v2.RISK_SCALED_PCT)
check("1c use_scaled=False explicit: RISK_DEFAULT_PCT", select_risk_pct(use_scaled=False) == config_v2.RISK_DEFAULT_PCT)

print(); print("="*78); print("SECTION 2 — POSITION SIZING (R1, cross-checked against RiskParameters' own worked example)"); print("="*78)

# RiskParameters.lot_size docstring worked example: balance=400, risk=2%,
# sl_pips=30, tick_value=1.0 (GBPUSD), commission=3.50/lot -> lots ≈ 0.0264.
# Reconstruct via compute_position_size using an entry/stop 30 pips apart
# on a 5-digit pair (GBPUSD, digits=5) with commission passed directly.
entry, stop = 1.30000, 1.29700  # 30 pips apart at 5 digits
lots = compute_position_size(
    account_balance=400.0, risk_pct=2.0, entry_price=entry, stop_loss=stop, symbol="GBPUSD",
    tick_value=1.0, commission_per_lot=3.50, digits=5, max_lot_size=10.0,
)
check("2a position size matches RiskParameters' own documented worked example (~0.0264 lots)",
      abs(lots - 0.0264) < 0.001, f"got {lots}")

lots_bear = compute_position_size(
    account_balance=400.0, risk_pct=2.0, entry_price=stop, stop_loss=entry, symbol="GBPUSD",
    tick_value=1.0, commission_per_lot=3.50, digits=5, max_lot_size=10.0,
)
check("2b bearish direction (stop above entry) gives the identical size (uses abs distance)",
      abs(lots_bear - lots) < 1e-9, f"{lots_bear} vs {lots}")

zero_distance = compute_position_size(
    account_balance=400.0, risk_pct=2.0, entry_price=1.3000, stop_loss=1.3000, symbol="GBPUSD",
    tick_value=1.0, commission_per_lot=3.50, digits=5, max_lot_size=10.0,
)
check("2c zero SL distance: returns 0.0 rather than dividing by zero", zero_distance == 0.0)

print(); print("="*78); print("SECTION 3 — MAX LOT CLAMPING (R1)"); print("="*78)

huge_lots = compute_position_size(
    account_balance=1_000_000.0, risk_pct=5.0, entry_price=1.3000, stop_loss=1.2990, symbol="GBPUSD",
    tick_value=1.0, commission_per_lot=3.50, digits=5, max_lot_size=10.0,
)
check("3a position size clamped to max_lot_size even when the formula would exceed it",
      huge_lots == 10.0, f"got {huge_lots}")

print(); print("="*78); print("SECTION 4 — CORRELATED EXPOSURE (R3)"); print("="*78)

CURR = {"EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"], "XAUUSD": ["USD"], "USDJPY": ["USD", "JPY"], "AUDUSD": ["AUD", "USD"]}

check("4a no open trades: 0 correlated", correlated_open_count("EURUSD", [], CURR) == 0)
check("4b one open trade sharing USD: count=1", correlated_open_count("EURUSD", ["GBPUSD"], CURR) == 1)
check("4c two open trades sharing USD: count=2", correlated_open_count("EURUSD", ["GBPUSD", "XAUUSD"], CURR) == 2)
check("4d open trade sharing NO currency (e.g. a JPY-only pair vs a EUR-only pair): count=0 for that pair",
      correlated_open_count("EURJPY_HYPOTHETICAL", ["USDJPY"], {**CURR, "EURJPY_HYPOTHETICAL": ["EUR"]}) == 0)

check("4e exposure NOT exceeded with count below MAX_TRADES_PER_CURRENCY",
      not correlated_exposure_exceeded("EURUSD", ["GBPUSD"], CURR))
check("4f exposure exceeded when count reaches MAX_TRADES_PER_CURRENCY",
      correlated_exposure_exceeded("EURUSD", ["GBPUSD", "XAUUSD"], CURR) == (config_v2.MAX_TRADES_PER_CURRENCY <= 2))

print(); print("="*78); print("SECTION 5 — SPREAD FILTER DELEGATION (R4)"); print("="*78)

pip_calc = PipCalculator(digits=5)
cost_ok = BrokerCost(spread_pips=1.0, commission_usd=0.35, tick_value=1.0)
cost_bad = BrokerCost(spread_pips=50.0, commission_usd=0.35, tick_value=1.0)

def fake_cost_check(symbol, atr_price, broker_cost, pip_calculator):
    # mirrors NewsManager.broker_cost_exceeds_atr's own logic exactly, as a
    # test double (no import of the concrete NewsManager/its repo here).
    if atr_price <= 0:
        return False
    atr_pips = pip_calculator.price_to_pips(atr_price)
    threshold = atr_pips * config_v2.MIN_RR / config_v2.MIN_RR * 0.25  # 0.25 threshold, same as ATR_COST_THRESHOLD
    return broker_cost.total_cost_pips > threshold

check("5a tight spread relative to ATR: not blocked",
      not spread_filter_blocks("EURUSD", 0.0010, cost_ok, pip_calc, fake_cost_check))
check("5b wide spread relative to ATR: blocked",
      spread_filter_blocks("EURUSD", 0.0010, cost_bad, pip_calc, fake_cost_check))

print(); print("="*78); print("SECTION 6 — SLIPPAGE HANDLING (R5)"); print("="*78)

check("6a fill within tolerance: acceptable", is_slippage_acceptable(1.10000, 1.10003, "EURUSD", 1.0, 5))
check("6b fill beyond tolerance: not acceptable", not is_slippage_acceptable(1.10000, 1.10020, "EURUSD", 1.0, 5))
check("6c favorable slippage (better than intended): still acceptable (no floor on negative slippage)",
      is_slippage_acceptable(1.10000, 1.09998, "EURUSD", 1.0, 5))

risk_no_slip = realized_risk_amount(fill_price=1.30000, stop_loss=1.29700, lot_size=0.0264, tick_value=1.0, digits=5)
risk_with_slip = realized_risk_amount(fill_price=1.30010, stop_loss=1.29700, lot_size=0.0264, tick_value=1.0, digits=5)
check("6d worse fill (larger SL distance) increases realized risk", risk_with_slip > risk_no_slip,
      f"{risk_with_slip} vs {risk_no_slip}")

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
