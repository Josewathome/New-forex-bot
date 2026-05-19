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
    commission_usd  — round-turn commission in ACCOUNT CURRENCY per lot
                      (for KES accounts this is already in KES — see config_loader)
    tick_value      — ACCOUNT CURRENCY value of 1 tick for 1 standard lot
    """
    spread_pips:    float
    commission_usd: float   # naming kept for legacy compat; value is in account currency
    tick_value:     float

    @property
    def commission_pips(self) -> float:
        """Convert the fixed commission to pip-equivalent."""
        if self.tick_value <= 0:
            return 0.0
        return self.commission_usd / self.tick_value

    @property
    def total_cost_pips(self) -> float:
        """Total cost of entry + exit in pips."""
        return self.spread_pips + self.commission_pips


# ─────────────────────────────────────────────
#  Risk
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class RiskParameters:
    """
    Account-level risk configuration.

    All monetary values are in ACCOUNT CURRENCY.
    For Demojose's HFMarketsKE account that currency is KES.

    Lot-size formula:
        risk_amount   = account_balance × (risk_percent / 100)
        lots          = risk_amount / (sl_pips × tick_value_per_lot)

    tick_value_per_lot is provided by MT5 in account currency — so the
    formula works identically whether the account is USD, KES, or any other
    currency supported by HFM.
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
        tick_value: float,           # per lot, in account currency
        pip_calculator: "PipCalculator",
    ) -> float:
        """
        Calculate position size in standard lots.

        Example (KES account):
            balance        = 500,000 KES
            risk_percent   = 1.0
            risk_amount    = 5,000 KES
            sl_pips        = 20
            tick_value     = 1,300 KES/pip/lot  (MT5 value for EURUSD on KES acct)
            lots           = 5,000 / (20 × 1,300) = 0.19 lots ≈ 0.19
        """
        if sl_pips <= 0 or tick_value <= 0:
            return 0.0
        # Include round-trip commission in the effective SL so position size
        # reflects the true cost of the trade, not just the pip-stop distance.
        commission_pips = self.commission_per_lot / tick_value if tick_value > 0 else 0.0
        effective_sl_pips = sl_pips + commission_pips
        return self.risk_amount / (effective_sl_pips * tick_value)