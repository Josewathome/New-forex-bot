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
    Return True if *dt* (UTC) falls within any configured trading session.

    Sessions are defined in config.TRADING_SESSIONS as {start_hour, end_hour}
    pairs (inclusive start, exclusive end).  Trades outside these windows are
    filtered out to avoid Asian-session chop and low-liquidity dead zones.
    """
    hour = dt.hour
    for session in config.TRADING_SESSIONS.values():
        if session["start"] <= hour < session["end"]:
            return True
    return False


# ─────────────────────────────────────────────
#  Weighted Setup Scorer
# ─────────────────────────────────────────────

def score_setup(
    zone_direction: "Direction",
    h4_bias:        Optional["Direction"],
    bar_price:      float,
    h4_eq:          Optional[float],
) -> int:
    """
    Return a weighted setup score for a zone in its current market context.

    This replaces the old binary kill-switches (H4 bias conflict → skip,
    premium/discount mismatch → skip) with a probabilistic scoring system
    where multiple "Maybes" can total to a "Yes".

    Points awarded:
        +3 — Zone direction matches H4 bias (trend-following trade)
        +2 — Price is on the correct side of H4 equilibrium
               (discount for bulls, premium for bears)

    Grade mapping (for the caller to apply):
        score ≥ 5  →  A+ setup  →  full risk (SETUP_RISK_A_PLUS)
        score ≥ 3  →  B  setup  →  half risk (SETUP_RISK_B)
        score < 3  →  C  setup  →  quarter risk counter-trend (SETUP_RISK_C)

    Returns:
        Integer score 0–5.
    """
    score = 0

    # H4 bias alignment (+3)
    if h4_bias is not None and zone_direction == h4_bias:
        score += 3

    # Discount / premium alignment (+2)
    if h4_eq is not None:
        if zone_direction == Direction.BULLISH and bar_price <= h4_eq:
            score += 2   # buying in the cheap half → good pricing
        elif zone_direction == Direction.BEARISH and bar_price >= h4_eq:
            score += 2   # selling in the expensive half → good pricing

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
    for i in range(start_idx, len(candles)):
        c = candles[i]
        if direction == Direction.BULLISH and c.low <= zone_high:
            return True
        if direction == Direction.BEARISH and c.high >= zone_low:
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
    if len(h4_candles) < lookback:
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


def compute_h4_equilibrium(h4_candles: List[Candle]) -> Tuple[float, float, float]:
    """
    Compute the premium / discount range from the active H4 window.

    Returns:
        (h4_high, h4_low, equilibrium_midpoint)
    """
    lookback = config.H4_EQUILIBRIUM_LOOKBACK
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
        created_at=datetime.now(tz=timezone.utc),
    )


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

    zone_tapped           = False
    retracement_start_idx = None

    for i in range(1, len(m5_candles) - 1):
        c = m5_candles[i]

        # ── Step 1: zone tap (with ATR buffer) ────────────────────
        # zone_buffer expands zone edges so a 1-3 pip institution
        # front-run doesn't disqualify an otherwise valid setup.
        if not zone_tapped:
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

            # Displacement quality gate (OR logic):
            # A valid displacement must show EITHER a volume spike OR a large body.
            # Requiring BOTH would double-filter and kill too many valid setups.
            volume_ok = avg_volume > 0 and (disp.volume / avg_volume) >= config.MIN_DISPLACEMENT_VOLUME_RATIO
            body_ok = False
            if i >= 3:
                prior_avg_body = sum(m5_candles[i - k].body_size for k in range(1, 4)) / 3.0
                body_ok = prior_avg_body > 0 and disp.body_size >= prior_avg_body * config.CANDLE_SIZE_RATIO

            if not volume_ok and not body_ok:
                if diag:
                    diag.record_rejection(symbol, "choch_weak_displacement")
                continue

            # ── Step 5: M5 FVG from displacement ──────────────────
            if i < 2:
                continue
            pre_disp = m5_candles[i - 2]
            entry    = pre_disp.high if pre_disp.high < disp.low else disp.open

            sl = zone.low - sl_buffer
            tp = liquidity_pool.high

        else:  # BEARISH
            # wave_low = lowest low of retracement BEFORE displacement candle
            wave_low = min(c2.low for c2 in wave)
            if c.close >= wave_low:
                continue

            disp = c

            # Displacement quality gate (OR logic — same as bullish)
            volume_ok = avg_volume > 0 and (disp.volume / avg_volume) >= config.MIN_DISPLACEMENT_VOLUME_RATIO
            body_ok = False
            if i >= 3:
                prior_avg_body = sum(m5_candles[i - k].body_size for k in range(1, 4)) / 3.0
                body_ok = prior_avg_body > 0 and disp.body_size >= prior_avg_body * config.CANDLE_SIZE_RATIO

            if not volume_ok and not body_ok:
                if diag:
                    diag.record_rejection(symbol, "choch_weak_displacement")
                continue

            if i < 2:
                continue
            pre_disp = m5_candles[i - 2]
            entry    = pre_disp.low if pre_disp.low > disp.high else disp.open

            sl = zone.high + sl_buffer
            tp = liquidity_pool.low

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
        h1_atr    = atr(candles)          # used to filter micro-FVGs
        obs       = find_order_blocks(candles, symbol, timeframe)
        fvgs      = find_fair_value_gaps(candles, symbol, timeframe, atr_value=h1_atr)
        pool      = find_liquidity_pool(candles, symbol, timeframe)
        all_zones = obs + fvgs
        logger.info("[%s] %d OBs, %d FVGs (ATR=%.5f, min_fvg=%.5f)  pool=H%.5f/L%.5f",
                    symbol, len(obs), len(fvgs), h1_atr,
                    h1_atr * config.FVG_MIN_ATR_RATIO,
                    pool.high if pool else 0, pool.low if pool else 0)
        return all_zones, pool