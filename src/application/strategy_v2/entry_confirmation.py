# src/application/strategy_v2/entry_confirmation.py
#
# Entry Confirmation — the M1 confirmation sequence gating an actual trade
# entry: liquidity sweep -> HTF (M15) POI touch -> discount/premium
# validation -> Absorption -> M1 CHoCH trigger (stateless) -> Entry Zone
# (M1 OB+FVG) -> SL (confirmation swing) -> TP priority fallback -> RR>=5.
#
# CORRECTED per docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md — this
# replaces an earlier version of this module that was built without the
# locked V4 spec in context and diverged from it in two severe ways (both
# fixed here):
#   - "Local structure confirmation" used to run a full, persistent
#     StructureEngine (trend/BOS state) on M1. Spec section 16 explicitly
#     forbids this ("no M1 trend state, no M1 BOS state, no M1 external
#     structure") -- M1 CHoCH must be a STATELESS trigger: identify the
#     most recent confirmed M1 swing and test a body close beyond it.
#   - Take-profit selection used to pick the nearer of 2 candidates.
#     Spec section 19 requires trying 3 candidates in PRIORITY ORDER (H1
#     External Swing -> Equal High/Low -> Opposite Asian Range), taking
#     the first that clears RR>=5.
#
# Pure composition over already-frozen/already-built primitives -- no
# structure/FVG/OB/liquidity/premium-discount logic reimplemented.
# swings.confirmed_swings_at_close (stateless, already frozen) is reused
# directly for the M1 CHoCH trigger -- NOT the StructureEngine class.
#
# DOCUMENTED IMPLEMENTATION DECISIONS (this module's own scope):
#
#   E1. Sweep confirmation: unchanged from the prior version -- a
#       LiquidityLevel with reaction == 'confirmed_reversal' (never
#       'breakout'), recent (within max_sweep_age of the entry candle).
#   E2. HTF POI touch: the current M1 candle's range intersects the M15
#       POI zone's [zone_low, zone_high] -- inclusive, mirrors order_
#       blocks.py's own "first touch" convention.
#   E3. Discount/Premium validation: delegates to premium_discount.
#       is_in_favorable_zone against the H1 pair -- unchanged, was
#       already correct (spec §10 is H1-only, matches).
#   E4. Absorption (spec §15): last 3 M1 candles strictly decreasing body
#       size (ties disqualify), average body < 50%xATR(10), no candle
#       closes beyond the HTF POI (the M15 zone from E2).
#   E5. M1 CHoCH trigger (spec §16, STATELESS): the most recently
#       confirmed M1 swing (via swings.confirmed_swings_at_close, called
#       once per M1 bar close by the caller and accumulated into a plain
#       list -- no StructureEngine instance anywhere on M1) of the kind
#       matching the trade direction; the current candle's body must
#       close beyond it. Also carries the displacement test (body
#       >=1.5xATR(10), spec §7) -- both conditions together are "M1
#       Confirmation" per §16.
#   E6. Entry Zone (spec §17): most recent M1 OB and/or M1 FVG matching
#       direction. If both exist and overlap (order_blocks.
#       ob_fvg_confluence, inclusive, already-locked semantics): Entry
#       Zone = the intersection, entry price = its 50% midpoint. If only
#       one exists, or they don't overlap: use "the deeper discount/
#       premium structure" -- interpreted here as whichever available
#       zone's own midpoint sits deeper in the favorable half of the H1
#       pair's range (lower Fib ratio for a bullish/discount setup,
#       higher for bearish/premium); that zone's own midpoint is the
#       entry price. Neither zone existing means no valid Entry Zone --
#       reject.
#   E6b. FLAGGED ASSUMPTION -- fill window: the spec (as recovered) does
#       not state how long a computed Entry Zone price may remain a valid
#       pending order (the original plan file mentioned "15-min max age"
#       for a different, earlier draft's numbering that this V4 document
#       superseded and does not repeat). This module computes the Entry
#       Zone price and the caller (the backtester/replay) is responsible
#       for the fill-window policy; a conservative, explicitly documented
#       default (bounded look-forward) is used at the call site, not
#       invented here.
#   E7. Stop Loss (spec §18): beyond the CONFIRMATION SWING (E5's swing,
#       not the POI zone edge) by the per-instrument buffer
#       (config_v2.SL_BUFFER_PIPS/_XAUUSD).
#   E8. Take Profit (spec §19): try, IN ORDER, the H1 External Swing
#       target, then the Equal High/Low target, then the Opposite Asian
#       Range target; take the FIRST that clears RR>=config_v2.MIN_RR;
#       reject only if none of the three do. A missing/unavailable
#       candidate (e.g. no equal-level target exists) is simply skipped,
#       not treated as a failure of that tier.

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple

from src.domain.entities import Candle, Direction, Timeframe
from src.domain.strategy_v2_entities import ExternalSwingPair, LiquidityLevel, SwingPoint, SwingType
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.atr import is_displacement, body_size
from src.application.strategy_v2.numerics import approx_gte, approx_lte
from src.application.strategy_v2.premium_discount import is_in_favorable_zone
from src.application.strategy_v2.order_blocks import ob_fvg_confluence


@dataclass
class EntrySignal:
    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    take_profit_source: str   # "htf_swing" | "equal_level" | "opposite_asia"
    rr: float
    poi_kind: str              # "fvg" | "ob"
    confirmation_swing: SwingPoint
    entry_time: datetime


def sweep_confirmed(level: LiquidityLevel, now: datetime, max_age: timedelta) -> bool:
    """E1 -- unchanged: a valid entry-triggering sweep."""
    if not level.swept or level.reaction != "confirmed_reversal" or level.swept_at is None:
        return False
    return (now - level.swept_at) <= max_age


def poi_touched(candle: Candle, zone_low: float, zone_high: float) -> bool:
    """E2 -- inclusive first-touch test. Callers use this to DETECT and
    timestamp a touch event (see zone_touch_confirmed below) -- it is not
    used as a same-bar gate inside evaluate_entry (see E2's revised
    docstring for why)."""
    return approx_lte(candle.low, zone_high) and approx_gte(candle.high, zone_low)


def zone_touch_confirmed(zone_touched_at: Optional[datetime], now: datetime, max_touch_age: timedelta) -> bool:
    """E2, CORRECTED (see docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md's
    real-data follow-up): the HTF POI touch and the M1 confirmation
    (Absorption + CHoCH trigger + displacement, spec S16) do NOT have to
    happen on the same candle. Absorption describes a quiet, consolidating
    sequence -- which is the OPPOSITE of the displacement/momentum implied
    by "arriving at and touching a zone" on that same bar. In real SMC
    price action, price touches a POI, consolidates for a few bars
    (Absorption), and only THEN does a later candle deliver the
    confirming break+displacement. Requiring same-bar simultaneity was
    confirmed on real data to make M1 confirmation fail on effectively
    every real touch (0 passes across thousands of real opportunities,
    even though the Absorption and displacement/CHoCH-trigger conditions
    each independently occur plenty of times in the data -- just never on
    the exact same bar as a fresh touch). This function instead checks
    the touch happened within a bounded recency window before `now`
    (mirrors sweep_confirmed's own max_sweep_age pattern).

    FLAGGED ASSUMPTION: the recovered spec text does not state an explicit
    window for how long a POI touch remains valid before requiring M1
    confirmation -- `max_touch_age` is a caller-supplied, explicit
    parameter (not a new hidden default), matching this module's existing
    discipline of surfacing such gaps rather than silently picking one."""
    if zone_touched_at is None:
        return False
    return timedelta(0) <= (now - zone_touched_at) <= max_touch_age


def poi_in_favorable_zone(pair: ExternalSwingPair, zone_low: float, zone_high: float) -> bool:
    """E3 -- delegates to premium_discount, no reimplementation."""
    return is_in_favorable_zone(pair, zone_low, zone_high)


def absorption_confirmed(
    direction: Direction, last_3_candles: Sequence[Candle], atr_value: Optional[float],
    htf_poi_low: float, htf_poi_high: float,
) -> bool:
    """E4, spec §15. `last_3_candles` must be exactly 3, chronological.

    CORRECTED (see docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md's real-
    data replay follow-up): "no candle closes beyond the HTF POI" means
    overshooting past the FAR side of the zone in the direction of travel
    -- NOT simply being outside the zone on the approach side, which is
    the normal, expected state for absorption candles (they occur BEFORE
    the actual touch, so they haven't reached the zone yet by
    construction). A BULLISH setup approaches a discount POI from ABOVE;
    "beyond" means closing below zone_low (undershooting past support
    without holding). A BEARISH setup approaches from BELOW; "beyond"
    means closing above zone_high. Testing both edges equally (the
    original implementation) made this condition fail on essentially
    every real approach sequence, since absorption candles are almost
    always still on the "not yet arrived" side of the zone -- confirmed
    on real data: 0/5497 real touch-and-favorable opportunities passed
    Absorption under the two-sided test."""
    if len(last_3_candles) != 3 or atr_value is None or atr_value <= 0:
        return False
    b1, b2, b3 = (body_size(c) for c in last_3_candles)
    if not (b1 > b2 > b3):   # strictly decreasing; ties disqualify
        return False
    avg_body = (b1 + b2 + b3) / 3.0
    if not (avg_body < 0.5 * atr_value):
        return False
    for c in last_3_candles:
        if direction == Direction.BULLISH:
            if c.close < htf_poi_low:
                return False
        else:
            if c.close > htf_poi_high:
                return False
    return True


def most_recent_swing(m1_swings: Sequence[SwingPoint], kind: SwingType) -> Optional[SwingPoint]:
    """Helper for E5 -- the most recently confirmed M1 swing of `kind` in a
    plain, caller-maintained list (built via repeated calls to swings.
    confirmed_swings_at_close -- NOT a StructureEngine)."""
    for s in reversed(m1_swings):
        if s.kind == kind:
            return s
    return None


def m1_choch_trigger(direction: Direction, m1_swings: Sequence[SwingPoint], candle: Candle) -> Optional[SwingPoint]:
    """E5, spec §16 -- STATELESS: the current candle's body must close
    beyond the most recently confirmed M1 swing of the matching kind (HIGH
    for bullish, LOW for bearish). Returns that swing (needed for SL, E7)
    if triggered, else None. No M1 trend/BOS state anywhere."""
    kind = SwingType.HIGH if direction == Direction.BULLISH else SwingType.LOW
    swing = most_recent_swing(m1_swings, kind)
    if swing is None:
        return None
    if direction == Direction.BULLISH:
        return swing if candle.close > swing.price else None
    return swing if candle.close < swing.price else None


def m1_confirmation(
    direction: Direction, m1_swings: Sequence[SwingPoint], candle: Candle, m1_atr_at_prev: Optional[float],
    last_3_candles: Sequence[Candle], htf_poi_low: float, htf_poi_high: float,
) -> Optional[SwingPoint]:
    """Spec §16 combined gate: Absorption + M1 CHoCH trigger + displacement
    (>=1.5xATR(10)). Returns the confirmation swing if ALL THREE hold,
    else None."""
    if not absorption_confirmed(direction, last_3_candles, m1_atr_at_prev, htf_poi_low, htf_poi_high):
        return None
    swing = m1_choch_trigger(direction, m1_swings, candle)
    if swing is None:
        return None
    if not is_displacement(candle, m1_atr_at_prev, config_v2.DISPLACEMENT_MULTIPLIER_M1):
        return None
    return swing


def _select_zone_near_swing(
    zones: Sequence[Tuple[float, float, datetime]], swing_price: float,
) -> Optional[Tuple[float, float]]:
    """Helper for compute_entry_zone -- among candidate (low, high,
    created_at) zones, picks the one geometrically CLOSEST (by midpoint
    distance) to the confirmation swing's price, ties broken by most
    recently created.

    CORRECTED after a real-data finding: an earlier version of this
    filter picked the most recently CREATED still-active zone,
    unconditionally. Since swing detection (a structural pattern) and
    OB/FVG detection (a displacement-gated event) are independent, there
    is no guaranteed timing relationship between them -- "most recently
    created" could be (and empirically was) a zone left over from a much
    earlier, unrelated move, sitting 50+ pips away from the current
    reversal (confirmed via real-data diagnosis on USDJPY), which produced
    a nonsensical inverted SL-vs-entry ordering on every real confirmed
    case (0/18 valid across the full real-data replay). A stricter
    "created at/after the swing" filter was tried next and turned out to
    eliminate essentially all candidates (zero temporal overlap in
    practice) -- confirming the real defect was PRICE distance, not
    timing. Distance-to-the-swing is the correct proxy for "the OB/FVG
    relevant to this specific reversal."""
    if not zones:
        return None
    lo, hi, _ = min(zones, key=lambda t: (abs(((t[0] + t[1]) / 2.0) - swing_price), -t[2].timestamp()))
    return lo, hi


def compute_entry_zone(
    direction: Direction, pair: ExternalSwingPair, confirmation_swing: SwingPoint,
    m1_obs: Sequence[Tuple[float, float, datetime]], m1_fvgs: Sequence[Tuple[float, float, datetime]],
) -> Optional[Tuple[float, float, float, str]]:
    """E6, spec §17. `m1_obs`/`m1_fvgs` are ALL active, direction-matching
    M1 OB/FVG candidates (low, high, created_at) -- this function selects
    among them (see _select_zone_near_swing), it is not pre-selected by
    the caller. Returns (entry_price, zone_low, zone_high, poi_kind) or
    None if no qualifying M1 OB or FVG is available."""
    m1_ob = _select_zone_near_swing(m1_obs, confirmation_swing.price)
    m1_fvg = _select_zone_near_swing(m1_fvgs, confirmation_swing.price)

    if m1_ob is not None and m1_fvg is not None:
        conf = ob_fvg_confluence(m1_ob[0], m1_ob[1], m1_fvg[0], m1_fvg[1])
        if conf is not None:
            lo, hi, _kind = conf
            return (lo + hi) / 2.0, lo, hi, "ob+fvg"
        # no overlap -- fall through to "deeper discount/premium structure"

    candidates = []
    if m1_ob is not None:
        candidates.append((m1_ob, "ob"))
    if m1_fvg is not None:
        candidates.append((m1_fvg, "fvg"))
    if not candidates:
        return None

    def depth_key(item):
        (lo, hi), _kind = item
        mid = (lo + hi) / 2.0
        range_lo, range_hi = min(pair.low, pair.high), max(pair.low, pair.high)
        if range_hi == range_lo:
            return 0.0
        ratio = (mid - range_lo) / (range_hi - range_lo)
        # deeper discount (bullish) = LOWER ratio is "deeper"; deeper premium
        # (bearish) = HIGHER ratio is "deeper" -- negate for bullish so max()
        # picks the deepest in both cases uniformly.
        return -ratio if direction == Direction.BULLISH else ratio

    (lo, hi), kind = max(candidates, key=depth_key)
    return (lo + hi) / 2.0, lo, hi, kind


def compute_stop_loss(direction: Direction, confirmation_swing: SwingPoint, symbol: str) -> float:
    """E7, spec §18 -- beyond the CONFIRMATION SWING, not the POI zone."""
    buffer_pips = config_v2.SL_BUFFER_PIPS_XAUUSD if symbol == "XAUUSD" else config_v2.SL_BUFFER_PIPS
    buffer = buffer_pips * config_v2.pip_size(symbol)
    if direction == Direction.BULLISH:
        return confirmation_swing.price - buffer
    return confirmation_swing.price + buffer


def compute_rr(entry_price: float, stop_loss: float, take_profit: float) -> float:
    risk = abs(entry_price - stop_loss)
    if risk == 0:
        return 0.0
    reward = abs(take_profit - entry_price)
    return reward / risk


def select_take_profit(
    direction: Direction, entry_price: float, stop_loss: float,
    htf_swing_target: Optional[float], equal_level_target: Optional[float], opposite_asia_target: Optional[float],
) -> Optional[Tuple[float, float, str]]:
    """E8, spec §19 -- priority-ordered fallback: H1 External Swing ->
    Equal High/Low -> Opposite Asian Range, first to clear RR>=MIN_RR.
    Returns (take_profit, rr, source_label) or None if nothing clears."""
    for target, label in (
        (htf_swing_target, "htf_swing"),
        (equal_level_target, "equal_level"),
        (opposite_asia_target, "opposite_asia"),
    ):
        if target is None:
            continue
        rr = compute_rr(entry_price, stop_loss, target)
        if rr >= config_v2.MIN_RR:
            return target, rr, label
    return None


def evaluate_entry(
    symbol: str,
    direction: Direction,
    pair: ExternalSwingPair,
    sweep_level: LiquidityLevel,
    poi_kind: str,
    zone_low: float,
    zone_high: float,
    zone_touched_at: Optional[datetime],
    entry_candle: Candle,
    m1_swings: Sequence[SwingPoint],
    last_3_m1_candles: Sequence[Candle],
    m1_atr_at_prev: Optional[float],
    m1_obs: Sequence[Tuple[float, float, datetime]],
    m1_fvgs: Sequence[Tuple[float, float, datetime]],
    htf_swing_target: Optional[float],
    equal_level_target: Optional[float],
    opposite_asia_target: Optional[float],
    max_sweep_age: timedelta = timedelta(hours=6),
    max_touch_age: timedelta = timedelta(hours=6),
) -> Optional[EntrySignal]:
    """Orchestrates E1-E8 in sequence. Returns None at the first failing
    gate; a fully-qualified EntrySignal only if every gate (including
    RR >= config_v2.MIN_RR against whichever TP tier cleared it) passes.

    `zone_touched_at`: the most recent time this specific zone was
    touched (via poi_touched(), tracked by the caller), or None if never
    touched. See zone_touch_confirmed()'s docstring for why this is
    decoupled from `entry_candle` rather than re-testing poi_touched on
    entry_candle itself."""
    if not sweep_confirmed(sweep_level, entry_candle.time, max_sweep_age):
        return None
    if not zone_touch_confirmed(zone_touched_at, entry_candle.time, max_touch_age):
        return None
    if not poi_in_favorable_zone(pair, zone_low, zone_high):
        return None

    confirmation_swing = m1_confirmation(
        direction, m1_swings, entry_candle, m1_atr_at_prev, last_3_m1_candles, zone_low, zone_high,
    )
    if confirmation_swing is None:
        return None

    entry_zone = compute_entry_zone(direction, pair, confirmation_swing, m1_obs, m1_fvgs)
    if entry_zone is None:
        return None
    entry_price, entry_zone_low, entry_zone_high, entry_zone_kind = entry_zone

    stop_loss = compute_stop_loss(direction, confirmation_swing, symbol)

    # Safety gate: the Entry Zone (M1 OB/FVG) and the confirmation swing are
    # independent anchors (spec keeps them as separate concepts, §17 vs
    # §18); in real data they're naturally consistent (the zone sits behind
    # the swing that just broke), but nothing structurally prevents an
    # inverted SL-vs-entry ordering in an unusual configuration. A trade
    # with SL on the wrong side of entry is definitionally invalid risk,
    # not a valid signal -- reject rather than produce a nonsensical trade.
    if direction == Direction.BULLISH and stop_loss >= entry_price:
        return None
    if direction == Direction.BEARISH and stop_loss <= entry_price:
        return None

    tp_result = select_take_profit(direction, entry_price, stop_loss, htf_swing_target, equal_level_target, opposite_asia_target)
    if tp_result is None:
        return None
    take_profit, rr, tp_source = tp_result

    return EntrySignal(
        symbol=symbol, direction=direction, entry_price=entry_price, stop_loss=stop_loss,
        take_profit=take_profit, take_profit_source=tp_source, rr=rr, poi_kind=poi_kind,
        confirmation_swing=confirmation_swing, entry_time=entry_candle.time,
    )
