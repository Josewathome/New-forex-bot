"""
Domain Entities — Pure Business Objects.
No imports from infrastructure or application layers.
These are the canonical data shapes the entire system reasons about.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────
#  Enumerations
# ─────────────────────────────────────────────

class Direction(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class ZoneType(Enum):
    ORDER_BLOCK    = "order_block"
    FAIR_VALUE_GAP = "fvg"
    LIQUIDITY_POOL = "liquidity_pool"


class TradeStatus(Enum):
    PENDING   = "pending"
    OPEN      = "open"
    CLOSED    = "closed"
    CANCELLED = "cancelled"


class SignalType(Enum):
    REJECTION_WICK    = "rejection_wick"
    VOLUME_CONFIRM    = "volume_confirm"
    BULLISH_ENGULFING = "bullish_engulfing"


class Timeframe(Enum):
    M1  = "M1"
    M5  = "M5"
    M15 = "M15"
    H1  = "H1"
    H4  = "H4"
    D1  = "D1"


# ─────────────────────────────────────────────
#  Value-like primitives
# ─────────────────────────────────────────────

@dataclass
class Candle:
    """OHLCV bar. All prices are raw broker prices (not pips)."""
    time:      datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float
    symbol:    str
    timeframe: Timeframe

    # ── derived geometry ──────────────────────

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> float:
        return self.high - self.body_high

    @property
    def lower_wick(self) -> float:
        return self.body_low - self.low

    def wick_ratio(self, side: str) -> float:
        """Fraction of total range occupied by the named wick. side='upper'|'lower'"""
        if self.total_range == 0:
            return 0.0
        wick = self.upper_wick if side == "upper" else self.lower_wick
        return wick / self.total_range


# ─────────────────────────────────────────────
#  Zone / POI aggregates
# ─────────────────────────────────────────────

@dataclass
class Zone:
    """
    A mapped institutional Point of Interest (POI).
    Represents an Order Block or Fair Value Gap on H1/H4.
    """
    id:                  str
    symbol:              str
    zone_type:           ZoneType
    direction:           Direction
    high:                float
    low:                 float
    created_at:          datetime
    timeframe:           Timeframe
    is_active:           bool = True
    touch_count:         int  = 0
    source_candle_index: Optional[int] = None

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def size(self) -> float:
        return self.high - self.low

    def contains_price(self, price: float) -> bool:
        return self.low <= price <= self.high

    def mark_tapped(self) -> None:
        self.touch_count += 1

    def invalidate(self) -> None:
        self.is_active = False


@dataclass
class LiquidityPool:
    """
    Recent swing high/low acting as a magnet for price.
    Calculated from the last N H1 candles.
    """
    symbol:           str
    high:             float          # highest High in lookback window
    low:              float          # lowest Low in lookback window
    lookback_candles: int
    created_at:       datetime
    timeframe:        Timeframe


# ─────────────────────────────────────────────
#  News / Risk Guard
# ─────────────────────────────────────────────

@dataclass
class NewsEvent:
    currency:   str
    event_name: str
    impact:     str                 # "low" | "medium" | "high"
    event_time: datetime            # always UTC
    actual:     Optional[str]
    forecast:   Optional[str]
    previous:   Optional[str]
    source:     str


@dataclass
class NoTradeWindow:
    """
    Derived from a high-impact NewsEvent.
    Represents the ±N min blackout period around an event.
    """
    start:      datetime
    end:        datetime
    currency:   str
    event_name: str

    def is_blocking(self, now: datetime) -> bool:
        return self.start <= now <= self.end


# ─────────────────────────────────────────────
#  Signal & Execution
# ─────────────────────────────────────────────

@dataclass
class TradeSignal:
    """
    A confirmed entry signal produced by the execution engine.
    Carries all the math needed to size and place the trade.
    """
    symbol:       str
    direction:    Direction
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    zone:         Zone
    signal_type:  SignalType
    volume_ratio: float           # rejection candle volume / avg(10) volume
    created_at:   datetime
    risk_reward:  float


@dataclass
class Trade:
    """
    A live or historical trade record.
    Written when an order is placed; updated on close.

    NEW fields (v4 — post-trade analytics):
        mfe_price          — raw price of Maximum Favorable Excursion
        mfe_pips           — MFE expressed in pips from entry
        pips_to_tp_at_mfe  — pips remaining to TP when MFE was reached
                             (0 for winners; > 0 tells you "how close" a loser got)
        duration_minutes   — how long the trade was open (entry → close/SL/TP)
        reversal_zone_type — type of opposing zone (if any) at the MFE reversal point:
                             "order_block" | "fvg" | "no_zone" | None (winners have None)
        balance_before     — account equity before this trade was sized
        balance_after      — account equity after PnL was applied
    """
    id:           str
    symbol:       str
    direction:    Direction
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    lot_size:     float
    status:       TradeStatus
    created_at:   datetime

    # ── existing optional fields ──────────────────────────────────
    closed_at:     Optional[datetime] = None
    close_price:   Optional[float]    = None
    pnl:           Optional[float]    = None
    mt5_ticket:    Optional[int]      = None
    commission:    float              = 0.0
    spread_cost:   float              = 0.0
    news_affected: bool               = False
    slippage_pips: float              = 0.0

    # ── NEW: post-trade analytics (backtest only) ─────────────────
    mfe_price:          Optional[float] = None
    mfe_pips:           Optional[float] = None
    pips_to_tp_at_mfe:  Optional[float] = None
    duration_minutes:   Optional[float] = None
    reversal_zone_type: Optional[str]   = None
    balance_before:     Optional[float] = None
    balance_after:      Optional[float] = None