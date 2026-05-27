"""
analysis.py — The "Brain": All Price Action Mathematics.

UPGRADED (v3):
  • find_h4_bias()         — macro direction filter from H4 market structure
  • find_m5_choch_signal() — primary entry signal via M5 Change of Character
                             (now accepts zone_buffer + min_rr_override)
  • Dynamic ATR-based stop-loss buffer (replaces static pip table)
  • calculate_zone_buffer()— ATR-based zone tap buffer (fuzzy detection)
  • score_setup()          — weighted scoring (replaces binary kill-switches)
  • grade_setup()          — converts score → (grade, risk_pct, min_rr)

Original functions (find_ltf_signal, ZoneMapper, atr, OB/FVG detectors)
are fully preserved for backward compatibility.

All tunable numbers come from config.py.
Zero infrastructure imports.
"""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple, TYPE_CHECKING

import src.config as config
from src.domain.entities import (
    Candle, Direction, LiquidityPool, SignalType,
    Timeframe, TradeSignal, Zone, ZoneType,
)
from src.domain.value_objects import PipCalculator

if TYPE_CHECKING:
    from src.application.diagnostics import DiagnosticsCollector

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  ATR
# ─────────────────────────────────────────────

def atr(candles: List[Candle], period: int = 14) -> float:
    """Average True Range over `period` candles. Returns price units."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


# ─────────────────────────────────────────────
#  ATR Zone Buffer
# ─────────────────────────────────────────────

def calculate_zone_buffer(atr_price: float, buffer_pct: Optional[float] = None) -> float:
    """
    Return a price-unit buffer to expand zone edges for tap detection.

    Prevents "point-perfect failure" where price misses a zone by 1-2 pips
    because institutions routinely front-run order blocks slightly.

    Args:
        atr_price:  H1 ATR(14) in price units (from the ``atr()`` function).
        buffer_pct: Fraction of ATR to use as buffer.  Defaults to
                    ``config.ZONE_ATR_BUFFER_PCT`` (10%).

    Returns:
        Buffer in price units (add to zone.high, subtract from zone.low).
    """
    if buffer_pct is None:
        buffer_pct = config.ZONE_ATR_BUFFER_PCT
    return atr_price * buffer_pct if atr_price > 0 else 0.0


def is_in_trading_session(dt: datetime) -> bool:
    """
    Return True if *dt* (UTC) is within an active trading window.

    Rules:
    - Weekends (Saturday=5, Sunday=6) are always blocked — no live market.
    - Each session in config.TRADING_SESSIONS has a 1-hour entry buffer after
      open and a 1-hour exit buffer before close (SESSION_BUFFER_MIN).
      Analysis and zone mapping are unaffected; only trade execution is gated.
    """
    if dt.weekday() >= 5:  # Saturday or Sunday
        return False

    buffer_min = getattr(config, "SESSION_BUFFER_MIN", 60)
    # Convert dt to fractional minutes-since-midnight for sub-hour precision.
    now_min = dt.hour * 60 + dt.minute
    for session in config.TRADING_SESSIONS.values():
        open_min  = session["start"] * 60 + buffer_min   # e.g. 08:00 + 60 = 09:00
        close_min = session["end"]   * 60 - buffer_min   # e.g. 16:00 - 60 = 15:00
        if open_min <= now_min < close_min:
            return True
    return False


# ─────────────────────────────────────────────
#  Weighted Setup Scorer
# ─────────────────────────────────────────────

def find_htf_zone_confluence(h1_zone: "Zone", h4_zones: List["Zone"]) -> bool:
    """
    Return True if the H1 zone overlaps with any same-direction H4 zone.

    Two institutional timeframes pointing at the same price level signals
    a significantly stronger area of interest. An H1 OB/FVG nested inside
    or sharing meaningful overlap with an H4 OB/FVG is treated as
    double-confirmed institutional supply/demand.

    Overlap is considered meaningful when it covers at least
    config.H4_ZONE_MIN_OVERLAP_PCT (20%) of the smaller zone's height.
    A zone fully nested inside a larger one also qualifies.
    """
    if not h4_zones:
        return False
    min_overlap_pct = getattr(config, "H4_ZONE_MIN_OVERLAP_PCT", 0.2)
    for h4z in h4_zones:
        if h4z.direction != h1_zone.direction:
            continue
        overlap_low  = max(h1_zone.low,  h4z.low)
        overlap_high = min(h1_zone.high, h4z.high)
        if overlap_high > overlap_low:
            overlap_size  = overlap_high - overlap_low
            smaller_size  = min(h1_zone.size, h4z.size)
            if smaller_size > 0 and overlap_size / smaller_size >= min_overlap_pct:
                return True
    return False


def find_m30_zone_confluence(h1_zone: "Zone", m30_zones: List["Zone"]) -> bool:
    """
    Return True if the H1 zone overlaps a same-direction M30 zone.

    M30 zones sit one step below H1 in the hierarchy.  When an H1 OB/FVG
    is also confirmed by an M30 OB/FVG at the same price level, the area
    has been printed as significant on two timeframes — stronger institutional
    interest.  Overlap threshold mirrors the H4 confluence check (20%).
    """
    if not m30_zones:
        return False
    min_overlap_pct = getattr(config, "M30_ZONE_MIN_OVERLAP_PCT", 0.2)
    for z in m30_zones:
        if z.direction != h1_zone.direction:
            continue
        overlap_low  = max(h1_zone.low,  z.low)
        overlap_high = min(h1_zone.high, z.high)
        if overlap_high > overlap_low:
            overlap_size = overlap_high - overlap_low
            smaller_size = min(h1_zone.size, z.size)
            if smaller_size > 0 and overlap_size / smaller_size >= min_overlap_pct:
                return True
    return False


def find_m5_zone_confluence(h1_zone: "Zone", m5_zones: List["Zone"]) -> bool:
    """
    Return True if the H1 zone overlaps a same-direction M5 zone.

    M5 zones are the tightest structural confirmation — an M5 OB/FVG inside
    an H1 demand zone means the most recent M5 bar close confirmed the level.
    This is the closest thing to real-time institutional confirmation before
    the M1 entry fires.  Same 20% overlap threshold as M30/H4 checks.
    """
    if not m5_zones:
        return False
    min_overlap_pct = getattr(config, "M5_ZONE_MIN_OVERLAP_PCT", 0.2)
    for z in m5_zones:
        if z.direction != h1_zone.direction:
            continue
        overlap_low  = max(h1_zone.low,  z.low)
        overlap_high = min(h1_zone.high, z.high)
        if overlap_high > overlap_low:
            overlap_size = overlap_high - overlap_low
            smaller_size = min(h1_zone.size, z.size)
            if smaller_size > 0 and overlap_size / smaller_size >= min_overlap_pct:
                return True
    return False


def score_setup(
    zone_direction:     "Direction",
    h4_bias:            Optional["Direction"],
    bar_price:          float,
    h4_eq:              Optional[float],
    d1_bias:            Optional["Direction"] = None,
    h4_zone_confluent:  bool = False,
    m30_zone_confluent: bool = False,
    m5_zone_confluent:  bool = False,
) -> int:
    """
    Return a weighted setup score for a zone in its current market context.

    Points awarded:
        +2 — Zone direction matches D1 macro bias (all three TFs aligned)
        +3 — Zone direction matches H4 bias (primary trend filter)
        +2 — Price is on the correct side of H4 equilibrium
               (discount for bulls, premium for bears)
        +2 — H1 zone overlaps a same-direction H4 OB/FVG (zone confluence)
        +1 — H1 zone overlaps a same-direction M30 OB/FVG (intra-hour confirm)
        +1 — H1 zone overlaps a same-direction M5 OB/FVG  (real-time confirm)

    Max score: 11  (D1 + H4 bias + equilibrium + H4 + M30 + M5 confluence)

    The M30 and M5 bonuses are intentionally small (+1 each) — they confirm
    the H1 zone is still active right now, but they never override the macro
    picture.  A trade with no M30/M5 confluence still gets the full D1/H4
    score and is never blocked.  A trade WITH M30/M5 confluence scores higher
    → gets larger position size via dynamic_risk() → more profit when right.

    Grade thresholds are unchanged (A+ ≥ 5, B ≥ 3) so existing good trades
    are not disrupted and no new hard blocks are introduced.

    Callers must apply REQUIRE_H4_BIAS and BLOCK_COUNTER_D1 pre-filters
    BEFORE calling this function — this function only scores, never blocks.
    """
    score = 0

    # D1 macro alignment (+2) — bonus for triple-timeframe confluence
    if d1_bias is not None and zone_direction == d1_bias:
        score += 2

    # H4 bias alignment (+3)
    if h4_bias is not None and zone_direction == h4_bias:
        score += 3

    # Discount / premium alignment (+2)
    if h4_eq is not None:
        if zone_direction == Direction.BULLISH and bar_price <= h4_eq:
            score += 2   # buying in the cheap half → good pricing
        elif zone_direction == Direction.BEARISH and bar_price >= h4_eq:
            score += 2   # selling in the expensive half → good pricing

    # H4 zone confluence (+2) — H1 zone nested inside / overlapping H4 zone
    if h4_zone_confluent:
        score += getattr(config, "H4_ZONE_CONFLUENCE_BONUS", 2)

    # M30 zone confluence (+1) — intra-hour structural confirmation
    if m30_zone_confluent:
        score += getattr(config, "M30_ZONE_CONFLUENCE_BONUS", 1)

    # M5 zone confluence (+1) — real-time bar-level confirmation
    if m5_zone_confluent:
        score += getattr(config, "M5_ZONE_CONFLUENCE_BONUS", 1)

    return score


def grade_setup(score: int) -> tuple[str, float, float]:
    """
    Convert a numeric setup score into (grade, risk_pct, min_rr).

    Returns:
        grade    — "A+", "B", or "C"
        risk_pct — % of account balance to risk on this trade
        min_rr   — minimum Reward:Risk ratio required
    """
    if score >= 5:
        return "A+", config.SETUP_RISK_A_PLUS, config.MIN_RR
    if score >= 3:
        return "B",  config.SETUP_RISK_B,      config.MIN_RR
    return "C", config.SETUP_RISK_C, config.COUNTER_TREND_MIN_RR


# ─────────────────────────────────────────────
#  Market Structure Break helpers
# ─────────────────────────────────────────────

def _is_swing_high(candles: List[Candle], index: int) -> bool:
    if index < 2 or index > len(candles) - 3:
        return False
    h = candles[index].high
    return (h > candles[index-1].high and h > candles[index-2].high and
            h > candles[index+1].high and h > candles[index+2].high)

def _is_swing_low(candles: List[Candle], index: int) -> bool:
    if index < 2 or index > len(candles) - 3:
        return False
    l = candles[index].low
    return (l < candles[index-1].low and l < candles[index-2].low and
            l < candles[index+1].low and l < candles[index+2].low)

def _breaks_structure_up(candles: List[Candle], from_index: int, lookback: int = 15) -> bool:
    start = max(0, from_index - lookback)
    swing_highs = [candles[start+i].high for i in range(from_index - start)
                   if _is_swing_high(candles, start+i)]
    if not swing_highs:
        return False
    target_high = max(swing_highs)
    impulse_window = candles[from_index: min(len(candles), from_index + config.OB_IMPULSE_CANDLES + 2)]
    return any(c.close > target_high for c in impulse_window)

def _breaks_structure_down(candles: List[Candle], from_index: int, lookback: int = 15) -> bool:
    start = max(0, from_index - lookback)
    swing_lows = [candles[start+i].low for i in range(from_index - start)
                  if _is_swing_low(candles, start+i)]
    if not swing_lows:
        return False
    target_low = min(swing_lows)
    impulse_window = candles[from_index: min(len(candles), from_index + config.OB_IMPULSE_CANDLES + 2)]
    return any(c.close < target_low for c in impulse_window)


# ─────────────────────────────────────────────
#  Mitigation Checker
# ─────────────────────────────────────────────

def _is_mitigated(candles: List[Candle], start_idx: int,
                  zone_low: float, zone_high: float, direction: Direction) -> bool:
    # A zone is consumed only when a candle CLOSES through it, not merely
    # wicks into it.  Using wick-touch here was stricter than evaluate_zone_health
    # check 1 (which uses close-through), causing zones to be silently discarded
    # in map_zones while still appearing valid to the health check — producing
    # an empty active-zone list in live trading.  Both gates must use the same
    # definition so a zone that survives map_zones also survives health evaluation.
    for i in range(start_idx, len(candles)):
        c = candles[i]
        if direction == Direction.BULLISH and c.close < zone_low:
            return True
        if direction == Direction.BEARISH and c.close > zone_high:
            return True
    return False


# ─────────────────────────────────────────────
#  NEW: H4 Macro Bias
# ─────────────────────────────────────────────

def find_h4_bias(h4_candles: List[Candle]) -> Optional[Direction]:
    """
    Determine macro directional bias from H4 market structure.

    Algorithm:
      1. Collect swing highs and swing lows from the last
         H4_EQUILIBRIUM_LOOKBACK candles (pivot-point detection).
      2. If the most recent swing high > previous swing high AND
         most recent swing low > previous swing low → BULLISH.
      3. If lower highs AND lower lows → BEARISH.
      4. Otherwise fall back to comparing the mean close of the first
         vs second half of the window (trend slope proxy).
      5. Returns None when the structure is clearly ranging / ambiguous.

    Returns:
        Direction.BULLISH | Direction.BEARISH | None (ranging)
    """
    lookback = config.H4_EQUILIBRIUM_LOOKBACK
    # Guard against None before calling len() — market_data_repo returns None
    # (not an empty list) when MT5 has no data for a symbol/timeframe.
    if not h4_candles or len(h4_candles) < lookback:
        return None

    window = h4_candles[-lookback:]

    # ── Pivot-point method ────────────────────────────────────────
    swing_highs: List[float] = []
    swing_lows:  List[float] = []

    for i in range(1, len(window) - 1):
        if window[i].high > window[i - 1].high and window[i].high > window[i + 1].high:
            swing_highs.append(window[i].high)
        if window[i].low < window[i - 1].low and window[i].low < window[i + 1].low:
            swing_lows.append(window[i].low)

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        higher_highs = swing_highs[-1] > swing_highs[-2]
        higher_lows  = swing_lows[-1]  > swing_lows[-2]
        lower_highs  = swing_highs[-1] < swing_highs[-2]
        lower_lows   = swing_lows[-1]  < swing_lows[-2]

        if higher_highs and higher_lows:
            logger.debug("H4 bias: BULLISH (HH+HL confirmed)")
            return Direction.BULLISH
        if lower_highs and lower_lows:
            logger.debug("H4 bias: BEARISH (LH+LL confirmed)")
            return Direction.BEARISH

    # ── Slope fallback: first-half vs second-half mean close ──────
    mid               = len(window) // 2
    first_half_close  = sum(c.close for c in window[:mid]) / mid
    second_half_close = sum(c.close for c in window[mid:]) / (len(window) - mid)

    threshold = 0.0015  # 0.15% price drift before we call a trend
    if second_half_close > first_half_close * (1 + threshold):
        logger.debug("H4 bias: BULLISH (slope fallback)")
        return Direction.BULLISH
    if second_half_close < first_half_close * (1 - threshold):
        logger.debug("H4 bias: BEARISH (slope fallback)")
        return Direction.BEARISH

    logger.debug("H4 bias: RANGING — no trade filter applied")
    return None  # ranging; caller decides whether to trade or skip


def find_d1_bias(d1_candles: List[Candle]) -> Optional[Direction]:
    """
    Determine the macro daily trend bias from D1 market structure.

    Uses identical HH/HL pivot logic as find_h4_bias but on the daily
    timeframe — giving a ~1-month macro view to anchor H4 entries.

    Returns:
        Direction.BULLISH  — daily trend is up (higher highs + higher lows)
        Direction.BEARISH  — daily trend is down (lower highs + lower lows)
        None               — daily structure is ranging / ambiguous
    """
    lookback = getattr(config, "D1_EQUILIBRIUM_LOOKBACK", 15)
    if not d1_candles or len(d1_candles) < lookback:
        return None

    window = d1_candles[-lookback:]

    swing_highs: List[float] = []
    swing_lows:  List[float] = []
    for i in range(1, len(window) - 1):
        if window[i].high > window[i - 1].high and window[i].high > window[i + 1].high:
            swing_highs.append(window[i].high)
        if window[i].low < window[i - 1].low and window[i].low < window[i + 1].low:
            swing_lows.append(window[i].low)

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
            logger.debug("D1 bias: BULLISH (HH+HL)")
            return Direction.BULLISH
        if swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
            logger.debug("D1 bias: BEARISH (LH+LL)")
            return Direction.BEARISH

    # Slope fallback
    mid = len(window) // 2
    first_avg  = sum(c.close for c in window[:mid])  / mid
    second_avg = sum(c.close for c in window[mid:])  / (len(window) - mid)
    threshold  = 0.003   # 0.3% drift — daily needs a wider gate than H4
    if second_avg > first_avg * (1 + threshold):
        logger.debug("D1 bias: BULLISH (slope)")
        return Direction.BULLISH
    if second_avg < first_avg * (1 - threshold):
        logger.debug("D1 bias: BEARISH (slope)")
        return Direction.BEARISH

    logger.debug("D1 bias: RANGING")
    return None


def compute_h4_equilibrium(h4_candles: List[Candle]) -> Tuple[float, float, float]:
    """
    Compute the premium / discount range from the active H4 window.

    Returns:
        (h4_high, h4_low, equilibrium_midpoint)
        Returns (0.0, 0.0, 0.0) when h4_candles is None or too short.
    """
    lookback = config.H4_EQUILIBRIUM_LOOKBACK
    if not h4_candles or len(h4_candles) < 1:
        return 0.0, 0.0, 0.0
    window   = h4_candles[-lookback:]
    h4_high  = max(c.high for c in window)
    h4_low   = min(c.low  for c in window)
    return h4_high, h4_low, (h4_high + h4_low) / 2.0


# ─────────────────────────────────────────────
#  Order Block Detection
# ─────────────────────────────────────────────

def find_order_blocks(candles: List[Candle], symbol: str, timeframe: Timeframe) -> List[Zone]:
    zones = []
    scan_range = len(candles) - config.OB_IMPULSE_CANDLES - 1

    for i in range(1, scan_range):
        c              = candles[i]
        impulse_window = candles[i + 1: i + 1 + config.OB_IMPULSE_CANDLES]
        if not impulse_window:
            continue

        avg_impulse    = sum(x.body_size for x in impulse_window) / len(impulse_window)
        strong_impulse = avg_impulse >= c.body_size * config.OB_IMPULSE_MULTIPLIER

        # Bullish OB
        if c.is_bearish and strong_impulse and _breaks_structure_up(candles, i + 1):
            if not _is_mitigated(candles, i + config.OB_IMPULSE_CANDLES,
                                  c.body_low, c.body_high, Direction.BULLISH):
                zones.append(Zone(
                    id=str(uuid.uuid4()), symbol=symbol,
                    zone_type=ZoneType.ORDER_BLOCK, direction=Direction.BULLISH,
                    high=c.body_high, low=c.body_low, created_at=c.time,
                    timeframe=timeframe, source_candle_index=i,
                ))

        # Bearish OB
        elif c.is_bullish and strong_impulse and _breaks_structure_down(candles, i + 1):
            if not _is_mitigated(candles, i + config.OB_IMPULSE_CANDLES,
                                  c.body_low, c.body_high, Direction.BEARISH):
                zones.append(Zone(
                    id=str(uuid.uuid4()), symbol=symbol,
                    zone_type=ZoneType.ORDER_BLOCK, direction=Direction.BEARISH,
                    high=c.body_high, low=c.body_low, created_at=c.time,
                    timeframe=timeframe, source_candle_index=i,
                ))

    return zones


# ─────────────────────────────────────────────
#  Fair Value Gap Detection
# ─────────────────────────────────────────────

def find_fair_value_gaps(
    candles:   List[Candle],
    symbol:    str,
    timeframe: Timeframe,
    atr_value: float = 0.0,
) -> List[Zone]:
    """
    Detect Fair Value Gaps (3-candle imbalances).

    atr_value: H1 ATR(14) passed in from ZoneMapper.  FVGs smaller than
               config.FVG_MIN_ATR_RATIO × ATR are micro-noise and are
               discarded.  Pass 0.0 to skip the size filter.
    """
    zones    = []
    min_size = atr_value * config.FVG_MIN_ATR_RATIO if atr_value > 0 else 0.0

    for i in range(2, len(candles) - 1):
        prev, curr, nxt = candles[i - 1], candles[i], candles[i + 1]

        # Bullish FVG
        if prev.high < nxt.low:
            fvg_low, fvg_high = prev.high, nxt.low
            if fvg_high - fvg_low < min_size:
                continue                          # too small — noise
            if _breaks_structure_up(candles, i + 1):
                if not _is_mitigated(candles, i + 2, fvg_low, fvg_high, Direction.BULLISH):
                    zones.append(Zone(
                        id=str(uuid.uuid4()), symbol=symbol,
                        zone_type=ZoneType.FAIR_VALUE_GAP, direction=Direction.BULLISH,
                        high=fvg_high, low=fvg_low,
                        created_at=curr.time, timeframe=timeframe,
                    ))

        # Bearish FVG
        elif prev.low > nxt.high:
            fvg_high, fvg_low = prev.low, nxt.high
            if fvg_high - fvg_low < min_size:
                continue                          # too small — noise
            if _breaks_structure_down(candles, i + 1):
                if not _is_mitigated(candles, i + 2, fvg_low, fvg_high, Direction.BEARISH):
                    zones.append(Zone(
                        id=str(uuid.uuid4()), symbol=symbol,
                        zone_type=ZoneType.FAIR_VALUE_GAP, direction=Direction.BEARISH,
                        high=fvg_high, low=fvg_low,
                        created_at=curr.time, timeframe=timeframe,
                    ))

    return zones


# ─────────────────────────────────────────────
#  Liquidity Pool
# ─────────────────────────────────────────────

def find_liquidity_pool(
    candles: List[Candle], symbol: str, timeframe: Timeframe,
) -> Optional[LiquidityPool]:
    window = candles[-config.LIQUIDITY_LOOKBACK:]
    if not window:
        return None
    return LiquidityPool(
        symbol=symbol, timeframe=timeframe,
        high=max(c.high for c in window),
        low=min(c.low   for c in window),
        lookback_candles=config.LIQUIDITY_LOOKBACK,
        # Use the last candle's timestamp — this is the broker-time moment
        # the pool was detected, directly from IC Markets candle data.
        created_at=window[-1].time,
    )


def find_m30_structural_tp(
    direction:   Direction,
    entry:       float,
    m30_zones:   List["Zone"],
    pip_calc:    PipCalculator,
    fallback_tp: float,
) -> float:
    """
    Find the nearest unmitigated M30 OB/FVG edge as a TP1 target.

    Why this beats the liquidity-pool TP:
      Backtest showed avg 86.3 pips to TP at trade's best point, while
      the monitor was closing trades at avg 51.9 pip MFE.  The market
      consistently stalled ~34 pips before the liquidity-pool TP.
      M30 OB/FVG edges are structural resistance/support levels that
      price reacts to in real time — they sit in that 20-40 pip range
      where the market actually delivers, not the 86-pip distant pool.

    Logic (bullish example):
      Scan m30_zones for same-direction zones whose LOW edge is above
      entry (price has not yet reached them — they are the next draw).
      Return the LOWEST such edge (nearest target first).
      If no M30 zone qualifies, return fallback_tp (liquidity pool).

    Bearish: scan for same-direction zones whose HIGH edge is below entry,
    return the HIGHEST such edge (nearest target).

    Args:
        direction:   trade direction
        entry:       entry price
        m30_zones:   list of Zone objects from run_m30_zone_mapping()
        pip_calc:    pip calculator for the symbol
        fallback_tp: liquidity pool TP to use when no M30 zone found

    Returns:
        float — TP price (M30 structural level or liquidity pool fallback)
    """
    if not m30_zones:
        return fallback_tp

    # Minimum meaningful distance: must be at least 1 pip beyond entry
    # so we never accidentally set TP at or below entry.
    min_dist = pip_calc.pips_to_price(1.0)

    candidates: List[float] = []

    for z in m30_zones:
        if z.direction != direction:
            continue
        if direction == Direction.BULLISH:
            # The zone's LOW edge is the first price that will be tested
            # when price approaches from below — that's the TP target.
            if z.low > entry + min_dist:
                candidates.append(z.low)
        else:
            # Bearish: zone HIGH edge is the first price tested from above.
            if z.high < entry - min_dist:
                candidates.append(z.high)

    if not candidates:
        return fallback_tp

    # Nearest structural level wins — closest target is most reachable.
    if direction == Direction.BULLISH:
        return min(candidates)   # nearest level above entry
    return max(candidates)       # nearest level below entry


def compute_tp1_price(
    direction:  Direction,
    entry:      float,
    stop_loss:  float,
) -> Optional[float]:
    """
    Compute the first take-profit target (TP1) for tiered exits.
 
    TP1 = entry ± (SL_distance × TIERED_TP1_RATIO)
 
    TP1 is the "lock in" level.  When price reaches it, 60% of the
    position is closed and SL moves to entry.  The remaining 40% runs
    to the full liquidity-pool TP.
 
    Returns None when tiered TP is disabled in config.
    """
    if not getattr(config, "TIERED_TP_ENABLED", True):
        return None
 
    sl_dist  = abs(entry - stop_loss)
    ratio    = getattr(config, "TIERED_TP1_RATIO", 2.0)
    tp1_dist = sl_dist * ratio
 
    if direction == Direction.BULLISH:
        return round(entry + tp1_dist, 5)
    return round(entry - tp1_dist, 5)
# Add these to src/application/analysis.py (after compute_tp1_price and before ZoneMapper)

def compute_tp2_price(
    direction:  Direction,
    entry:      float,
    stop_loss:  float,
) -> Optional[float]:
    """
    Compute the second take‑profit target (TP2) for tiered exits.
    TP2 = entry ± (SL_distance × TIERED_TP2_RATIO)
    Returns None if tiered TP is disabled.
    """
    if not getattr(config, "TIERED_TP_ENABLED", True):
        return None
    sl_dist = abs(entry - stop_loss)
    ratio   = getattr(config, "TIERED_TP2_RATIO", 3.5)
    tp2_dist = sl_dist * ratio
    if direction == Direction.BULLISH:
        return round(entry + tp2_dist, 5)
    return round(entry - tp2_dist, 5)


def get_initial_risk(entry: float, stop_loss: float) -> float:
    """Absolute price distance from entry to original SL."""
    return abs(entry - stop_loss)
 
def check_min_sl_pips(
    symbol:    str,
    sl_pips:   float,
) -> bool:
    """
    Return True if the SL distance is large enough to be a real trade.
 
    Filters out anomaly setups where price is at the exact zone edge
    and the SL is effectively zero (e.g. 0.1 pip on GBPUSD — unlivable
    in practice with 1 pip spread).
 
    Returns True (OK to trade) if sl_pips >= MIN_SL_PIPS for the symbol.
    Returns False (reject) if SL is too tight.
    """
    min_pips = config.MIN_SL_PIPS.get(symbol, config.MIN_SL_PIPS_DEFAULT)
    return sl_pips >= min_pips

# ─────────────────────────────────────────────
#  Dynamic SL buffer helper
# ─────────────────────────────────────────────

def _compute_sl_buffer(
    symbol:     str,
    atr_price:  float,
    pip_calc:   PipCalculator,
    spread_pips: float,
) -> float:
    """
    Return the stop-loss buffer in PRICE units.

    Priority:
      1. ATR-based: ATR(14) × ATR_SL_MULTIPLIERS[symbol]   (preferred)
      2. Static pip fallback: EXTRA_PIPS_SL + spread        (when ATR = 0)
    """
    if atr_price > 0:
        multiplier = config.ATR_SL_MULTIPLIERS.get(symbol, config.ATR_SL_MULTIPLIER_DEFAULT)
        return atr_price * multiplier

    # Fallback — ATR not yet available (very first bar of a new session)
    sl_pips_static = config.EXTRA_PIPS_SL.get(symbol, config.EXTRA_PIPS_SL_DEFAULT)
    return pip_calc.pips_to_price(spread_pips + sl_pips_static)


# ─────────────────────────────────────────────
#  NEW PRIMARY: M5 Change of Character Signal
# ─────────────────────────────────────────────

def find_m5_choch_signal(
    m5_candles:      List[Candle],
    zone:            Zone,
    pip_calc:        PipCalculator,
    spread_pips:     float,
    liquidity_pool:  LiquidityPool,
    atr_price:       float = 0.0,
    diag:            Optional["DiagnosticsCollector"] = None,
    zone_buffer:     float = 0.0,
    min_rr_override: Optional[float] = None,
    m30_zones:       Optional[List["Zone"]] = None,
    symbol:          str = "",
    precision_zone:  Optional[Tuple[float, float]] = None,
) -> Optional[TradeSignal]:
    """
    M5 Change of Character (ChoCh) entry — replaces the old M1 engulfing scan.

    Execution sequence (bullish example):
      1. Wait for price to TAP the H1 zone on the M5 chart.
         The tap check is expanded by ``zone_buffer`` (ATR × 10%) to prevent
         "point-perfect failure" where institutions front-run the OB by 1-3 pips.
      2. Track the retracement wave (downward move) inside the zone.
         Record the wave's highest point (origin/swing-high).
      3. A ChoCh fires when a candle closes AGGRESSIVELY above that
         swing-high — this is the DISPLACEMENT candle.
      4. Validate: displacement volume must exceed the 10-bar M5 average
         by MIN_DISPLACEMENT_VOLUME_RATIO (now 1.1 — filters pure noise).
         Also check CANDLE_SIZE_RATIO — body must be 2× prior 3-bar avg.
      5. Identify the M5 Fair Value Gap left by the displacement candle
         (the 3-candle imbalance: candle[i-2].high < displacement.low).
      6. Entry = at the lower boundary of that FVG (limit order on pullback).
      7. SL   = zone.low − ATR-based buffer.
      8. TP   = nearest H1 liquidity pool.
      9. Discard if RR < min_rr_override (or config.MIN_RR if not set).

    New parameters vs v2:
        zone_buffer:      Price-unit buffer added to zone edges for tap detection.
                          Pass ``calculate_zone_buffer(h1_atr)`` from the caller.
        min_rr_override:  Override config.MIN_RR — used for C-grade counter-trend
                          scalps which target a smaller 1.5:1 RR.

    Bearish logic is the exact mirror.
    """
    if len(m5_candles) < 10:
        return None

    symbol     = zone.symbol
    avg_volume = sum(c.volume for c in m5_candles[-11:-1]) / 10.0
    sl_buffer  = _compute_sl_buffer(symbol, atr_price, pip_calc, spread_pips)
    min_rr     = min_rr_override if min_rr_override is not None else config.MIN_RR
    vol_ratio  = getattr(config, "SYMBOL_DISPLACEMENT_VOLUME_RATIO", {}).get(
        symbol, config.MIN_DISPLACEMENT_VOLUME_RATIO)
    body_ratio = getattr(config, "SYMBOL_CANDLE_SIZE_RATIO", {}).get(
        symbol, config.CANDLE_SIZE_RATIO)

    zone_tapped           = False
    retracement_start_idx = None

    for i in range(1, len(m5_candles) - 1):
        c = m5_candles[i]

        # ── Step 1: zone tap (with ATR buffer) ────────────────────
        # Skip candles that predate the zone — they cannot be a valid tap
        # because the zone did not yet exist when those candles formed.
        # Without this, a zone tap from hours before map_zones ran would
        # start the retracement wave in the past, producing stale signals.
        if not zone_tapped:
            if c.time <= zone.created_at:
                continue
            if c.low <= zone.high + zone_buffer and c.high >= zone.low - zone_buffer:
                zone_tapped           = True
                retracement_start_idx = i
            continue   # keep scanning until zone is tapped

        # ── Step 2: measure retracement wave (EXCLUDING current candle) ─
        # CRITICAL: wave must NOT include candle i (the candidate displacement).
        # If wave included c, then wave_high >= c.high >= c.close (always true),
        # making c.close > wave_high mathematically impossible and producing 0 signals.
        wave = m5_candles[retracement_start_idx: i]  # up to but NOT including i
        if len(wave) < 2:
            continue

        # ── Step 3 & 4: ChoCh + volume + candle-size check ────────
        if zone.direction == Direction.BULLISH:
            # wave_high = highest high of retracement BEFORE displacement candle
            wave_high = max(c2.high for c2 in wave)
            if c.close <= wave_high:
                continue

            disp = c

            # Displacement quality gate (AND logic):
            # A genuine institutional displacement must show BOTH a volume spike
            # AND a large body.  The old OR gate (either alone sufficed) allowed
            # random noise candles through, producing only 8.3% entry win rate.
            volume_ok = avg_volume > 0 and (disp.volume / avg_volume) >= vol_ratio
            body_ok = False
            if i >= 3:
                prior_avg_body = sum(m5_candles[i - k].body_size for k in range(1, 4)) / 3.0
                body_ok = prior_avg_body > 0 and disp.body_size >= prior_avg_body * body_ratio

            if not volume_ok or not body_ok:
                if diag:
                    diag.record_rejection(symbol, "choch_weak_displacement")
                continue

            # ── Step 5: M5 FVG from displacement ──────────────────
            if i < 2:
                continue
            pre_disp = m5_candles[i - 2]
            entry    = pre_disp.high if pre_disp.high < disp.low else disp.open

            # Use M5 precision OB as SL anchor when available — tighter stop,
            # better RR, and structurally grounded inside the H1 zone.
            if precision_zone is not None:
                sl = precision_zone[0] - sl_buffer   # precision_zone[0] = OB low
            else:
                sl = disp.low - sl_buffer
            tp = find_m30_structural_tp(
                direction=Direction.BULLISH, entry=entry,
                m30_zones=m30_zones or [], pip_calc=pip_calc,
                fallback_tp=liquidity_pool.high,
            )

        else:  # BEARISH
            # wave_low = lowest low of retracement BEFORE displacement candle
            wave_low = min(c2.low for c2 in wave)
            if c.close >= wave_low:
                continue

            disp = c

            # Displacement quality gate (AND logic — same as bullish)
            volume_ok = avg_volume > 0 and (disp.volume / avg_volume) >= vol_ratio
            body_ok = False
            if i >= 3:
                prior_avg_body = sum(m5_candles[i - k].body_size for k in range(1, 4)) / 3.0
                body_ok = prior_avg_body > 0 and disp.body_size >= prior_avg_body * body_ratio

            if not volume_ok or not body_ok:
                if diag:
                    diag.record_rejection(symbol, "choch_weak_displacement")
                continue

            if i < 2:
                continue
            pre_disp = m5_candles[i - 2]
            entry    = pre_disp.low if pre_disp.low > disp.high else disp.open

            if precision_zone is not None:
                sl = precision_zone[1] + sl_buffer   # precision_zone[1] = OB high
            else:
                sl = disp.high + sl_buffer
            tp = find_m30_structural_tp(
                direction=Direction.BEARISH, entry=entry,
                m30_zones=m30_zones or [], pip_calc=pip_calc,
                fallback_tp=liquidity_pool.low,
            )

        # ── Step 9: RR gate (uses grade-specific min_rr) ──────────
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        if sl_dist == 0:
            continue
        rr = tp_dist / sl_dist

        if rr < min_rr:
            if diag:
                diag.record_rejection(symbol, "rr")
            continue

        # ── Step 10: Signal age gate ───────────────────────────────
        # The scan runs oldest-to-newest and accepts the first qualifying
        # candle.  Without this gate, a valid ChoCh from 4 hours ago fires
        # as if it's happening now — in live trading that would place a
        # limit order at a level price has long since left.
        # Only signals from the most recent MAX_SIGNAL_AGE_M5_BARS bars
        # are actionable.  The scan continues rather than returning None so
        # a more recent qualifying signal in the same window is not missed.
        max_age = getattr(config, "MAX_SIGNAL_AGE_M5_BARS", 12)  # 1 hour
        if i < len(m5_candles) - max_age:
            if diag:
                diag.record_rejection(symbol, "choch_signal_too_old")
            continue

        if diag:
            diag.record_signal(symbol)

        logger.info(
            "ChoCh signal ✓ | %s %s | entry=%.5f SL=%.5f TP=%.5f | "
            "RR=%.2f | vol_ratio=%.1f | ATR_buf=%.5f | zone_buf=%.5f",
            symbol, zone.direction.value, entry, sl, tp, rr,
            disp.volume / avg_volume if avg_volume > 0 else 0,
            sl_buffer, zone_buffer,
        )
        return TradeSignal(
            symbol=       zone.symbol,
            direction=    zone.direction,
            entry_price=  entry,
            stop_loss=    sl,
            take_profit=  tp,
            zone=         zone,
            signal_type=  SignalType.BULLISH_ENGULFING,
            volume_ratio= disp.volume / avg_volume if avg_volume > 0 else 1.0,
            created_at=   disp.time,
            risk_reward=  rr,
        )

    if not zone_tapped and diag:
        diag.record_rejection(symbol, "no_zone_tap")

    return None


# ─────────────────────────────────────────────
#  NEW: M1 Predictive Signal (Candle Range Theory / ICT 2022 Model)
# ─────────────────────────────────────────────

def find_m1_predictive_signal(
    m1_candles:      List[Candle],
    zone:            Zone,
    pip_calc:        PipCalculator,
    spread_pips:     float,
    liquidity_pool:  LiquidityPool,
    current_m5:      Optional[Candle]    = None,
    atr_price:       float               = 0.0,
    m5_bar_open_time: Optional[datetime] = None,
    diag:            Optional["DiagnosticsCollector"] = None,
    zone_buffer:     float               = 0.0,
    min_rr_override: Optional[float]     = None,
    m30_zones:       Optional[List["Zone"]] = None,
    m5_candles:      Optional[List[Candle]] = None,
) -> Optional[TradeSignal]:
    """
    M1 Predictive Entry — ICT Candle Range Theory (CRT) implementation.

    Core concept: every M5 candle is a range of M1 price action.  When M1
    prints a Liquidity Sweep → Market Structure Shift (MSS) → FVG sequence
    INSIDE an H1 zone, the M5 candle containing those M1 bars is biased to
    close in the MSS direction.  We enter on M1 instead of waiting for the
    full M5 displacement to close — getting a tighter stop and better RR.

    Entry sequence (bullish example):
      1. Zone tap confirmed — at least one M1 candle has traded inside the
         H1 demand zone (low ≤ zone.high + buffer, high ≥ zone.low − buffer).
      2. Liquidity sweep — a subsequent M1 candle wicks BELOW the lowest M1
         swing low seen since the zone tap (the stop hunt that fuels the move).
      3. MSS / ChoCh — an M1 displacement candle CLOSES above the highest M1
         swing high seen since the zone tap.  This is the directional flip.
      4. Quality gates (AND logic — both must pass):
           • Volume : displacement volume ≥ M1_DISPLACEMENT_VOLUME_RATIO × 10-bar avg
           • Body   : displacement body ≥ M1_CANDLE_SIZE_RATIO × 3-bar avg body
                      AND body ≥ M1_BODY_MIN_PRICE (absolute pip floor)
      5. M1 FVG — the 3-candle imbalance left by the displacement:
           entry = candle[i−2].high  (bottom of the FVG gap)
           If no clean gap exists, entry falls back to displacement candle open.
      6. M5 candle-colour gate — the M5 candle currently forming must be
         bullish (close > open).  A bearish M5 while M1 shows a ChoCh means
         ~50% of signals are noise; this single filter halves the false-signal
         rate without waiting for M5 to fully close.
      7. Zone size gate — zone must be ≥ M1_MIN_ZONE_SIZE_PIPS tall so M1
         wave structure has room to form before zone.low invalidates.
      8. SL = zone.mid − ATR sl_buffer  (NOT below M1 FVG — too tight to
         survive institutional sweeps that reach zone.mid before reversing).
         This gives ~7-pip SL vs ~14-pip SL in the M5 ChoCh approach.
      9. TP = liquidity pool (same as M5 ChoCh).
     10. RR gate — same MIN_RR as M5 ChoCh; will now pass at 30-pip TP where
         M5 ChoCh fails (RR 2.36 vs 1.24 at 30-pip target).

    Bearish logic is the exact mirror.

    Returns None when:
      - Fewer than 10 M1 candles available
      - Zone is narrower than M1_MIN_ZONE_SIZE_PIPS
      - No zone tap found in the M1 window
      - M1 wave is shorter than M1_MIN_WAVE_CANDLES
      - Volume or body quality gates fail
      - Current M5 candle colour opposes the trade direction
      - RR < min_rr_override (or config.MIN_RR)

    On success returns a TradeSignal identical in structure to find_m5_choch_signal
    so the caller needs no special handling — it's a drop-in with better RR.
    """
    if len(m1_candles) < 10:
        return None

    symbol    = zone.symbol
    pip_value = pip_calc.pips_to_price(1.0)

    # ── Zone size gate ─────────────────────────────────────────────
    min_zone_price = pip_calc.pips_to_price(
        getattr(config, "M1_MIN_ZONE_SIZE_PIPS", 6.0)
    )
    if zone.size < min_zone_price:
        if diag:
            diag.record_rejection(symbol, "m1_zone_too_narrow")
        return None

    # ── M5 candle-colour gate ──────────────────────────────────────
    # If the currently-forming M5 candle is already bearish (for a bullish
    # setup), the M1 ChoCh is likely noise inside a larger bearish M5 wave.
    # Skip now; the M5 ChoCh fallback will re-evaluate once M5 closes.
    if current_m5 is not None:
        if zone.direction == Direction.BULLISH and current_m5.close < current_m5.open:
            if diag:
                diag.record_rejection(symbol, "m1_m5_colour_mismatch")
            return None
        if zone.direction == Direction.BEARISH and current_m5.close > current_m5.open:
            if diag:
                diag.record_rejection(symbol, "m1_m5_colour_mismatch")
            return None

    # ── Bar-position gate ─────────────────────────────────────────
    # The M1 signal must fire in the EARLY portion of the M5 bar (first 3 min).
    # A ChoCh at minute 4 of the M5 bar is not predictive — it describes what
    # already happened.
    # In live trading, m5_bar_open_time is derived from broker time as
    # floor(now / 300) * 300 — this is the forming bar's open, which is what
    # we need.  Without it, we'd use current_m5.time (last CLOSED bar's open),
    # which is 5 minutes behind and would reject valid early-bar M1 signals.
    # Backtest callers do not pass m5_bar_open_time, so they fall back to
    # current_m5.time — preserving existing backtest behaviour.
    _m5_gate_open = m5_bar_open_time if m5_bar_open_time is not None else (
        current_m5.time if current_m5 is not None else None
    )
    if _m5_gate_open is not None and m1_candles:
        if not m1_is_early_in_m5_bar(m1_candles[-1].time, _m5_gate_open):
            if diag:
                diag.record_rejection(symbol, "m1_late_in_m5_bar")
            return None

    # ── Precompute thresholds ──────────────────────────────────────
    avg_volume   = sum(c.volume for c in m1_candles[-11:-1]) / 10.0
    sl_buffer    = _compute_sl_buffer(symbol, atr_price, pip_calc, spread_pips)
    min_rr       = min_rr_override if min_rr_override is not None else config.MIN_RR
    vol_ratio    = getattr(config, "M1_DISPLACEMENT_VOLUME_RATIO", 1.8)
    body_ratio   = getattr(config, "M1_CANDLE_SIZE_RATIO",         3.0)
    body_min     = getattr(config, "M1_BODY_MIN_PRICE",            0.00015)
    min_wave     = getattr(config, "M1_MIN_WAVE_CANDLES",          5)

    zone_tapped           = False
    sweep_occurred        = False
    retracement_start_idx = None
    sweep_extreme         = None   # lowest low seen after tap (bullish) / highest high (bearish)

    for i in range(1, len(m1_candles) - 1):
        c = m1_candles[i]

        # ── Step 1: zone tap ───────────────────────────────────────
        # Skip candles predating the zone — the zone did not exist when they formed.
        if not zone_tapped:
            if c.time <= zone.created_at:
                continue
            if c.low <= zone.high + zone_buffer and c.high >= zone.low - zone_buffer:
                zone_tapped           = True
                retracement_start_idx = i
                sweep_extreme = c.low if zone.direction == Direction.BULLISH else c.high
            continue

        # ── Step 2: track sweep extreme & detect liquidity sweep ──
        # For bullish: track the lowest low after tap — the sweep is the
        # candle whose wick goes below the most recent M1 swing low.
        # We record the sweep extreme without requiring a closed candle
        # below it; the sweep is the wick extension itself.
        if zone.direction == Direction.BULLISH:
            if c.low < sweep_extreme:
                sweep_extreme  = c.low
                sweep_occurred = True   # any new low = liquidity taken
        else:
            if c.high > sweep_extreme:
                sweep_extreme  = c.high
                sweep_occurred = True

        # ── Step 3: wave length gate ───────────────────────────────
        wave = m1_candles[retracement_start_idx: i]
        if len(wave) < min_wave:
            continue

        # ── Step 4: MSS / ChoCh + quality gates ───────────────────
        if zone.direction == Direction.BULLISH:
            wave_high = max(c2.high for c2 in wave)
            if c.close <= wave_high:
                continue

            # Require a prior liquidity sweep before accepting the ChoCh.
            # Without this, random M1 oscillation inside the zone fires.
            if not sweep_occurred:
                if diag:
                    diag.record_rejection(symbol, "m1_no_sweep_before_choch")
                continue

            # ── M5-swing significance gate ────────────────────────
            # The wave_high being broken must be near an M5 swing high,
            # not just a random M1 oscillation peak.  An M1 ChoCh that
            # breaks a level M5 also recognises is a genuine structure shift.
            if m5_candles and getattr(config, "M1_REQUIRE_M5_SWING_BREAK", True):
                if not m1_choch_breaks_m5_swing(wave_high, m5_candles,
                                                 Direction.BULLISH, pip_calc):
                    if diag:
                        diag.record_rejection(symbol, "m1_choch_not_m5_swing")
                    continue

            disp = c

            # Volume gate
            volume_ok = avg_volume > 0 and (disp.volume / avg_volume) >= vol_ratio

            # Body gate: ratio AND absolute floor
            body_ok = False
            if i >= 3:
                prior_avg_body = sum(
                    m1_candles[i - k].body_size for k in range(1, 4)
                ) / 3.0
                body_ok = (
                    prior_avg_body > 0
                    and disp.body_size >= prior_avg_body * body_ratio
                    and disp.body_size >= body_min
                )

            if not volume_ok or not body_ok:
                if diag:
                    diag.record_rejection(symbol, "m1_weak_displacement")
                continue

            # ── Step 5: M1 FVG entry ──────────────────────────────
            if i < 2:
                continue
            pre_disp = m1_candles[i - 2]
            entry    = pre_disp.high if pre_disp.high < disp.low else disp.open

            # ── Step 8: SL at zone.mid − buffer ───────────────────
            # zone.mid is the midpoint of the H1 zone.  Using this instead
            # of zone.low gives ~7-pip SL — tight enough to improve RR
            # dramatically vs M5 ChoCh (~14 pip), but wide enough to survive
            # standard institutional sweeps that reach zone midpoint.
            use_mid = getattr(config, "M1_SL_USE_ZONE_MID", True)
            sl_anchor = zone.mid if use_mid else zone.low
            sl        = sl_anchor - sl_buffer
            tp        = find_m30_structural_tp(
                direction=Direction.BULLISH, entry=entry,
                m30_zones=m30_zones or [], pip_calc=pip_calc,
                fallback_tp=liquidity_pool.high,
            )

        else:  # BEARISH
            wave_low = min(c2.low for c2 in wave)
            if c.close >= wave_low:
                continue

            if not sweep_occurred:
                if diag:
                    diag.record_rejection(symbol, "m1_no_sweep_before_choch")
                continue

            # ── M5-swing significance gate ────────────────────────
            if m5_candles and getattr(config, "M1_REQUIRE_M5_SWING_BREAK", True):
                if not m1_choch_breaks_m5_swing(wave_low, m5_candles,
                                                 Direction.BEARISH, pip_calc):
                    if diag:
                        diag.record_rejection(symbol, "m1_choch_not_m5_swing")
                    continue

            disp = c

            volume_ok = avg_volume > 0 and (disp.volume / avg_volume) >= vol_ratio
            body_ok   = False
            if i >= 3:
                prior_avg_body = sum(
                    m1_candles[i - k].body_size for k in range(1, 4)
                ) / 3.0
                body_ok = (
                    prior_avg_body > 0
                    and disp.body_size >= prior_avg_body * body_ratio
                    and disp.body_size >= body_min
                )

            if not volume_ok or not body_ok:
                if diag:
                    diag.record_rejection(symbol, "m1_weak_displacement")
                continue

            if i < 2:
                continue
            pre_disp = m1_candles[i - 2]
            entry    = pre_disp.low if pre_disp.low > disp.high else disp.open

            use_mid   = getattr(config, "M1_SL_USE_ZONE_MID", True)
            sl_anchor = zone.mid if use_mid else zone.high
            sl        = sl_anchor + sl_buffer
            tp        = find_m30_structural_tp(
                direction=Direction.BEARISH, entry=entry,
                m30_zones=m30_zones or [], pip_calc=pip_calc,
                fallback_tp=liquidity_pool.low,
            )

        # ── Step 10: RR gate ──────────────────────────────────────
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        if sl_dist == 0:
            continue
        rr = tp_dist / sl_dist

        if rr < min_rr:
            if diag:
                diag.record_rejection(symbol, "m1_rr")
            continue

        # ── Step 11: Signal age gate ───────────────────────────────
        # Same rationale as find_m5_choch_signal: a qualifying M1 ChoCh
        # that happened 60+ bars (1 hour) ago is stale in live trading.
        # Continue scanning so a more recent qualifying candle is found.
        max_age = getattr(config, "MAX_SIGNAL_AGE_M1_BARS", 60)  # 1 hour
        if i < len(m1_candles) - max_age:
            if diag:
                diag.record_rejection(symbol, "m1_signal_too_old")
            continue

        if diag:
            diag.record_signal(symbol)

        logger.info(
            "M1 Predictive signal ✓ | %s %s | entry=%.5f SL=%.5f TP=%.5f | "
            "RR=%.2f | vol_ratio=%.1f | sweep_extreme=%.5f | zone_mid=%.5f",
            symbol, zone.direction.value, entry, sl, tp, rr,
            disp.volume / avg_volume if avg_volume > 0 else 0,
            sweep_extreme or 0, zone.mid,
        )
        return TradeSignal(
            symbol=       zone.symbol,
            direction=    zone.direction,
            entry_price=  entry,
            stop_loss=    sl,
            take_profit=  tp,
            zone=         zone,
            signal_type=  SignalType.BULLISH_ENGULFING,
            volume_ratio= disp.volume / avg_volume if avg_volume > 0 else 1.0,
            created_at=   disp.time,
            risk_reward=  rr,
        )

    if not zone_tapped and diag:
        diag.record_rejection(symbol, "m1_no_zone_tap")

    return None


# ─────────────────────────────────────────────
#  LEGACY: M1 LTF Signal (kept for reference / backward compat)
# ─────────────────────────────────────────────

def find_ltf_signal(
    m1_candles:     List[Candle],
    zone:           Zone,
    pip_calc:       PipCalculator,
    spread_pips:    float,
    liquidity_pool: LiquidityPool,
    atr_price:      float = 0.0,
    diag:           Optional["DiagnosticsCollector"] = None,
) -> Optional[TradeSignal]:
    """
    DEPRECATED — aggressive M1 engulfing entry.

    This function is preserved only so the backtester can run a
    side-by-side comparison with the old strategy.  The live
    execution_service now calls find_m5_choch_signal() instead.

    The ATR-based SL buffer has been back-ported here so that even
    the legacy path benefits from dynamic sizing.
    """
    if len(m1_candles) < 15:
        return None

    symbol        = zone.symbol
    avg_volume_10 = sum(c.volume for c in m1_candles[-11:-1]) / 10.0
    zone_tapped   = False
    sl_buffer     = _compute_sl_buffer(symbol, atr_price, pip_calc, spread_pips)

    for i in range(1, len(m1_candles) - 1):
        c = m1_candles[i]

        if not (c.low <= zone.high and c.high >= zone.low):
            continue
        zone_tapped = True

        # Rejection wick
        wick_ok = (
            c.wick_ratio("lower") > config.REJECTION_WICK_RATIO
            if zone.direction == Direction.BULLISH
            else c.wick_ratio("upper") > config.REJECTION_WICK_RATIO
        )
        if not wick_ok:
            if diag:
                diag.record_rejection(symbol, "wick")
            continue

        # Volume
        if avg_volume_10 > 0 and (c.volume / avg_volume_10) < config.VOLUME_RATIO_MIN:
            if diag:
                diag.record_rejection(symbol, "volume")
            continue

        # Engulfing confirmation
        if i + 1 >= len(m1_candles):
            continue
        confirm = m1_candles[i + 1]
        if zone.direction == Direction.BULLISH:
            engulfing = confirm.is_bullish and confirm.close > c.body_high
        else:
            engulfing = confirm.is_bearish and confirm.close < c.body_low
        if not engulfing:
            if diag:
                diag.record_rejection(symbol, "engulfing")
            continue

        # Entry / SL / TP  (now uses dynamic ATR buffer)
        if zone.direction == Direction.BULLISH:
            entry, sl, tp = confirm.close, zone.low - sl_buffer, liquidity_pool.high
        else:
            entry, sl, tp = confirm.close, zone.high + sl_buffer, liquidity_pool.low

        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        if sl_dist == 0:
            continue
        rr = tp_dist / sl_dist
        if rr < config.MIN_RR:
            if diag:
                diag.record_rejection(symbol, "rr")
            continue

        if diag:
            diag.record_signal(symbol)

        return TradeSignal(
            symbol=      zone.symbol,
            direction=   zone.direction,
            entry_price= entry,
            stop_loss=   sl,
            take_profit= tp,
            zone=        zone,
            signal_type= SignalType.BULLISH_ENGULFING,
            volume_ratio= c.volume / avg_volume_10 if avg_volume_10 > 0 else 1.0,
            created_at=  confirm.time,
            risk_reward= rr,
        )

    if not zone_tapped and diag:
        diag.record_rejection(symbol, "no_zone_tap")

    return None



# Add to src/application/analysis.py

def compute_dynamic_tp(
    direction: Direction,
    entry: float,
    stop_loss: float,
    atr: float,
    baseline_atr: float = 0.0,
) -> tuple[float, float]:
    """
    Return (tp1_ratio, tp2_ratio) based on current ATR vs baseline.
    """
    if baseline_atr <= 0:
        return config.TIERED_TP1_RATIO, config.TIERED_TP2_RATIO

    ratio = atr / baseline_atr
    if ratio < 0.8:
        # Low volatility: use config defaults — smaller ATR means TPs are already
        # harder to reach, so don't push them further out.
        tp1, tp2 = config.TIERED_TP1_RATIO, config.TIERED_TP2_RATIO
    elif ratio > 1.2:
        # High volatility: extend runners — big ATR means price can travel further.
        # Scale up 33% from config to capture momentum without over-extending.
        tp1 = round(config.TIERED_TP1_RATIO * 1.33, 2)
        tp2 = round(config.TIERED_TP2_RATIO * 1.40, 2)
    else:
        # Normal regime: match static defaults
        tp1, tp2 = config.TIERED_TP1_RATIO, config.TIERED_TP2_RATIO
    return tp1, tp2


def score_trade(
    htf_aligned: bool,
    liquidity_sweep: bool,
    fvg_clean: bool,
    strong_displacement: bool,
    in_kill_zone: bool,
) -> int:
    score = 0
    if htf_aligned:
        score += config.SCORE_HTF_ALIGNMENT
    if liquidity_sweep:
        score += config.SCORE_LIQUIDITY_SWEEP
    if fvg_clean:
        score += config.SCORE_CLEAN_FVG
    if strong_displacement:
        score += config.SCORE_STRONG_DISPLACEMENT
    if in_kill_zone:
        score += config.SCORE_KILL_ZONE
    return score


def dynamic_risk(score: int) -> float:
    # Max possible score = 6 (HTF=2, sweep=1, FVG=1, displacement=1, kill_zone=1).
    # Threshold 5 catches excellent setups (HTF aligned + 3 of 4 other criteria),
    # not just mathematically perfect ones — avoids under-risking good setups.
    if score >= 5:
        return config.RISK_HIGH_CONVICTION
    elif score >= 3:
        return config.RISK_MEDIUM_CONVICTION
    else:
        return config.RISK_LOW_CONVICTION


def is_strong_trend(candles: List[Candle], direction: Direction) -> bool:
    if len(candles) < 4:
        return False
    last_three = candles[-3:]
    if direction == Direction.BULLISH:
        # 3 consecutive bullish candles with each close higher than the last.
        # The old check (close > prior HIGH) required near-marubozu candles and
        # almost never fired, making stall exits always trigger.
        return (
            all(c.is_bullish for c in last_three) and
            last_three[1].close > last_three[0].close and
            last_three[2].close > last_three[1].close
        )
    else:
        return (
            all(c.is_bearish for c in last_three) and
            last_three[1].close < last_three[0].close and
            last_three[2].close < last_three[1].close
        )


def is_kill_zone(dt: datetime) -> bool:
    hour = dt.hour
    for start, end in config.KILL_ZONES.values():
        if start <= hour < end:
            return True
    return False


# ─────────────────────────────────────────────
#  M5 Zone Story Reader
# ─────────────────────────────────────────────

def read_m5_zone_story(
    zone:       "Zone",
    m5_candles: List["Candle"],
    h1_atr:     float,
) -> str:
    """
    Read the story that M5 bars inside the H1 zone are telling.

    The H1 zone is a mapped address (e.g. "demand at 1.2700–1.2730").
    But who is actually inside that building right now?  The M5 bars
    that have printed INSIDE the zone since price arrived reveal the
    intent of the institutions operating at that level:

    "absorbing"   — price is being held up / turned around inside the zone.
                    Signs: rejection wicks pointing away from zone edge,
                    closes moving toward the expected TP direction,
                    at least one bullish close (for demand) after first touch.

    "distributing"— institutions are using the zone as a supply/distribution
                    point instead of demand.  Signs: consecutive closes drifting
                    AWAY from TP (deeper into or through the zone), bodies
                    pointing against zone direction with no wick rejection.

    "neutral"     — not enough M5 bars inside the zone yet to tell a story
                    (< MIN_ZONE_STORY_BARS bars), or mixed signals.

    Returns one of: "absorbing" | "distributing" | "neutral"
    """
    min_bars = getattr(config, "MIN_ZONE_STORY_BARS", 3)

    # Collect M5 candles whose BODY overlaps the zone (price was inside)
    inside = [
        c for c in m5_candles
        if c.body_low <= zone.high and c.body_high >= zone.low
    ]

    if len(inside) < min_bars:
        return "neutral"

    # Use the last N bars inside the zone so we read the CURRENT story,
    # not what happened 20 bars ago when price first grazed the zone.
    window = inside[-getattr(config, "ZONE_STORY_WINDOW", 6):]

    vol_sample = [c.volume for c in m5_candles[-11:-1]]
    avg_vol    = sum(vol_sample) / len(vol_sample) if vol_sample else 0.0
    vol_thresh = getattr(config, "ZONE_STORY_VOL_THRESH", 1.2)

    if zone.direction == Direction.BULLISH:
        # Absorption signs (demand zone holding):
        #   • candles closing in the upper half of their range (wick below = rejection)
        #   • at least one bullish close with elevated volume
        #   • closing prices trending UP from first to last bar in window
        bullish_closes  = sum(1 for c in window if c.is_bullish)
        strong_bullish  = sum(1 for c in window
                              if c.is_bullish and avg_vol > 0 and c.volume >= avg_vol * vol_thresh)
        # Lower-wick ratio > 0.4 on majority = wicks pointing down = demand rejection
        wick_rejections = sum(1 for c in window if c.wick_ratio("lower") > 0.4)

        # Distribution signs (zone being sold INTO):
        bearish_closes  = sum(1 for c in window if c.is_bearish)
        strong_bearish  = sum(1 for c in window
                              if c.is_bearish and avg_vol > 0 and c.volume >= avg_vol * vol_thresh)
        # Closes moving lower — the zone is leaking
        closes_falling  = all(
            window[i].close < window[i - 1].close
            for i in range(1, len(window))
        )

        # Distributing: majority bearish + strong bearish vol + closes leaking lower
        dist_threshold = getattr(config, "ZONE_STORY_DIST_MAJORITY", 0.6)
        if (bearish_closes / len(window) >= dist_threshold
                and strong_bearish >= 1
                and closes_falling):
            return "distributing"

        # Absorbing: majority bullish, at least one strong bullish, wick rejections
        abs_threshold = getattr(config, "ZONE_STORY_ABS_MAJORITY", 0.5)
        if (bullish_closes / len(window) >= abs_threshold
                and (strong_bullish >= 1 or wick_rejections >= 2)):
            return "absorbing"

    else:  # BEARISH zone
        bearish_closes  = sum(1 for c in window if c.is_bearish)
        strong_bearish  = sum(1 for c in window
                              if c.is_bearish and avg_vol > 0 and c.volume >= avg_vol * vol_thresh)
        wick_rejections = sum(1 for c in window if c.wick_ratio("upper") > 0.4)

        bullish_closes  = sum(1 for c in window if c.is_bullish)
        strong_bullish  = sum(1 for c in window
                              if c.is_bullish and avg_vol > 0 and c.volume >= avg_vol * vol_thresh)
        closes_rising   = all(
            window[i].close > window[i - 1].close
            for i in range(1, len(window))
        )

        dist_threshold = getattr(config, "ZONE_STORY_DIST_MAJORITY", 0.6)
        if (bullish_closes / len(window) >= dist_threshold
                and strong_bullish >= 1
                and closes_rising):
            return "distributing"

        abs_threshold = getattr(config, "ZONE_STORY_ABS_MAJORITY", 0.5)
        if (bearish_closes / len(window) >= abs_threshold
                and (strong_bearish >= 1 or wick_rejections >= 2)):
            return "absorbing"

    return "neutral"


# ─────────────────────────────────────────────
#  M30 Story Reader
# ─────────────────────────────────────────────

def read_m30_zone_story(
    zone:        "Zone",
    m30_candles: List["Candle"],
    h1_atr:      float,
) -> str:
    """
    Read the M30 bar story relative to the H1 zone — one timeframe above M5.

    M30 bars are 30 minutes wide and contain six M5 bars each.  When the
    last M30 bar CLOSED inside or through the H1 zone in the wrong direction,
    it is telling a much louder story than a single M5 bar.

    Returns:
        "against"  — last M30 bar closed against the zone direction
                     (e.g. bearish close inside a demand zone).  This is the
                     strongest early warning that institutions are distributing
                     from the zone, not absorbing.
        "with"     — last M30 bar supports the zone direction (bullish close
                     inside demand zone = absorption at the level).
        "neutral"  — M30 bar did not interact with the zone, or mixed.
    """
    if not m30_candles:
        return "neutral"

    # Find M30 candles that overlap the zone
    zone_m30 = [
        c for c in m30_candles
        if c.low <= zone.high and c.high >= zone.low
    ]
    if not zone_m30:
        return "neutral"

    last = zone_m30[-1]

    if zone.direction == Direction.BULLISH:
        if last.is_bearish and last.close < zone.high:
            # Bearish M30 candle closed INSIDE or below demand zone — distribution
            return "against"
        if last.is_bullish and last.close > last.open:
            return "with"
    else:
        if last.is_bullish and last.close > zone.low:
            return "against"
        if last.is_bearish and last.close < last.open:
            return "with"

    return "neutral"


# ─────────────────────────────────────────────
#  M5 Sub-Zone Precision Finder
# ─────────────────────────────────────────────

def find_m5_precision_entry(
    zone:       "Zone",
    m5_candles: List["Candle"],
    pip_calc:   "PipCalculator",
    h1_atr:     float,
) -> Optional[Tuple[float, float]]:
    """
    Find the tightest valid entry inside an H1 zone using the M5 sub-structure.

    The H1 OB is the address.  Inside that address the M5 bars have printed
    their own micro OBs and FVGs — these are the ACTUAL institutional order
    clusters.  Entering at the M5 OB inside the H1 OB gives:
        • Tighter SL (M5 OB edge, not H1 OB edge)
        • Better RR (same TP, smaller risk)
        • Price is already showing the institutional level is respected

    Algorithm (bullish example):
        1. Find all M5 candles whose body is inside the H1 zone.
        2. Among those, find the most recent M5 bearish candle followed by
           a strong bullish move (= M5 OB inside the H1 zone).
        3. Return (ob_low, ob_high) as the precision entry zone.
           Caller uses ob_high as entry, ob_low - buffer as SL.

    Returns (precision_low, precision_high) or None if no M5 OB found inside.
    """
    min_pips = getattr(config, "M5_PRECISION_MIN_PIPS", 2.0)
    min_price = pip_calc.pips_to_price(min_pips)

    inside = [
        (i, c) for i, c in enumerate(m5_candles)
        if c.body_low >= zone.low and c.body_high <= zone.high
    ]
    if len(inside) < 3:
        return None

    if zone.direction == Direction.BULLISH:
        # Look for the last M5 bearish candle inside the zone whose next
        # candle is a strong bullish move — that's the M5 OB (demand).
        for k in range(len(inside) - 2, 0, -1):
            idx, ob_candle = inside[k]
            if not ob_candle.is_bearish:
                continue
            # Next candle after OB must be bullish and have a large body
            if idx + 1 >= len(m5_candles):
                continue
            nxt = m5_candles[idx + 1]
            if not nxt.is_bullish:
                continue
            if nxt.body_size < ob_candle.body_size * 1.5:
                continue
            # OB must be meaningful size
            if ob_candle.body_size < min_price:
                continue
            return (ob_candle.body_low, ob_candle.body_high)

    else:  # BEARISH
        for k in range(len(inside) - 2, 0, -1):
            idx, ob_candle = inside[k]
            if not ob_candle.is_bullish:
                continue
            if idx + 1 >= len(m5_candles):
                continue
            nxt = m5_candles[idx + 1]
            if not nxt.is_bearish:
                continue
            if nxt.body_size < ob_candle.body_size * 1.5:
                continue
            if ob_candle.body_size < min_price:
                continue
            return (ob_candle.body_low, ob_candle.body_high)

    return None


# ─────────────────────────────────────────────
#  M1 Bar-Position-in-M5 Gate
# ─────────────────────────────────────────────

def m1_is_early_in_m5_bar(
    m1_signal_time: datetime,
    m5_bar_open_time: datetime,
) -> bool:
    """
    Return True when the M1 signal fired in the EARLY portion of the M5 bar.

    ICT Candle Range Theory requires that the M1 sweep + MSS happens early
    inside the M5 bar so that the prediction 'this M5 bar will close bullish'
    still has time to play out.  If the M1 ChoCh fires at minute 3-4 of the
    M5 bar there is no predictive value — the M5 bar is nearly done and the
    signal is describing what already happened, not what is about to happen.

    Threshold: M1_EARLY_BAR_MINUTES (default 3).  The M1 signal must fire
    within the first 3 minutes of the M5 bar (minutes 0, 1, 2 are valid;
    minutes 3, 4 are rejected).
    """
    if m5_bar_open_time is None:
        return True   # can't determine — allow through
    elapsed_seconds = (m1_signal_time - m5_bar_open_time).total_seconds()
    max_seconds = getattr(config, "M1_EARLY_BAR_MINUTES", 3) * 60
    return 0 <= elapsed_seconds < max_seconds


def get_current_m5_bar_open(m5_candles: List["Candle"]) -> Optional[datetime]:
    """Return the open time of the most recent (possibly still-forming) M5 bar."""
    if not m5_candles:
        return None
    return m5_candles[-1].time


# ─────────────────────────────────────────────
#  M1 ChoCh vs M5 Swing Validator
# ─────────────────────────────────────────────

def m1_choch_breaks_m5_swing(
    m1_wave_extreme: float,
    m5_candles:      List["Candle"],
    direction:       "Direction",
    pip_calc:        "PipCalculator",
) -> bool:
    """
    Return True when the M1 ChoCh wave-extreme (wave_high for bulls,
    wave_low for bears) coincides with a SIGNIFICANT M5 swing level.

    Why this matters:
        Inside an H1 zone the M1 is oscillating.  An M1 ChoCh that merely
        breaks a random M1 high that sits in the middle of a bearish M5 candle
        body is noise — it is describing M1 internal volatility, not a real
        change of character.  A genuine institutional shift MUST break a level
        that the M5 timeframe also recognises as a swing point.

    Method:
        Check whether m1_wave_extreme is within M1_M5_SWING_PROXIMITY_PIPS
        of any M5 candle high (bull) or low (bear) in the recent lookback.
        If yes, breaking it carries M5-level significance.

    Returns True (valid) or False (M1 noise — reject).
    """
    lookback = getattr(config, "M1_M5_SWING_LOOKBACK", 12)
    prox     = pip_calc.pips_to_price(
        getattr(config, "M1_M5_SWING_PROXIMITY_PIPS", 3.0)
    )
    window = m5_candles[-lookback:] if len(m5_candles) >= lookback else m5_candles

    if direction == Direction.BULLISH:
        m5_highs = [c.high for c in window]
        return any(abs(m1_wave_extreme - h) <= prox for h in m5_highs)
    else:
        m5_lows = [c.low for c in window]
        return any(abs(m1_wave_extreme - l) <= prox for l in m5_lows)


# ─────────────────────────────────────────────
#  Zone Health Re-Evaluator
# ─────────────────────────────────────────────

def evaluate_zone_health(
    zone:       "Zone",
    h1_candles: List["Candle"],
    m5_candles: List["Candle"],
    h1_atr:     float,
) -> tuple[bool, str]:
    """
    Re-evaluate a zone's validity at the exact moment price arrives at it.

    Called just before any entry signal search begins.  Zones are mapped
    on H1 bar close — by the time price actually taps the zone, hours may
    have passed and the market may have flipped.  Four checks run in order
    of severity; the first failure short-circuits and rejects the zone.

    Returns:
        (True,  "ok")           — zone is still valid, proceed to signal search
        (False, "<reason>")     — zone is stale/consumed/flipped, skip it

    Checks (in order):
      1. H1 candle CLOSED through the zone after it was created
         → liquidity was consumed; trading it again is trading into nothing.
      2. Three consecutive H1 candles closed against the zone direction
         (all bearish + descending for a bullish zone, vice versa)
         → market structure has flipped since the zone formed.
      3. Touch count exceeded the zone type's durability limit
         (FVG: 1 reuse max; OB: 2 retests max)
         → institutional orders have been absorbed over repeated taps.
      4. Last four M5 candles are ALL against the zone direction AND at
         least two carry above-average volume
         → a local displacement is forming against the zone right now.
    """
    # ── Check 1: H1 close-through (mitigation) ──────────────────────
    # Only examine candles that formed AFTER the zone was created so we
    # don't penalise a zone for the very candle that built it.
    post_zone_h1 = [c for c in h1_candles if c.time > zone.created_at]
    for candle in post_zone_h1:
        if zone.direction == Direction.BULLISH and candle.close < zone.low:
            return False, "h1_close_mitigated"
        if zone.direction == Direction.BEARISH and candle.close > zone.high:
            return False, "h1_close_mitigated"

    # ── Check 2: H1 structure flipped against zone ───────────────────
    if len(post_zone_h1) >= 3:
        last3 = post_zone_h1[-3:]
        if zone.direction == Direction.BULLISH:
            if (all(c.is_bearish for c in last3) and
                    last3[2].close < last3[1].close < last3[0].close):
                return False, "h1_structure_flipped"
        else:
            if (all(c.is_bullish for c in last3) and
                    last3[2].close > last3[1].close > last3[0].close):
                return False, "h1_structure_flipped"

    # ── Check 3: Touch count — zone liquidity exhausted ─────────────
    if zone.zone_type == ZoneType.FAIR_VALUE_GAP and zone.touch_count >= 2:
        return False, "fvg_over_tapped"
    if zone.zone_type == ZoneType.ORDER_BLOCK and zone.touch_count >= 3:
        return False, "ob_over_tapped"

    # ── Check 4: M5 momentum displacement against zone ───────────────
    # 3/4 recent M5 candles oppose the zone AND at least 1 carries a volume
    # spike (>1.5× avg) → catching distribution/accumulation EARLIER than
    # the old 4/4 + 2-strong gate which fired only after 20+ pip adverse move.
    if len(m5_candles) >= 5:
        last4 = m5_candles[-4:]
        vol_sample = [c.volume for c in m5_candles[-11:-1]]
        avg_vol = sum(vol_sample) / len(vol_sample) if vol_sample else 0.0
        vol_spike_threshold = getattr(config, "ZONE_HEALTH_VOL_SPIKE", 1.5)
        against_min         = getattr(config, "ZONE_HEALTH_AGAINST_MIN", 3)
        strong_min          = getattr(config, "ZONE_HEALTH_STRONG_MIN",  1)
        if zone.direction == Direction.BULLISH:
            against = [c for c in last4 if c.is_bearish]
            strong  = sum(1 for c in against
                          if avg_vol > 0 and c.volume >= avg_vol * vol_spike_threshold)
        else:
            against = [c for c in last4 if c.is_bullish]
            strong  = sum(1 for c in against
                          if avg_vol > 0 and c.volume >= avg_vol * vol_spike_threshold)
        if len(against) >= against_min and strong >= strong_min:
            return False, "m5_momentum_against_zone"

    # ── Check 5: M5 internal distribution / absorption story ────────────
    # Reads the sub-bar story INSIDE the zone — are M5 bars being absorbed
    # (wicks rejecting from zone edge, closes moving toward TP) or distributed
    # (closes drifting away from TP, bodies pointing against zone direction)?
    zone_story = read_m5_zone_story(zone, m5_candles, h1_atr)
    if zone_story == "distributing":
        return False, "m5_zone_distributing"

    return True, "ok"


# ─────────────────────────────────────────────
#  Opposing Opportunity Finder
# ─────────────────────────────────────────────

def find_opposing_opportunity(
    rejected_zone:  "Zone",
    m5_candles:     List["Candle"],
    pip_calc:       "PipCalculator",
    spread_pips:    float,
    liquidity_pool: "LiquidityPool",
    h1_atr:         float,
    zone_buffer:    float = 0.0,
    min_rr:         float = 2.0,
    diag:           Optional["DiagnosticsCollector"] = None,
) -> Optional[Tuple["TradeSignal", "Zone"]]:
    """
    When institutional momentum fires AGAINST a mapped zone, evaluate
    whether that opposing move qualifies as a valid entry in the new direction.

    The displacement that invalidated the original zone IS a ChoCh — just
    in the opposite direction.  A synthetic zone is constructed at the
    rejection point and the standard ChoCh signal finder is reused unchanged.

    Concept (bullish zone rejected bearishly):
      • The zone HIGH becomes institutional supply — price rejected from there.
      • Synthetic BEARISH zone spans from zone.high down to zone.mid.
      • find_m5_choch_signal looks for: tap of that supply → brief pullback →
        bearish displacement (already forming in the m5 candles we detected).
      • SL goes above zone.high; TP targets pool.low.

    Returns (TradeSignal, synthetic_zone) if valid, else None.
    """
    if not m5_candles:
        return None

    opp_direction = (
        Direction.BEARISH
        if rejected_zone.direction == Direction.BULLISH
        else Direction.BULLISH
    )

    # Build synthetic zone at the institutional rejection level.
    # For a rejected bullish zone: price bounced OFF zone.high → that's supply.
    # For a rejected bearish zone: price bounced OFF zone.low  → that's demand.
    if opp_direction == Direction.BEARISH:
        syn_high = rejected_zone.high + zone_buffer
        syn_low  = rejected_zone.mid
    else:
        syn_high = rejected_zone.mid
        syn_low  = rejected_zone.low - zone_buffer

    synthetic_zone = Zone(
        id=          str(uuid.uuid4()),
        symbol=      rejected_zone.symbol,
        zone_type=   ZoneType.ORDER_BLOCK,
        direction=   opp_direction,
        high=        round(syn_high, 5),
        low=         round(syn_low,  5),
        created_at=  m5_candles[-1].time,
        timeframe=   Timeframe.M5,
    )

    signal = find_m5_choch_signal(
        m5_candles=      m5_candles,
        zone=            synthetic_zone,
        pip_calc=        pip_calc,
        spread_pips=     spread_pips,
        liquidity_pool=  liquidity_pool,
        atr_price=       h1_atr,
        zone_buffer=     zone_buffer,
        min_rr_override= min_rr,
        diag=            diag,
    )

    if signal is None:
        return None

    logger.info(
        "ZONE FLIP ✓ | %s %s | opposing %s entry found | "
        "entry=%.5f SL=%.5f TP=%.5f RR=%.2f",
        rejected_zone.symbol, rejected_zone.direction.value,
        opp_direction.value,
        signal.entry_price, signal.stop_loss, signal.take_profit,
        signal.risk_reward,
    )
    return signal, synthetic_zone


# ─────────────────────────────────────────────
#  Zone Mapper
# ─────────────────────────────────────────────

class ZoneMapper:
    """Runs Phase 1 (Zone Mapping) on each H1 bar close."""

    def map_zones(
        self,
        candles:   List[Candle],
        symbol:    str,
        timeframe: Timeframe = Timeframe.H1,
    ) -> tuple[List[Zone], Optional[LiquidityPool]]:
        # Use the most recent 15 candles for ATR so the FVG size threshold
        # reflects current-session volatility, not a multi-day average that
        # inflates during post-spike windows and starves the zone list.
        # The full candle list is still used for structure detection (OB/FVG
        # scan) — only the ATR computation is narrowed.
        # NOTE: atr(period=14) requires period+1 = 15 candles to produce 14
        # True Range values (each TR needs the previous bar's close).  Using
        # candles[-14:] gave exactly 14 — always one short — so atr() always
        # returned 0.0.  Fixed: slice to 15.
        atr_window = candles[-15:] if len(candles) >= 15 else candles
        h1_atr    = atr(atr_window)       # used to filter micro-FVGs
        obs       = find_order_blocks(candles, symbol, timeframe)
        fvgs      = find_fair_value_gaps(candles, symbol, timeframe, atr_value=h1_atr)
        pool      = find_liquidity_pool(candles, symbol, timeframe)

        # ── Zone quality cap ──────────────────────────────────────
        # Without a cap every 100-bar window spawns dozens of zones, most of
        # which are old and thin.  We keep only the top-N by a composite score:
        #   60% recency  (newer created_at = more relevant to current price)
        #   40% size     (larger zone relative to ATR = more institutional weight)
        # OBs and FVGs are capped separately so neither type crowds out the other.
        if h1_atr > 0:
            newest_ts = max((z.created_at for z in obs + fvgs), default=None)
            oldest_ts = min((z.created_at for z in obs + fvgs), default=None)
            ts_range  = (newest_ts - oldest_ts).total_seconds() if (newest_ts and oldest_ts and newest_ts != oldest_ts) else 1.0

            def _score(z: Zone) -> float:
                age_norm = (z.created_at - oldest_ts).total_seconds() / ts_range if ts_range > 0 else 0.0
                size_norm = (z.high - z.low) / h1_atr
                return 0.6 * age_norm + 0.4 * size_norm

            obs.sort(key=_score, reverse=True)
            fvgs.sort(key=_score, reverse=True)

        if timeframe == Timeframe.H4:
            max_obs  = getattr(config, "H4_ZONE_MAX_OBS",   5)
            max_fvgs = getattr(config, "H4_ZONE_MAX_FVGS",  4)
        elif timeframe == Timeframe.M30:
            max_obs  = getattr(config, "M30_ZONE_MAX_OBS",  4)
            max_fvgs = getattr(config, "M30_ZONE_MAX_FVGS", 3)
        elif timeframe == Timeframe.M5:
            max_obs  = getattr(config, "M5_ZONE_MAX_OBS",   3)
            max_fvgs = getattr(config, "M5_ZONE_MAX_FVGS",  3)
        else:
            max_obs  = getattr(config, "MAX_OBS_PER_BAR",  10)
            max_fvgs = getattr(config, "MAX_FVGS_PER_BAR",  8)
        obs  = obs[:max_obs]
        fvgs = fvgs[:max_fvgs]

        all_zones = obs + fvgs
        logger.info("[%s] %d OBs, %d FVGs after cap (ATR=%.5f)  pool=H%.5f/L%.5f",
                    symbol, len(obs), len(fvgs), h1_atr,
                    pool.high if pool else 0, pool.low if pool else 0)
        return all_zones, pool