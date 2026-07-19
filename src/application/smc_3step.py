# src/application/smc_3step.py
#
# Python port of the user-supplied SmartMoney_3Step_EA.mq5 (H1 EMA+swing
# trend -> M15 OB/FVG pullback zone (bounded max-age) -> M5 CHoCH entry,
# fixed RR). This is a DIFFERENT, self-contained strategy from
# strategy_v2 (the V4 spec build) -- ported faithfully from the .mq5
# source line-by-line, not reinterpreted. Every function here has a
# named MQL5 counterpart; deviations from the literal source are called
# out explicitly in that function's docstring.
#
# Ported, not redesigned: this module intentionally reuses config_v2's
# pip/point table (src/application/strategy_v2/config_v2.pip_size/
# point_size) since it's identical to this account's SYMBOL_POINT values
# already verified against real MT5 symbol_info -- not a strategy-logic
# borrowing, purely a units lookup.

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional, Sequence, Tuple

from src.domain.entities import Candle
from src.application.strategy_v2 import config_v2


# ============================================================
# EMA (GetTrend's EMA leg — MQL5 iMA(..., MODE_EMA, PRICE_CLOSE))
# ============================================================

def ema_series(closes: Sequence[float], period: int) -> List[Optional[float]]:
    """Standard EMA: seeded with the SMA of the first `period` closes,
    then EMA[i] = Close[i]*k + EMA[i-1]*(1-k), k = 2/(period+1). This
    matches MT5's iMA(MODE_EMA) convention (SMA seed, then recursive).
    None until the seed bar."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = closes[i] * k + prev * (1 - k)
        out[i] = prev
    return out


# ============================================================
# Fractal swings (GetLastSwings) — symmetric strict wing-bar fractal,
# NOT the same asymmetric 3-bar-left-strict/right-nonstrict definition
# used in strategy_v2's swings.py. This EA uses its own, simpler,
# symmetric definition (wing bars strictly greater/less on BOTH sides).
# ============================================================

class SwingKind(Enum):
    HIGH = "high"
    LOW = "low"


@dataclass
class Swing:
    kind: SwingKind
    price: float
    index: int   # index into the candle list passed in (older->newer order)


def find_fractal_swings(
    candles: Sequence[Candle], wing: int, lookback: int, up_to_index: int,
) -> Tuple[List[Swing], List[Swing]]:
    """Port of GetLastSwings. `candles` is in NORMAL chronological order
    (oldest first); `up_to_index` is the index of the last CLOSED candle
    to consider (mirrors MQL5's CopyHigh/Low(shift=1) -- the currently-
    forming bar is never included). Scans backward from `up_to_index`,
    collecting the 2 most recent fractal highs and 2 most recent fractal
    lows (symmetric: strictly greater/less than all `wing` neighbors on
    BOTH sides), stopping once both are found or `lookback` is
    exhausted -- exactly mirroring the MQL5 early-break loop. Returns
    (highs, lows) each in OLDEST-FIRST order (index 0 = older, index 1 =
    newer), matching the EA's own re-ordering before use.

    Returns empty lists if fewer than 2 of either kind are found (MQL5
    returns false from GetLastSwings in that case) -- callers must check
    `len(highs) == 2 and len(lows) == 2` before using them, same as the
    EA's own `if(hCount < 2 || lCount < 2) return false`.
    """
    highs_found: List[Swing] = []
    lows_found: List[Swing] = []
    scanned = 0
    i = up_to_index - wing
    while i >= 0 and i - wing >= 0 and scanned < lookback:
        if len(highs_found) >= 2 and len(lows_found) >= 2:
            break
        is_high = True
        is_low = True
        for w in range(1, wing + 1):
            if i + w > up_to_index:
                is_high = False
                is_low = False
                break
            if candles[i].high <= candles[i - w].high or candles[i].high <= candles[i + w].high:
                is_high = False
            if candles[i].low >= candles[i - w].low or candles[i].low >= candles[i + w].low:
                is_low = False
        if is_high and len(highs_found) < 2:
            highs_found.append(Swing(SwingKind.HIGH, candles[i].high, i))
        if is_low and len(lows_found) < 2:
            lows_found.append(Swing(SwingKind.LOW, candles[i].low, i))
        i -= 1
        scanned += 1

    if len(highs_found) < 2 or len(lows_found) < 2:
        return [], []
    # highs_found/lows_found are newest-first (scan moves backward in time);
    # reverse to oldest-first, matching the EA's lastHighs[0]=highsFound[1] swap.
    return list(reversed(highs_found)), list(reversed(lows_found))


# ============================================================
# STEP 1 — Trend (GetTrend): EMA slope AND swing structure must agree.
# ============================================================

def get_trend(
    h1_candles: Sequence[Candle], up_to_index: int,
    ema_values: Sequence[Optional[float]], slope_bars: int,
    swing_lookback: int, wing: int,
) -> int:
    """Returns 1 (bullish), -1 (bearish), or 0 (no clear trend). `ema_values`
    must be pre-computed via ema_series over the same candle list (passed
    in rather than recomputed here so the backtester can cache it once per
    symbol instead of recomputing an O(n) EMA at every bar)."""
    if up_to_index >= len(ema_values) or up_to_index - slope_bars < 0:
        return 0
    ema_now = ema_values[up_to_index]
    ema_past = ema_values[up_to_index - slope_bars]
    if ema_now is None or ema_past is None:
        return 0
    ema_up = ema_now > ema_past
    ema_down = ema_now < ema_past

    highs, lows = find_fractal_swings(h1_candles, wing, swing_lookback, up_to_index)
    if not highs or not lows:
        return 0
    structure_up = highs[1].price > highs[0].price and lows[1].price > lows[0].price
    structure_down = highs[1].price < highs[0].price and lows[1].price < lows[0].price

    if ema_up and structure_up:
        return 1
    if ema_down and structure_down:
        return -1
    return 0


# ============================================================
# STEP 2 — Pullback zone: Order Block, then Fair Value Gap.
# ============================================================

@dataclass
class Zone:
    top: float
    bottom: float
    form_index: int   # index into the M15 candle list (oldest-first)
    form_time: datetime
    direction: int   # 1 = bullish (buy zone), -1 = bearish (sell zone)
    is_ob: bool


def is_zone_invalidated(zone: Zone, m15_candles: Sequence[Candle], up_to_index: int) -> bool:
    """Port of IsZoneInvalidated: any CLOSE strictly beyond the zone,
    at any candle AFTER the zone's formation candle up to `up_to_index`
    inclusive, invalidates it. Matches the EA's simple "any close through"
    rule -- deliberately simpler than strategy_v2's OB/FVG mitigation
    (wick-touch mitigation + opposite-side-close invalidation as two
    separate states); this EA has only one, coarser invalidation event."""
    for i in range(zone.form_index + 1, up_to_index + 1):
        c = m15_candles[i].close
        if zone.direction == 1 and c < zone.bottom:
            return True
        if zone.direction == -1 and c > zone.top:
            return True
    return False


def find_order_block(
    m15_candles: Sequence[Candle], up_to_index: int, trend: int,
    lookback: int, impulse_factor: float, current_price: float,
) -> Optional[Zone]:
    """Port of FindOrderBlock. Scans from the most recently CLOSED M15
    candle backward; `i` is the candidate OB (base) candle, `i+1` (older)
    is... note the MQL5 series-indexing: r[i] is the base candle, r[i-1]
    (MORE RECENT, since series index 0=newest) is the impulse candle
    immediately after it. In normal chronological (oldest-first) indexing
    used here, that means: base = candles[idx], impulse = candles[idx+1].
    Returns the FIRST (most recent) qualifying, non-invalidated zone
    whose [bottom, top] band CURRENTLY CONTAINS `current_price` -- this
    price-inside-zone requirement is load-bearing (see module docstring
    in the audit doc this port supports): it structurally guarantees the
    selected zone is geometrically relevant to current price, unlike
    strategy_v2's unbounded "most recent" selection."""
    for idx in range(up_to_index - 1, max(up_to_index - lookback, 1) - 1, -1):
        base = m15_candles[idx]
        impulse = m15_candles[idx + 1]
        base_body = abs(base.close - base.open)
        if base_body <= 0:
            continue
        impulse_body = abs(impulse.close - impulse.open)

        if trend == 1 and base.close < base.open:   # bearish base candle
            impulse_up = (impulse.close > impulse.open and impulse.close > base.high
                          and impulse_body >= impulse_factor * base_body)
            if impulse_up:
                cand = Zone(top=base.high, bottom=base.low, form_index=idx,
                            form_time=base.time, direction=1, is_ob=True)
                if is_zone_invalidated(cand, m15_candles, up_to_index):
                    continue
                if cand.bottom <= current_price <= cand.top:
                    return cand
        elif trend == -1 and base.close > base.open:   # bullish base candle
            impulse_down = (impulse.close < impulse.open and impulse.close < base.low
                            and impulse_body >= impulse_factor * base_body)
            if impulse_down:
                cand = Zone(top=base.high, bottom=base.low, form_index=idx,
                            form_time=base.time, direction=-1, is_ob=True)
                if is_zone_invalidated(cand, m15_candles, up_to_index):
                    continue
                if cand.bottom <= current_price <= cand.top:
                    return cand
    return None


def find_fair_value_gap(
    m15_candles: Sequence[Candle], up_to_index: int, trend: int,
    lookback: int, current_price: float,
) -> Optional[Zone]:
    """Port of FindFairValueGap: 3-candle gap (a=oldest, b=middle,
    c=newest of the triplet), same price-inside-zone requirement as
    find_order_block.

    INDEX-DIRECTION NOTE: the MQL5 source scans series-indexed arrays
    (index 0 = most recent, larger index = further into the past), where
    the newest of the triplet (`c`) sits at the smallest index and the
    oldest (`a`) at `c+2`. This module uses normal forward-chronological
    indexing (larger index = more recent), so the equivalent relationship
    is `a = c - 2`, `b = c - 1` -- not `c + 2`/`c + 1`. An earlier draft of
    this port copied the MQL5 offsets unchanged, which silently reversed
    which candle was "oldest" and produced a mid-candle several bars in
    the WRONG temporal direction from `c`; caught by real-fixture unit
    testing (a hand-built bullish FVG was not found), not by inspection.
    """
    start = up_to_index
    end = max(up_to_index - lookback + 1, 2)
    for c_idx in range(start, end - 1, -1):
        if c_idx - 2 < 0:
            break
        a_idx, b_idx = c_idx - 2, c_idx - 1
        a, b, c = m15_candles[a_idx], m15_candles[b_idx], m15_candles[c_idx]
        mid_bullish = b.close > b.open
        mid_bearish = b.close < b.open

        if trend == 1 and mid_bullish and c.low > a.high:
            cand = Zone(top=c.low, bottom=a.high, form_index=c_idx,
                        form_time=c.time, direction=1, is_ob=False)
            if is_zone_invalidated(cand, m15_candles, up_to_index):
                continue
            if cand.bottom <= current_price <= cand.top:
                return cand
        elif trend == -1 and mid_bearish and c.high < a.low:
            cand = Zone(top=a.low, bottom=c.high, form_index=c_idx,
                        form_time=c.time, direction=-1, is_ob=False)
            if is_zone_invalidated(cand, m15_candles, up_to_index):
                continue
            if cand.bottom <= current_price <= cand.top:
                return cand
    return None


def find_pullback_zone(
    m15_candles: Sequence[Candle], up_to_index: int, trend: int,
    lookback: int, impulse_factor: float, current_price: float,
) -> Optional[Zone]:
    """Port of FindPullbackZone: OB tried first, then FVG."""
    z = find_order_block(m15_candles, up_to_index, trend, lookback, impulse_factor, current_price)
    if z is not None:
        return z
    return find_fair_value_gap(m15_candles, up_to_index, trend, lookback, current_price)


# ============================================================
# STEP 3 — CHoCH on M5 (CheckCHoCH)
# ============================================================

def check_choch(
    m5_candles: Sequence[Candle], up_to_index: int, direction: int,
    lookback: int, wing: int,
) -> Optional[float]:
    """Port of CheckCHoCH. Returns the broken swing level if the last
    closed M5 candle's CLOSE breaks it in `direction`'s favor, else None."""
    highs, lows = find_fractal_swings(m5_candles, wing, lookback, up_to_index)
    if not highs or not lows:
        return None
    last_close = m5_candles[up_to_index].close
    if direction == 1:
        level = highs[1].price
        return level if last_close > level else None
    else:
        level = lows[1].price
        return level if last_close < level else None


# ============================================================
# Position sizing (CalculateLotSize)
# ============================================================

def calculate_lot_size(
    balance: float, risk_percent: float, sl_distance: float,
    tick_value: float, tick_size: float,
    min_lot: float, max_lot: float, lot_step: float,
) -> float:
    """Port of CalculateLotSize. Returns 0.0 if sl_distance/tick_size/
    loss-per-lot are non-positive (mirrors the EA's early `return 0`)."""
    if sl_distance <= 0 or tick_size <= 0:
        return 0.0
    risk_money = balance * risk_percent / 100.0
    value_per_unit = tick_value / tick_size
    loss_per_lot = sl_distance * value_per_unit
    if loss_per_lot <= 0:
        return 0.0
    lot = risk_money / loss_per_lot
    lot = (lot // lot_step) * lot_step if lot_step > 0 else lot
    if lot < min_lot:
        lot = min_lot
    if lot > max_lot:
        lot = max_lot
    return round(lot, 2)


# ============================================================
# STATE MACHINE (Process / OnTick) — this EA is deliberately STATEFUL
# across M5 bars (STATE_WAIT_SETUP <-> STATE_WAIT_CHOCH), unlike
# strategy_v2's per-bar independent re-evaluation. Ported as a class
# holding exactly the same globals (g_state, g_zone, g_zoneWaitBars).
# ============================================================

class EAState(Enum):
    WAIT_SETUP = "wait_setup"
    WAIT_CHOCH = "wait_choch"


@dataclass
class ThreeStepConfig:
    ema_period: int = 50
    ema_slope_bars: int = 5
    swing_lookback_h1: int = 80
    fractal_wing: int = 2
    zone_lookback_m15: int = 80
    impulse_factor: float = 1.3
    zone_max_age_bars: int = 60
    choch_lookback_m5: int = 40
    zone_timeout_m5_bars: int = 40
    reward_risk_ratio: float = 2.0
    sl_buffer_points: int = 50
    max_spread_points: int = 30
    risk_percent: float = 1.0


@dataclass
class ThreeStepSignal:
    direction: int
    entry_price: float
    stop_loss: float
    take_profit: float
    zone: Zone


class ThreeStepEA:
    """Per-symbol state machine, mirroring OnTick's Process() exactly.
    Call `on_m5_bar(...)` once per new, fully-closed M5 bar."""

    def __init__(self, cfg: ThreeStepConfig, symbol: str):
        self.cfg = cfg
        self.symbol = symbol
        self.state = EAState.WAIT_SETUP
        self.zone: Optional[Zone] = None
        self.zone_wait_bars = 0

    def on_m5_bar(
        self, h1_candles: Sequence[Candle], h1_idx: int, h1_ema: Sequence[Optional[float]],
        m15_candles: Sequence[Candle], m15_idx: int,
        m5_candles: Sequence[Candle], m5_idx: int,
        current_price_bid: float, current_price_ask: float,
    ) -> Optional[ThreeStepSignal]:
        cfg = self.cfg
        trend = get_trend(h1_candles, h1_idx, h1_ema, cfg.ema_slope_bars,
                           cfg.swing_lookback_h1, cfg.fractal_wing)
        if trend == 0:
            self.state = EAState.WAIT_SETUP
            return None

        if self.state == EAState.WAIT_SETUP:
            price = current_price_ask if trend == 1 else current_price_bid
            z = find_pullback_zone(m15_candles, m15_idx, trend, cfg.zone_lookback_m15,
                                    cfg.impulse_factor, price)
            if z is not None:
                age_bars = m15_idx - z.form_index
                if 0 <= age_bars <= cfg.zone_max_age_bars:
                    self.zone = z
                    self.state = EAState.WAIT_CHOCH
                    self.zone_wait_bars = 0
            return None

        # STATE_WAIT_CHOCH
        self.zone_wait_bars += 1
        if (trend != self.zone.direction
                or is_zone_invalidated(self.zone, m15_candles, m15_idx)
                or self.zone_wait_bars > cfg.zone_timeout_m5_bars):
            self.state = EAState.WAIT_SETUP
            self.zone = None
            return None

        level = check_choch(m5_candles, m5_idx, self.zone.direction,
                             cfg.choch_lookback_m5, cfg.fractal_wing)
        if level is not None:
            signal = build_signal(cfg, self.zone, self.symbol, current_price_bid, current_price_ask)
            self.state = EAState.WAIT_SETUP
            self.zone = None
            return signal
        return None


def build_signal(
    cfg: ThreeStepConfig, zone: Zone, symbol: str, bid: float, ask: float,
) -> ThreeStepSignal:
    """Port of the pricing half of ExecuteTrade (SL/TP/entry-price
    computation only -- lot sizing and order submission are the
    backtester's job, matching how strategy_v2 separates evaluate_entry
    from the backtester's fill simulation)."""
    point = config_v2.point_size(symbol)
    if zone.direction == 1:
        price = ask
        sl = zone.bottom - cfg.sl_buffer_points * point
        sl_dist = price - sl
        tp = price + cfg.reward_risk_ratio * sl_dist
    else:
        price = bid
        sl = zone.top + cfg.sl_buffer_points * point
        sl_dist = sl - price
        tp = price - cfg.reward_risk_ratio * sl_dist
    return ThreeStepSignal(direction=zone.direction, entry_price=price, stop_loss=sl,
                            take_profit=tp, zone=zone)
