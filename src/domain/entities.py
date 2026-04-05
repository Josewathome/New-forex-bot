# src/domain/entities.py

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


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


@dataclass
class Candle:
    time:      datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float
    symbol:    str
    timeframe: Timeframe

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
        if self.total_range == 0:
            return 0.0
        wick = self.upper_wick if side == "upper" else self.lower_wick
        return wick / self.total_range


@dataclass
class Zone:
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
    symbol:           str
    high:             float
    low:              float
    lookback_candles: int
    created_at:       datetime
    timeframe:        Timeframe


@dataclass
class NewsEvent:
    currency:   str
    event_name: str
    impact:     str
    event_time: datetime
    actual:     Optional[str]
    forecast:   Optional[str]
    previous:   Optional[str]
    source:     str


@dataclass
class NoTradeWindow:
    start:      datetime
    end:        datetime
    currency:   str
    event_name: str

    def is_blocking(self, now: datetime) -> bool:
        return self.start <= now <= self.end


@dataclass
class TradeSignal:
    symbol:       str
    direction:    Direction
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    zone:         Zone
    signal_type:  SignalType
    volume_ratio: float
    created_at:   datetime
    risk_reward:  float


@dataclass
class Trade:
    id:           str
    symbol:       str
    direction:    Direction
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    lot_size:     float
    status:       TradeStatus
    created_at:   datetime

    # existing optional fields
    closed_at:     Optional[datetime] = None
    close_price:   Optional[float]    = None
    pnl:           Optional[float]    = None
    mt5_ticket:    Optional[int]      = None
    commission:    float              = 0.0
    spread_cost:   float              = 0.0
    news_affected: bool               = False
    slippage_pips: float              = 0.0

    # post-trade analytics (existing)
    mfe_price:          Optional[float] = None
    mfe_pips:           Optional[float] = None
    pips_to_tp_at_mfe:  Optional[float] = None
    duration_minutes:   Optional[float] = None
    reversal_zone_type: Optional[str]   = None
    balance_before:     Optional[float] = None
    balance_after:      Optional[float] = None

    # NEW: multi‑TP tracking and profit management
    tp1_hit: bool = False
    tp2_hit: bool = False
    initial_risk: float = 0.0          # distance from entry to original SL in price units
    realized_pnl: float = 0.0          # cumulative PnL from partial closes
    remaining_volume: float = 0.0      # remaining lot size after partial closes
    exit_reason: str = ""              # "TP1", "TP2", "SL", "MONITOR_CLOSE"
    mfe: float = 0.0                   # maximum favorable excursion in price units
    mae: float = 0.0                   # maximum adverse excursion in price units