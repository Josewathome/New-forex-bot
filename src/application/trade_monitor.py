"""
trade_monitor.py — Phase 3.5: The Open Trade Guardian  (v3 — Smart Conviction)

THE PROBLEM WITH HARD BREAKEVEN MOVES
───────────────────────────────────────────────────────────────────────────────
Moving SL to exactly entry price on a single signal chokes the trade.  In a
trending market, price routinely retraces 50-70% of the last impulse before
continuing.  A hard SL at entry turns a normal pullback into a loss of the
trade — and then price hits TP without you.

THE FIX: TRAIL TO SWING STRUCTURE, NOT TO ENTRY
───────────────────────────────────────────────────────────────────────────────
When the monitor wants to tighten the SL, it finds the most recent M5 swing
low (for a long) or swing high (for a short) that sits ABOVE the original SL.
That swing is where market structure says "if price breaks here, the move is
genuinely over."  A mere retrace won't reach it.  Only a real reversal will.

                 Entry: 1.1000    TP: 1.1060
                 Original SL: 1.0980  (20 pips below)
                 
    Price rises to 1.1025 (25 pips, > 2× SL → monitor fires)
    Last M5 swing low: 1.1008
    Trail SL to: 1.1006  (swing - ATR buffer)
    
    ✓  If price retraces to 1.1012 then continues → trade stays open
    ✓  If price breaks 1.1006 → genuine reversal → stop it there
    ✗  Old system: SL at 1.1000 → retrace to 1.1001 → stopped for 0

CONVICTION-BASED ACTIONS (no single-signal panic)
───────────────────────────────────────────────────────────────────────────────
  1 signal  of any type  → record only (no action unless TRAIL_CONVICTION=1)
  2+ unique signal types within CONFIRM_WINDOW_M5 M5 bars
                          → trail SL to most recent M5 swing low/high
  3+ unique signal types  → close trade early (full conviction)

  Rationale: a retrace will trigger MFE stall.  A genuine reversal will
  trigger BOTH ChoCh AND an opposing zone.  Waiting for confluence
  dramatically cuts false positives.

CONFIG KEYS (add to config.py)
───────────────────────────────────────────────────────────────────────────────
  MONITOR_ENABLED            True
  MONITOR_ACT_ON_SIGNALS     False   ← start here; flip to True after analysis
  MONITOR_TRAIL_CONVICTION   1       ← signals needed before trailing SL
  MONITOR_CLOSE_CONVICTION   3       ← signals needed before closing early
  MONITOR_CONFIRM_WINDOW_M5  3       ← M5 bars to look back for confluence
  MONITOR_TRAIL_ATR_BUFFER   0.5     ← ATR multiple below swing for trail SL
  MFE_STALL_BARS             6       ← M5 bars without new MFE = stall
  OPPOSING_ZONE_WARN_ATR     1.0     ← ATR multiples before zone = warn
  BREAKEVEN_RR_TRIGGER       2.0     ← profit multiple of SL before trailing
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import src.config as config
from src.domain.entities import Candle, Direction, Zone
from src.domain.value_objects import PipCalculator

logger = logging.getLogger(__name__)

_M5 = timedelta(minutes=5)


# ─────────────────────────────────────────────
#  MonitorEvent
# ─────────────────────────────────────────────

@dataclass
class MonitorEvent:
    """One warning that fired on an open trade."""
    trade_id:           str
    symbol:             str
    signal_time:        datetime
    signal_type:        str      # breakeven_trigger|opposing_zone|reverse_choch|mfe_stall
    timeframe_source:   str      # "M1" or "M5"
    price_at_signal:    float
    mfe_pips_at_signal: float
    pips_to_tp:         float
    detail:             str
    action_taken:       str      # none|trailed_sl|closed_early
    new_sl:             float = 0.0   # set when action_taken == "trailed_sl"

    # Filled after trade resolves (backtest only)
    trade_outcome: str  = ""
    was_correct:   bool = False


# ─────────────────────────────────────────────
#  TradeMonitor
# ─────────────────────────────────────────────

class TradeMonitor:
    """
    Watches one open trade bar-by-bar and fires early-warning signals.

    Dual-timeframe:
      M1 bar  → MFE tracking, breakeven trigger, opposing zone approach
      M5 bar  → reverse ChoCh, MFE stall (only when new_m5_bar=True)

    Actions respect a conviction threshold:
      Low conviction  → trail SL to swing structure (NOT hard entry)
      High conviction → close trade early
    """

    def __init__(
        self, pip_calc: PipCalculator, atr_price: float, symbol: str,
    ) -> None:
        self._pip = pip_calc
        self._atr = atr_price
        self._sym = symbol

    # ── Pre-entry: scan opposing zones ───────────────────────────

    def scan_opposing_zones(
        self,
        direction:   Direction,
        entry:       float,
        take_profit: float,
        zones:       List[Zone],
    ) -> List[Zone]:
        """
        Find all opposing-direction H1 zones between entry and TP.
        Call once at entry; cache and pass to every update_dual() call.
        """
        opp   = Direction.BEARISH if direction == Direction.BULLISH else Direction.BULLISH
        found = []
        for z in zones:
            if z.direction != opp or not z.is_active:
                continue
            if direction == Direction.BULLISH:
                if z.low > entry and z.low < take_profit:
                    found.append(z)
            else:
                if z.high < entry and z.high > take_profit:
                    found.append(z)

        found.sort(key=lambda z: z.low if direction == Direction.BULLISH else -z.high)
        if found:
            logger.info("MONITOR | %s | %d opposing zone(s): %s",
                        self._sym, len(found),
                        " | ".join(f"{z.zone_type.value}@{z.low:.5f}" for z in found))
        return found

    # ── Primary dual-TF update ────────────────────────────────────

    def update_dual(
        self,
        trade_id:           str,
        direction:          Direction,
        entry:              float,
        current_sl:         float,      # current virtual SL (may already be trailed)
        original_sl:        float,      # the SL at trade entry — never changes
        take_profit:        float,
        m1_bar:             Candle,
        m5_window:          List[Candle],  # rolling last ≤12 closed M5 candles
        opposing_zones:     List[Zone],
        mfe_price:          float,
        m1_bars_since_mfe:  int,
        m5_bars_since_mfe:  int,
        new_m5_bar:         bool,
        recent_signals:     List[Tuple[str, datetime]],  # (type, time) window
    ) -> Tuple[List[MonitorEvent], str, float, bool]:
        """
        Run all four checks on the current M1 tick.

        Returns:
            events       — MonitorEvent list (may be empty)
            action       — "none" | "trail_sl" | "close_now"
            new_sl_price — the price to set if action == "trail_sl"
            should_close — True if monitor wants to close early
        """
        events:     List[MonitorEvent] = []
        action      = "none"
        new_sl_val  = current_sl
        should_close = False

        price    = m1_bar.close
        mfe_pips = self._pip.price_to_pips(abs(mfe_price - entry))
        left     = self._pip.price_to_pips(abs(take_profit - mfe_price))
        sl_dist  = abs(entry - original_sl)

        # ── Signal 1 (M1): breakeven / trail trigger ─────────────
        be_trigger = getattr(config, "BREAKEVEN_RR_TRIGGER", 2.0)
        profit_required = sl_dist * be_trigger
        in_trail_profit = (
            (direction == Direction.BULLISH and price >= entry + profit_required) or
            (direction == Direction.BEARISH and price <= entry - profit_required)
        )
        if in_trail_profit and current_sl == original_sl:
            # Only fire once — when SL hasn't been moved yet
            ev = MonitorEvent(
                trade_id=trade_id, symbol=self._sym,
                signal_time=m1_bar.time, signal_type="breakeven_trigger",
                timeframe_source="M1",
                price_at_signal=price, mfe_pips_at_signal=mfe_pips, pips_to_tp=left,
                detail=(
                    f"M1 | +{mfe_pips:.1f}p = {mfe_pips/max(self._pip.price_to_pips(sl_dist),0.01):.1f}× SL. "
                    f"Trail to swing structure."
                ),
                action_taken="none",
            )
            events.append(ev)

        # ── Signal 2 (M1): opposing zone approach ────────────────
        zone_hit = self._check_zone(direction, price, opposing_zones)
        if zone_hit:
            ev = MonitorEvent(
                trade_id=trade_id, symbol=self._sym,
                signal_time=m1_bar.time, signal_type="opposing_zone",
                timeframe_source="M1",
                price_at_signal=price, mfe_pips_at_signal=mfe_pips, pips_to_tp=left,
                detail=(
                    f"M1 | entering {zone_hit.zone_type.value} "
                    f"@ {zone_hit.low:.5f}-{zone_hit.high:.5f}. "
                    f"MFE={mfe_pips:.1f}p, {left:.1f}p to TP."
                ),
                action_taken="none",
            )
            events.append(ev)

        # ── Signal 3 (M5 only): reverse ChoCh ────────────────────
        if new_m5_bar and len(m5_window) >= 6:
            if self._check_reverse_choch(direction, entry, price, m5_window):
                ev = MonitorEvent(
                    trade_id=trade_id, symbol=self._sym,
                    signal_time=m1_bar.time, signal_type="reverse_choch",
                    timeframe_source="M5",
                    price_at_signal=price, mfe_pips_at_signal=mfe_pips, pips_to_tp=left,
                    detail=(
                        f"M5 | ChoCh against trade at +{mfe_pips:.1f}p. "
                        f"Structure is now "
                        f"{'bearish' if direction==Direction.BULLISH else 'bullish'}."
                    ),
                    action_taken="none",
                )
                events.append(ev)

        # ── Signal 4 (M5 only): MFE stall ────────────────────────
        stall_limit = getattr(config, "MFE_STALL_BARS", 6)
        if new_m5_bar and m5_bars_since_mfe >= stall_limit and mfe_pips > 0:
            ev = MonitorEvent(
                trade_id=trade_id, symbol=self._sym,
                signal_time=m1_bar.time, signal_type="mfe_stall",
                timeframe_source="M5",
                price_at_signal=price, mfe_pips_at_signal=mfe_pips, pips_to_tp=left,
                detail=(
                    f"M5 | stalled {m5_bars_since_mfe} M5 bars "
                    f"({m5_bars_since_mfe*5}min). "
                    f"+{mfe_pips:.1f}p, {left:.1f}p needed."
                ),
                action_taken="none",
            )
            events.append(ev)

        # ── Conviction check + action decision ────────────────────
        if events and getattr(config, "MONITOR_ACT_ON_SIGNALS", False):
            # Add new signals to the rolling window
            now           = m1_bar.time
            window_bars   = getattr(config, "MONITOR_CONFIRM_WINDOW_M5", 3)
            window_cutoff = now - timedelta(minutes=window_bars * 5)

            # Keep only signals within the confirmation window
            recent_in_window = [
                (st, t) for st, t in recent_signals if t >= window_cutoff
            ]
            # Add the new signals from this tick
            for ev in events:
                recent_in_window.append((ev.signal_type, now))

            unique_types = len({st for st, _ in recent_in_window})
            trail_conv   = getattr(config, "MONITOR_TRAIL_CONVICTION", 1)
            close_conv   = getattr(config, "MONITOR_CLOSE_CONVICTION", 3)

            if unique_types >= close_conv:
                # High conviction → close early
                action       = "close_now"
                should_close = True
                for ev in events:
                    ev.action_taken = "closed_early"

            elif unique_types >= trail_conv and current_sl == original_sl:
                # Enough conviction to trail SL — but trail to SWING, not entry
                swing_sl = find_swing_trail_sl(
                    direction=   direction,
                    entry=       entry,
                    original_sl= original_sl,
                    m5_window=   m5_window,
                    pip_calc=    self._pip,
                    atr_price=   self._atr,
                )
                if swing_sl is not None:
                    action    = "trail_sl"
                    new_sl_val = swing_sl
                    for ev in events:
                        if ev.signal_type == "breakeven_trigger":
                            ev.action_taken = "trailed_sl"
                            ev.new_sl       = swing_sl
                    logger.info(
                        "MONITOR TRAIL | %s | SL %.5f → %.5f (swing-based, not entry)",
                        self._sym, original_sl, swing_sl,
                    )

        return events, action, new_sl_val, should_close

    # ── Legacy M5-only update (backward compat) ───────────────────

    def update(
        self,
        trade_id, direction, entry, stop_loss, take_profit,
        bar, recent_m5, opposing_zones, mfe_price, bars_since_mfe_update,
    ):
        """Original M5-only interface. Delegates to update_dual()."""
        evts, act, new_sl, close = self.update_dual(
            trade_id=trade_id, direction=direction, entry=entry,
            current_sl=stop_loss, original_sl=stop_loss,
            take_profit=take_profit, m1_bar=bar,
            m5_window=recent_m5, opposing_zones=opposing_zones,
            mfe_price=mfe_price,
            m1_bars_since_mfe=bars_since_mfe_update,
            m5_bars_since_mfe=bars_since_mfe_update,
            new_m5_bar=True, recent_signals=[],
        )
        return evts, act if act not in ("trail_sl",) else "none", close

    # ── Live wrapper ──────────────────────────────────────────────

    def check_live_dual(
        self,
        trade_id, direction, entry, current_sl, original_sl, take_profit,
        current_m1, current_m5,
        opposing_zones, mfe_price,
        m1_bars_since_mfe, m5_bars_since_mfe,
        new_m5_bar,
        recent_signals,
    ) -> Tuple[List[MonitorEvent], str, float, bool]:
        """Entry point for live trading. Called every M1 tick (~60s loop)."""
        if not current_m1:
            return [], "none", current_sl, False
        return self.update_dual(
            trade_id=trade_id, direction=direction, entry=entry,
            current_sl=current_sl, original_sl=original_sl,
            take_profit=take_profit, m1_bar=current_m1[-1],
            m5_window=current_m5[-12:], opposing_zones=opposing_zones,
            mfe_price=mfe_price,
            m1_bars_since_mfe=m1_bars_since_mfe,
            m5_bars_since_mfe=m5_bars_since_mfe,
            new_m5_bar=new_m5_bar,
            recent_signals=recent_signals,
        )

    # ── Private checks ────────────────────────────────────────────

    def _check_zone(self, direction, price, opposing):
        buf = self._atr * getattr(config, "OPPOSING_ZONE_WARN_ATR", 1.0)
        for z in opposing:
            if direction == Direction.BULLISH and price >= z.low - buf:
                return z
            if direction == Direction.BEARISH and price <= z.high + buf:
                return z
        return None

    def _check_reverse_choch(self, direction, entry, price, m5):
        if direction == Direction.BULLISH and price <= entry:
            return False
        if direction == Direction.BEARISH and price >= entry:
            return False
        if len(m5) < 6:
            return False
        w = m5[-8:] if len(m5) >= 8 else m5
        if direction == Direction.BULLISH:
            hs = [c.high for c in w]
            if not (hs[-1] < hs[-2] < hs[-3]):
                return False
            return w[-1].close < min(c.low for c in w[-5:-1])
        else:
            ls = [c.low for c in w]
            if not (ls[-1] > ls[-2] > ls[-3]):
                return False
            return w[-1].close > max(c.high for c in w[-5:-1])


# ─────────────────────────────────────────────
#  Swing trail SL finder (module-level helper)
# ─────────────────────────────────────────────

def find_swing_trail_sl(
    direction:   Direction,
    entry:       float,
    original_sl: float,
    m5_window:   List[Candle],
    pip_calc:    PipCalculator,
    atr_price:   float,
    min_improvement_pips: float = 2.0,
) -> Optional[float]:
    """
    Find the most recent M5 swing low (for long) or swing high (for short)
    that can serve as a smarter trailing stop than hard entry price.

    Rules for a qualifying swing:
      • For long:  swing LOW must be > original_sl AND < entry
      • For short: swing HIGH must be < original_sl AND > entry
      • Must be at least min_improvement_pips better than original_sl
      • A buffer of MONITOR_TRAIL_ATR_BUFFER × ATR is subtracted (long)
        or added (short) to the swing level to avoid stop-hunting at the exact
        swing price

    Returns the trail SL price, or None if no valid swing found.
    (Caller keeps the original SL unchanged when None is returned.)
    """
    if len(m5_window) < 5:
        return None

    buf     = atr_price * getattr(config, "MONITOR_TRAIL_ATR_BUFFER", 0.5)
    min_imp = pip_calc.pips_to_price(min_improvement_pips)

    # Scan from most recent to oldest — take the first qualifying swing
    for i in range(len(m5_window) - 2, 1, -1):
        c    = m5_window[i]
        prev = m5_window[i - 1]
        nxt  = m5_window[i + 1]

        if direction == Direction.BULLISH:
            # Swing low: c.low < both neighbours
            is_swing = c.low < prev.low and c.low < nxt.low
            if not is_swing:
                continue
            candidate_sl = c.low - buf
            # Must be above original_sl + min_improvement and below entry
            if candidate_sl > original_sl + min_imp and candidate_sl < entry:
                return round(candidate_sl, 5)

        else:  # BEARISH
            # Swing high: c.high > both neighbours
            is_swing = c.high > prev.high and c.high > nxt.high
            if not is_swing:
                continue
            candidate_sl = c.high + buf
            # Must be below original_sl - min_improvement and above entry
            if candidate_sl < original_sl - min_imp and candidate_sl > entry:
                return round(candidate_sl, 5)

    return None