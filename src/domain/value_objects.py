"""
Domain Value Objects — Immutable, identity-free typed wrappers.
These enforce units and prevent primitive obsession throughout the system.

KES note:
  MT5 returns trade_tick_value in the ACCOUNT currency.
  For a KES account that value is already in KES.
  RiskParameters.lot_size() therefore works in any currency natively —
  as long as commission_per_lot is also expressed in the same currency.
  config_loader.py handles the USD→KES conversion at startup.
"""
from __future__ import annotations
from dataclasses import dataclass, field


# ─────────────────────────────────────────────
#  Price / Pip arithmetic
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class PipCalculator:
    """
    Encapsulates the 5-digit broker pip formula.

    For 5-digit brokers (EURUSD = 1.10000):
        point  = 0.00001
        1 pip  = 10 points = 0.0001

    Formula:  pips = price_diff / point / 10

    digits reference:
        EURUSD / GBPUSD / AUDUSD → 5
        USDJPY                   → 3
        XAUUSD                   → 2
    """
    digits: int

    @property
    def point(self) -> float:
        return 10 ** (-self.digits)

    def price_to_pips(self, price_diff: float) -> float:
        """Convert a raw price delta to pips."""
        return abs(price_diff) / self.point / 10

    def pips_to_price(self, pips: float) -> float:
        """Convert pips back to a price delta."""
        return pips * self.point * 10


@dataclass(frozen=True)
class BrokerCost:
    """
    All-in cost of entering a trade for a given symbol.

    spread_pips     — live spread in pips
    commission_usd  — one-way (per-side) commission in ACCOUNT CURRENCY per lot
                      (for KES accounts this is already in KES — see config_loader)
    tick_value      — ACCOUNT CURRENCY value of 1 tick (smallest price move)
                      for 1 standard lot, as returned by MT5 trade_tick_value.
                      For 5-digit pairs: 1 tick = 0.00001, pip_value = tick_value × 10.
                      For 3-digit pairs (JPY): 1 tick = 0.001, pip_value = tick_value × 10.
                      For XAUUSD (2 digits): 1 tick = 0.01, pip_value = tick_value × 10.
    """
    spread_pips:    float
    commission_usd: float   # naming kept for legacy compat; value is in account currency
    tick_value:     float

    @property
    def pip_value(self) -> float:
        """Account currency value of 1 pip for 1 standard lot.
        MT5 trade_tick_value is per tick; 1 pip = 10 ticks for all standard pairs."""
        return self.tick_value * 10

    @property
    def commission_pips(self) -> float:
        """Convert the one-way commission to pip-equivalent using pip_value."""
        if self.pip_value <= 0:
            return 0.0
        return self.commission_usd / self.pip_value

    @property
    def total_cost_pips(self) -> float:
        """Total entry cost in pips (spread + one-way commission)."""
        return self.spread_pips + self.commission_pips


# ─────────────────────────────────────────────
#  Risk
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class RiskParameters:
    """
    Account-level risk configuration.

    All monetary values are in ACCOUNT CURRENCY (USD for IC Markets).

    Lot-size formula:
        risk_amount   = account_balance × (risk_percent / 100)
        pip_value     = MT5_tick_value × 10   (1 pip = 10 ticks)
        lots          = risk_amount / (effective_sl_pips × pip_value)

    MT5 trade_tick_value is in account currency, so the formula works
    identically for USD, KES, or any other account currency.
    """
    account_balance:    float
    risk_percent:       float        # e.g. 1.0 means 1 %
    commission_per_lot: float        # in account currency (KES for this account)
    min_rr_ratio:       float        # minimum Risk:Reward to accept (e.g. 3.0)
    account_currency:   str  = "USD" # informational; drives log messages

    @property
    def risk_amount(self) -> float:
        """Monetary amount risked per trade, in account currency."""
        return self.account_balance * (self.risk_percent / 100.0)

    def lot_size(
        self,
        sl_pips:    float,
        tick_value: float,           # MT5 trade_tick_value per lot in account currency
        pip_calculator: "PipCalculator",
    ) -> float:
        """
        Calculate position size in standard lots.

        tick_value is MT5's trade_tick_value (value of 1 tick = smallest price move).
        pip_value  = tick_value × 10  (1 pip = 10 ticks for all standard pairs).

        Example (USD account, IC Markets):
            balance        = 400 USD
            risk_percent   = 2.0
            risk_amount    = 8 USD
            sl_pips        = 30
            tick_value     = 1.0 USD/tick/lot  (MT5 value for GBPUSD)
            pip_value      = 10.0 USD/pip/lot
            commission     = 3.50 USD/lot (one-way)
            commission_pips= 3.50 / 10.0 = 0.35 pips
            effective_sl   = 30 + 0.35 = 30.35 pips
            lots           = 8 / (30.35 × 10.0) = 0.0264 → 0.02 lots
        """
        if sl_pips <= 0 or tick_value <= 0:
            return 0.0
        pip_value = tick_value * 10
        commission_pips = self.commission_per_lot / pip_value if pip_value > 0 else 0.0
        effective_sl_pips = sl_pips + commission_pips
        return self.risk_amount / (effective_sl_pips * pip_value)