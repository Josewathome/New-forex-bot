# src/domain/strategy_v2_entities.py
#
# Domain types for the V4 SMC strategy ("strategy_v2"). Kept separate from
# entities.py: Candle/Direction/Timeframe/Trade there are generic and shared
# with legacy code; the shapes below (swings, FVG/OB zones, liquidity levels,
# external-swing-pair) are specific to this strategy's rules and would need
# reshaping for a materially different future strategy.

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from src.domain.entities import Direction, Timeframe


class TrendState(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SwingType(Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class SwingPoint:
    symbol: str
    timeframe: Timeframe
    kind: SwingType
    price: float
    candle_time: datetime      # timestamp of the candidate candle itself
    confirmed_at_index: int    # bar index at which confirmation completed (candidate_index + 3)


@dataclass
class ExternalSwingPair:
    """The single active {low, high} pair per symbol (V4 spec Contradiction 2).
    trend indicates which member was the BOS-breaking level: BULLISH means
    `high` was broken and `low` is the swing that generated the breakout;
    BEARISH mirrors."""
    symbol: str
    trend: TrendState               # BULLISH or BEARISH only — never NEUTRAL while a pair is active
    low: float
    high: float
    low_swing: SwingPoint
    high_swing: SwingPoint
    bos_time: datetime              # H1 candle close time that produced the active BOS


@dataclass
class FVGZone:
    symbol: str
    timeframe: Timeframe
    direction: Direction
    gap_high: float
    gap_low: float
    created_at: datetime
    mitigated: bool = False
    invalidated: bool = False

    @property
    def midpoint(self) -> float:
        return (self.gap_high + self.gap_low) / 2.0


@dataclass
class OrderBlockZone:
    symbol: str
    timeframe: Timeframe
    direction: Direction
    high: float
    low: float
    created_at: datetime
    mitigated: bool = False
    invalidated: bool = False

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


class LiquidityLevelType(Enum):
    ASIA_HIGH     = "asia_high"
    ASIA_LOW      = "asia_low"
    EQUAL_HIGH    = "equal_high"
    EQUAL_LOW     = "equal_low"
    PREV_DAY_HIGH = "prev_day_high"
    PREV_DAY_LOW  = "prev_day_low"


@dataclass
class LiquidityLevel:
    symbol: str
    kind: LiquidityLevelType
    price: float
    primary_timeframe: Timeframe                       # H1 per the dedup rule
    secondary_timeframe: Optional[Timeframe] = None    # M15, if also independently detected there
    created_at: Optional[datetime] = None
    swept: bool = False
    swept_at: Optional[datetime] = None
    sweep_penetration: float = 0.0                      # price distance beyond the level at sweep time
    reaction: Optional[str] = None                      # "confirmed_reversal" | "breakout", set at first sweep only
