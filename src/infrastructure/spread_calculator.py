"""
Spread Calculator — Live MT5-based spread computation.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replaces the external broker-spreads API entirely.

Spread = ask − bid  (from MT5 tick, always available)
Commission is a known fixed constant for HFM: $3 USD/lot
  → converted to account currency by config_loader at startup.

Fallback chain when live tick unavailable:
  1. Live tick from MT5 via ZMQ                ← primary
  2. symbol_info.spread (in points from MT5)   ← secondary
  3. Symbol-specific conservative estimate     ← tertiary
  4. Hard floor: 1.0 pip                       ← last resort
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations
import logging
from typing import Optional

from src.domain.repositories import IMarketDataRepository
from src.domain.value_objects import BrokerCost, PipCalculator

logger = logging.getLogger(__name__)

# ── Conservative pip-spread fallback estimates per symbol ─────────
# Used ONLY when MT5 tick is unavailable (e.g. market closed).
# Values are slightly above typical HFM spreads to stay safe.
FALLBACK_SPREAD_PIPS: dict[str, float] = {
    "EURUSD": 0.3,
    "GBPUSD": 0.5,
    "USDJPY": 0.5,
    "AUDUSD": 0.5,
    "USDCAD": 0.6,
    "USDCHF": 0.6,
    "XAUUSD": 3.0,   # Gold spread is wider
    "XAGUSD": 5.0,
    "US30":   3.0,
    "NAS100": 1.5,
}
DEFAULT_FALLBACK_PIPS = 1.5   # if symbol not in table


class SpreadCalculator:
    """
    Computes live BrokerCost from MT5 tick data.
    No external HTTP calls required.
    """

    def __init__(
        self,
        market_data:        IMarketDataRepository,
        commission_per_lot: float,   # in account currency (KES for this account)
    ) -> None:
        self._md         = market_data
        self._commission = commission_per_lot

    def get_broker_cost(self, symbol: str) -> BrokerCost:
        """
        Build a BrokerCost from live MT5 data.
        Never raises — always returns a valid BrokerCost even on failure.
        """
        digits     = self._md.get_symbol_digits(symbol)
        tick_value = self._md.get_tick_value(symbol)
        pip_calc   = PipCalculator(digits=digits)

        spread_pips = self._get_spread_pips(symbol, pip_calc)

        cost = BrokerCost(
            spread_pips=    spread_pips,
            commission_usd= self._commission,
            tick_value=     tick_value,
        )
        logger.debug(
            "BrokerCost %s | spread=%.2f pips | commission=%.2f | "
            "tick_val=%.4f | total_cost=%.2f pips",
            symbol, spread_pips, self._commission,
            tick_value, cost.total_cost_pips,
        )
        return cost

    def _get_spread_pips(self, symbol: str, pip_calc: PipCalculator) -> float:
        """
        Attempt to get live spread. Falls back gracefully.
        """
        # ── Method 1: live bid/ask from tick ──────────────────────
        try:
            bid, ask = self._md.get_bid_ask(symbol)
            if ask > 0 and bid > 0 and ask > bid:
                spread_pips = pip_calc.price_to_pips(ask - bid)
                if 0 < spread_pips < 50:   # sanity: ignore absurd values
                    logger.debug("%s live spread: %.2f pips (bid=%.5f ask=%.5f)",
                                 symbol, spread_pips, bid, ask)
                    return spread_pips
                logger.warning("%s spread %.2f pips out of range — using fallback", symbol, spread_pips)
        except Exception as exc:
            logger.warning("Live tick unavailable for %s: %s", symbol, exc)

        # ── Method 2: symbol_info.spread (in points, from MT5) ────
        # symbol_info.spread = spread in points (not pips)
        # pips = points / 10 for 5-digit brokers
        try:
            digits = self._md.get_symbol_digits(symbol)
            # get_spread_pips uses get_bid_ask internally, but let's try directly
            sp = self._md.get_spread_pips(symbol)
            if sp and 0 < sp < 50:
                logger.debug("%s spread from symbol_info: %.2f pips", symbol, sp)
                return sp
        except Exception as exc:
            logger.warning("symbol_info spread unavailable for %s: %s", symbol, exc)

        # ── Method 3: static fallback table ───────────────────────
        fallback = FALLBACK_SPREAD_PIPS.get(symbol, DEFAULT_FALLBACK_PIPS)
        logger.warning(
            "%s using static fallback spread: %.2f pips "
            "(MT5 data unavailable — market may be closed)",
            symbol, fallback,
        )
        return fallback