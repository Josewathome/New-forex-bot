# src/application/strategy_v2/structure.py
#
# External Swing Pair / BOS / CHoCH / Trend state machine — V4 spec §3, §5,
# Contradictions 1-2, and the "Startup / Post-CHoCH State" clarification.
#
# One StructureEngine instance per symbol. Fed H1 candle closes one at a
# time via process_new_candle_close(); the same call path is used live and
# in the backtester so both see identical logic (signal-parity requirement).
#
# Same-candle evaluation priority (execution-layer decision #9): on every
# new H1 close, newly-confirmed swings are logged first, then CHoCH is
# tested before BOS.
#
# DOCUMENTED IMPLEMENTATION DECISIONS (not explicit in the spec text,
# needed to make the rules mechanically operable — flagged for approval
# rather than silently assumed):
#
#   D1. "Current External Swing High/Low" (the BOS test level) is tracked
#       as the most recently confirmed swing of each type, continuously
#       overwritten as new swings confirm. Once a level fires a BOS it is
#       retired (set to None) so the same already-broken level cannot
#       re-fire BOS on every subsequent bar close that happens to remain
#       beyond it — BOS is edge-triggered, not a continuous condition.
#
#   D2. Deriving "the low that generated the bullish BOS": walked backward
#       through the confirmed-swing log for the most recent SwingType.LOW
#       entry whose candle_time is strictly before the BOS-breaking swing
#       high's own candle_time — NOT simply "the most recent swing low
#       known right now" (which could have confirmed AFTER the high, and
#       per the user's own example would be the wrong one to pair).
#
#   D3. If no qualifying opposite-type swing exists yet in the log when a
#       pending level is broken (e.g. immediately after a CHoCH reset, or
#       at the very start of history), the break is NOT treated as a
#       trend-establishing BOS — the bot stays in WAIT. The pending level
#       stays live (not retired) so the very next close beyond it is
#       re-tested once a qualifying swing exists.
#
#   D4 (revised per user instruction). On CHoCH, historical confirmed
#       swings are NEVER deleted — swing_log is kept intact for recovery,
#       auditing, and analytics. Only active structure state resets:
#       trend -> NEUTRAL, active_pair -> None. The two BOS-test pointers
#       (pending_high/pending_low) also reset to None here — this is a
#       sub-decision flagged for approval, not explicit in the user's
#       instruction: since the WAIT-state requirement ("no trading until
#       a valid pair exists... a new trend is established") describes an
#       ACTIVE-testing requirement, not a historical-data requirement, a
#       pre-CHoCH swing that's still sitting in the (now-preserved) log
#       should not be allowed to immediately re-serve as a live bootstrap
#       candidate for the very next BOS test — a fresh swing of each type
#       must reconfirm after the CHoCH before a new trend can establish.
#       POI/pending-order/re-entry-state clearing lives at the POI and
#       execution layers (not yet built), not in this module.
#
#   D5. While a trend is already active (BULLISH/BEARISH), the ONLY event
#       that can end or flip it is CHoCH — testing close against the
#       active pair's OWN low (bullish) / high (bearish). An independently
#       tracked opposite-type pending swing is never tested for a direct
#       trend flip while a trend is active, since Contradiction 2 defines
#       "the current External Swing Low" during an active bullish trend as
#       specifically the active pair's own low — nothing else. A same-
#       direction CONTINUATION (a fresh, later-confirmed swing high beyond
#       the pair's own high, subsequently broken by an even higher close)
#       is a new BOS event that extends the pair: high -> the newly broken
#       level, low -> re-derived via D2 relative to that new high.
#
#   D6 (locked). A continuation BOS in an already-active trend rolls the
#       active External Swing Pair forward to the newest pair (recommended
#       and confirmed) rather than keeping the original pair frozen.
#
# STATUS: LOCKED (2026-07-19). D2's "most recent confirmed opposite swing"
# rule is final. Two alternative selection rules were researched on the
# real 13-month H1 dataset (XAUUSD/EURUSD/GBPUSD/USDJPY) and REJECTED —
# recorded here so this isn't re-litigated without new evidence:
#
#   REJECTED 1 — largest-displacement selection. Replacing "most recent"
#     with "largest displacement in the whole candidate pool" collapsed
#     BOS/CHoCH frequency 3-10x and inflated average trend duration to
#     absurd levels (EURUSD: 1,322 bars ~ 55 days for one "trend"; XAUUSD
#     avg Fib width 681 -> 5,865 pips). Locks onto rare historical extremes
#     and stops resetting. Disqualified outright.
#
#   REJECTED 2 — equal-highs/lows bounded tie-break. A retrospective
#     significance study (6-component score: BOS-generation, CHoCH-
#     reference, liquidity sweep, displacement, equal-highs/lows,
#     +-20-bar prominence) found D2 matches the best-scoring candidate in
#     a bounded 15-swing window 46.4% of the time. Sampling 100 real BOS
#     events, 29 showed a "better" alternative by that score -- but 3 of
#     the 6 components are look-ahead (BOS-generation, CHoCH-reference,
#     +-20-bar prominence require future bars and can't inform a live
#     rule). Stripping those out: only 12/29 genuinely differ using
#     real-time-only signals, 4/29 actually favor D2 once look-ahead is
#     removed, and 13/29 are ties with no observable signal either way.
#     Of the 12 genuine cases, equal-highs/lows was the dominant pattern
#     (9/12). Built the smallest bounded tie-break (prefer the most recent
#     of the last 3 same-kind candidates with an equal-level nearby, else
#     D2 default) and replayed it across the full dataset: BOS/CHoCH
#     counts stayed stable (no blow-up), but generating-member prominence
#     -- the metric the whole exercise was meant to improve -- got
#     slightly WORSE, not better (+-10 bars: 41.6% -> 39.9%; +-20: 20.5%
#     -> 17.6%; +-50: 6.5% -> 4.2%). Equal-highs/lows likely marks a
#     retested, congested price zone rather than a decisively-held
#     extreme; the retrospective pattern did not survive an honest,
#     non-look-ahead replay. Rejected.
#
# No further swing-selection research is planned. Effort moves to FVG,
# Order Block, liquidity sweep, Premium/Discount, and entry-confirmation,
# where the actual trading edge is more likely to emerge.

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence

from src.domain.entities import Candle, Timeframe
from src.domain.strategy_v2_entities import ExternalSwingPair, SwingPoint, SwingType, TrendState
from src.application.strategy_v2.swings import confirmed_swings_at_close


@dataclass
class StructureEvent:
    new_swings: List[SwingPoint] = field(default_factory=list)
    bos_fired: Optional[TrendState] = None      # BULLISH or BEARISH if a BOS fired this bar, else None
    choch_fired: bool = False                   # True if a CHoCH fired this bar
    invalidated_trend: Optional[TrendState] = None  # trend that WAS active, if choch_fired
    trend: TrendState = TrendState.NEUTRAL       # trend AFTER this bar's processing
    active_pair: Optional[ExternalSwingPair] = None  # pair AFTER this bar's processing


class StructureEngine:
    def __init__(self, symbol: str, timeframe: Timeframe = Timeframe.H1):
        self.symbol = symbol
        self.timeframe = timeframe
        self.trend: TrendState = TrendState.NEUTRAL
        self.swing_log: List[SwingPoint] = []
        self.pending_high: Optional[SwingPoint] = None
        self.pending_low: Optional[SwingPoint] = None
        self.active_pair: Optional[ExternalSwingPair] = None

    def _find_generating_swing(self, kind: SwingType, before: SwingPoint) -> Optional[SwingPoint]:
        """D2: most recent confirmed swing of `kind` with candle_time strictly
        before `before`'s candle_time. swing_log is chronological, so scan
        from the end backward and return the first match."""
        for s in reversed(self.swing_log):
            if s.kind == kind and s.candle_time < before.candle_time:
                return s
        return None

    def process_new_candle_close(self, candles: Sequence[Candle], i: int) -> StructureEvent:
        """Call once per new H1 candle close, with i = index of that candle
        in `candles` (i.e. i = len(candles) - 1 for a live feed)."""
        event = StructureEvent()
        close = candles[i].close

        # Step 1 — log any swing(s) confirmed as of this close.
        new_swings = confirmed_swings_at_close(self.symbol, self.timeframe, candles, i)
        for s in new_swings:
            self.swing_log.append(s)
            if s.kind == SwingType.HIGH:
                self.pending_high = s
            else:
                self.pending_low = s
        event.new_swings = new_swings

        # Step 2/3 — branch entirely on the CURRENT trend (D5): CHoCH can only
        # be tested against an active pair's own low/high; a fresh trend can
        # only be established from NEUTRAL via the rolling pending levels.
        if self.trend == TrendState.NEUTRAL:
            self._try_establish_trend(candles, i, close, event)
        elif self.trend == TrendState.BULLISH:
            if close < self.active_pair.low:
                self._fire_choch(TrendState.BULLISH, event)
            else:
                self._try_continue_bullish(candles, i, close, event)
        elif self.trend == TrendState.BEARISH:
            if close > self.active_pair.high:
                self._fire_choch(TrendState.BEARISH, event)
            else:
                self._try_continue_bearish(candles, i, close, event)

        event.trend = self.trend
        event.active_pair = self.active_pair
        return event

    def _fire_choch(self, invalidated: TrendState, event: StructureEvent) -> None:
        event.choch_fired = True
        event.invalidated_trend = invalidated
        # D4 (revised) — swing_log is preserved intact for recovery/audit/
        # analytics. Only active structure state resets: trend, active pair,
        # and the two BOS-test bootstrap pointers (see D4 docstring above).
        self.trend = TrendState.NEUTRAL
        self.active_pair = None
        self.pending_high = None
        self.pending_low = None

    def _try_establish_trend(self, candles, i, close: float, event: StructureEvent) -> None:
        if self.pending_high is not None and close > self.pending_high.price:
            generating_low = self._find_generating_swing(SwingType.LOW, before=self.pending_high)
            if generating_low is not None:
                broken_high = self.pending_high
                self.pending_high = None  # D1 — retire so it can't re-fire
                self.trend = TrendState.BULLISH
                self.active_pair = ExternalSwingPair(
                    symbol=self.symbol, trend=TrendState.BULLISH,
                    low=generating_low.price, high=broken_high.price,
                    low_swing=generating_low, high_swing=broken_high,
                    bos_time=candles[i].time,
                )
                event.bos_fired = TrendState.BULLISH
            # D3 — else: no qualifying opposite swing yet, remain in WAIT.
        elif self.pending_low is not None and close < self.pending_low.price:
            generating_high = self._find_generating_swing(SwingType.HIGH, before=self.pending_low)
            if generating_high is not None:
                broken_low = self.pending_low
                self.pending_low = None
                self.trend = TrendState.BEARISH
                self.active_pair = ExternalSwingPair(
                    symbol=self.symbol, trend=TrendState.BEARISH,
                    low=broken_low.price, high=generating_high.price,
                    low_swing=broken_low, high_swing=generating_high,
                    bos_time=candles[i].time,
                )
                event.bos_fired = TrendState.BEARISH
            # D3 — else: remain in WAIT.

    def _try_continue_bullish(self, candles, i, close: float, event: StructureEvent) -> None:
        # D5 — continuation: a later-confirmed swing high beyond the pair's
        # own high, now itself broken by this close.
        ph = self.pending_high
        if ph is not None and ph.price > self.active_pair.high and close > ph.price:
            generating_low = self._find_generating_swing(SwingType.LOW, before=ph) or self.active_pair.low_swing
            self.pending_high = None
            self.active_pair = ExternalSwingPair(
                symbol=self.symbol, trend=TrendState.BULLISH,
                low=generating_low.price, high=ph.price,
                low_swing=generating_low, high_swing=ph,
                bos_time=candles[i].time,
            )
            event.bos_fired = TrendState.BULLISH

    def _try_continue_bearish(self, candles, i, close: float, event: StructureEvent) -> None:
        pl = self.pending_low
        if pl is not None and pl.price < self.active_pair.low and close < pl.price:
            generating_high = self._find_generating_swing(SwingType.HIGH, before=pl) or self.active_pair.high_swing
            self.pending_low = None
            self.active_pair = ExternalSwingPair(
                symbol=self.symbol, trend=TrendState.BEARISH,
                low=pl.price, high=generating_high.price,
                low_swing=pl, high_swing=generating_high,
                bos_time=candles[i].time,
            )
            event.bos_fired = TrendState.BEARISH
