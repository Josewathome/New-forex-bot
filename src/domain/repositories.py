"""
Domain Repository Interfaces.
Abstract base classes that define the contracts the application layer
depends on.  Infrastructure provides the concrete implementations.

No MT5, no HTTP, no file I/O here — only domain types.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from .entities import Candle, NewsEvent, Trade, Zone, LiquidityPool, Timeframe


# ─────────────────────────────────────────────
#  Market Data
# ─────────────────────────────────────────────

class IMarketDataRepository(ABC):

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: Timeframe, count: int) -> List[Candle]:
        """Return the most recent `count` closed candles for symbol/timeframe."""
        ...

    @abstractmethod
    def get_current_price(self, symbol: str) -> float:
        """Return the latest ask price."""
        ...

    @abstractmethod
    def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        """Return (bid, ask)."""
        ...

    @abstractmethod
    def get_spread_pips(self, symbol: str) -> float:
        """Return live spread in pips."""
        ...

    @abstractmethod
    def get_tick_value(self, symbol: str) -> float:
        """Return USD value of one tick for one standard lot."""
        ...

    @abstractmethod
    def get_symbol_digits(self, symbol: str) -> int:
        """Return the number of decimal places (5 for EURUSD, 3 for USDJPY)."""
        ...

    @abstractmethod
    def get_server_time_utc(self) -> datetime:
        """Return broker server time, normalised to UTC."""
        ...


# ─────────────────────────────────────────────
#  Trade Execution
# ─────────────────────────────────────────────

class ITradeRepository(ABC):

    @abstractmethod
    def place_trade(self, trade: Trade) -> Optional[int]:
        """Send order to broker.  Returns MT5 ticket on success, None on failure."""
        ...

    @abstractmethod
    def close_trade(self, ticket: int) -> bool:
        """Market-close the position identified by ticket."""
        ...

    @abstractmethod
    def modify_sl(self, ticket: int, new_sl: float) -> bool:
        """
        Move the stop-loss of an open position to new_sl without changing TP.

        Used by the TradeMonitor to trail the SL to a swing structure level
        when the trade is sufficiently in profit.  The TP is preserved
        exactly as set at entry.

        Returns True on success, False if MT5 rejected the request.
        """
        ...

    @abstractmethod
    def get_open_positions(self) -> List[Trade]:
        """Return all currently open positions as Trade objects."""
        ...

    @abstractmethod
    def get_account_balance(self) -> float:
        """Return current account balance in account currency."""
        ...


# ─────────────────────────────────────────────
#  News
# ─────────────────────────────────────────────

class INewsRepository(ABC):

    @abstractmethod
    def get_today_events(self, currencies: List[str], date_str: str) -> List[NewsEvent]:
        """Fetch or return cached high-impact events for today."""
        ...

    @abstractmethod
    def get_range_events(
        self,
        currencies: List[str],
        start_date: str,
        end_date:   str,
    ) -> List[NewsEvent]:
        """Fetch events over a date range (used by backtester)."""
        ...


# ─────────────────────────────────────────────
#  Zone Storage
# ─────────────────────────────────────────────

class IZoneRepository(ABC):

    @abstractmethod
    def save_zone(self, zone: Zone) -> None: ...

    @abstractmethod
    def get_active_zones(self, symbol: str) -> List[Zone]: ...

    @abstractmethod
    def get_all_zones(self) -> List[Zone]: ...

    @abstractmethod
    def invalidate_zone(self, zone_id: str) -> None: ...

    @abstractmethod
    def save_liquidity_pool(self, pool: LiquidityPool) -> None: ...

    @abstractmethod
    def get_liquidity_pool(self, symbol: str) -> Optional[LiquidityPool]: ...