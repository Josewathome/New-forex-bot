"""
gap_recovery_service.py — Restart Gap Recovery.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When the bot stops (power cut, crash, manual restart), it misses
live market events.  On restart this service:

  1. Computes the gap between the last checkpoint and now.
  2. If the gap is ≤ 3 hours, reconstructs what happened:
       a. Re-maps zones for any H1/M30/M5 bars that closed during gap.
       b. Fetches M5 candles from the gap period.
       c. Replays those candles through monitoring logic to update:
            - mfe_price  (max favourable excursion)
            - m5_window  (swing-trail sliding window)
            - m5_no_mfe  (stall counter)
            - last_m5_time
       d. Reconciles open trade volume/SL with MT5 reality.
       e. Catches up swing-trail SL if a better level was missed.
  3. Marks a `_min_signal_time` on ExecutionService so the first live
     loop does not act on signals whose formation candle fell inside
     the gap (stale historical setups).
  4. If the gap is > 3 hours, re-maps zones only and logs a warning.

Safety guarantees
─────────────────
• Entry evaluation is NEVER called during replay — `_replay_mode`
  blocks `collect_entry_candidate()` entirely.
• `zone.mark_tapped()` is suppressed during replay to keep touch
  counts honest.
• TP1 partial-close logic is NOT re-executed for gap candles because
  the bot was down when those bars formed.  Instead the service detects
  whether MT5 position volume shrank (indicating a manual or SL/TP
  event) and reconciles the lot_size accordingly.
• All exceptions are swallowed and logged; the bot always continues.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, TYPE_CHECKING

import src.config as config
from src.domain.entities import Candle, Direction, Timeframe
from src.domain.value_objects import PipCalculator
from src.infrastructure.zone_store import ZoneFileStore

if TYPE_CHECKING:
    from src.application.execution_service import ExecutionService, _OpenTradeState
    from src.infrastructure.market_data_repo import MT5MarketDataRepository
    from src.infrastructure.zone_repo import InMemoryZoneRepository
    from src.infrastructure.mt5_bridge.mt5_gateway import MT5Gateway
    from src.application.email_service import EmailService

logger = logging.getLogger(__name__)

# Hard ceiling on gap reconstruction. Gaps older than this skip candle
# replay (too many bars, stale context, low value) and do fresh zone
# mapping only.
MAX_GAP_SECONDS: int = 3 * 3600   # 3 hours

# Minimum gap below which no recovery is attempted — just one missed
# loop tick, not worth the MT5 round-trip overhead.
MIN_GAP_SECONDS: int = 60          # 1 minute


@dataclass
class GapRecoveryResult:
    """Summary of what was done during gap recovery. Logged by main.py."""
    skipped:              bool          = False   # True if gap > MAX or no checkpoint
    gap_seconds:          float         = 0.0
    h1_bars_missed:       int           = 0
    m30_bars_missed:      int           = 0
    m5_bars_missed:       int           = 0
    zones_remapped:       bool          = False
    trades_reconstructed: int           = 0
    warning_messages:     List[str]     = field(default_factory=list)


def _missed_bars(gap_start_ts: float, gap_end_ts: float, bar_secs: int) -> int:
    """Count bar boundaries crossed between two epoch timestamps."""
    last = int(gap_start_ts) // bar_secs * bar_secs
    curr = int(gap_end_ts)   // bar_secs * bar_secs
    return max(0, (curr - last) // bar_secs)


class GapRecoveryService:
    """
    Reconstructs market state for the period between the last checkpoint
    and the current restart time.
    """

    def __init__(
        self,
        execution:   "ExecutionService",
        market_data: "MT5MarketDataRepository",
        zone_repo:   "InMemoryZoneRepository",
        zone_store:  ZoneFileStore,
        trade_repo:  "MT5Gateway",      # raw gateway — used for get_open_positions
        symbols:     List[str],
        email_svc:   Optional["EmailService"] = None,
        clock=       None,              # BrokerClock — broker time for gap boundary
    ) -> None:
        self._exec       = execution
        self._md         = market_data
        self._zr         = zone_repo
        self._zs         = zone_store
        self._gw         = trade_repo
        self._symbols    = symbols
        self._email      = email_svc
        self._clock      = clock

    def _now(self) -> datetime:
        """Return current UTC time from IC Markets broker clock."""
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(tz=timezone.utc)

    # ── Public API ────────────────────────────────────────────────────

    def needs_recovery(self, checkpoint: dict) -> bool:
        """True when the gap since last checkpoint warrants reconstruction."""
        ts = checkpoint.get("last_loop_ts") or checkpoint.get("saved_at")
        if not ts:
            return False
        try:
            gap = (self._now() - datetime.fromisoformat(ts)).total_seconds()
            return gap > MIN_GAP_SECONDS
        except Exception:
            return False

    def run(self, checkpoint: dict) -> GapRecoveryResult:
        """
        Full gap-recovery pass.  Never raises — all errors are caught and
        logged so the bot always starts even if reconstruction fails.
        """
        result = GapRecoveryResult()

        # ── Step 1: compute the gap ──────────────────────────────────
        ts_str = checkpoint.get("last_loop_ts") or checkpoint.get("saved_at")
        if not ts_str:
            result.skipped = True
            result.warning_messages.append("No temporal anchor in checkpoint — gap recovery skipped.")
            return result

        try:
            gap_start = datetime.fromisoformat(ts_str)
            if gap_start.tzinfo is None:
                gap_start = gap_start.replace(tzinfo=timezone.utc)
        except Exception as exc:
            result.skipped = True
            result.warning_messages.append(f"Cannot parse checkpoint timestamp '{ts_str}': {exc}")
            return result

        now = self._now()
        gap_secs = (now - gap_start).total_seconds()
        result.gap_seconds = gap_secs

        # Clock-drift guard: negative or trivial gap → nothing to do.
        if gap_secs <= MIN_GAP_SECONDS:
            result.skipped = True
            logger.debug("Gap recovery: gap=%.0fs < %ds — nothing to reconstruct.", gap_secs, MIN_GAP_SECONDS)
            return result

        logger.info(
            "GAP RECOVERY | gap=%.0fs (%.1f min) | checkpoint=%s | now=%s",
            gap_secs, gap_secs / 60,
            gap_start.strftime("%Y-%m-%dT%H:%M:%S"),
            now.strftime("%Y-%m-%dT%H:%M:%S"),
        )

        # ── Step 2: enforce max gap ──────────────────────────────────
        if gap_secs > MAX_GAP_SECONDS:
            msg = (
                f"Gap of {gap_secs/3600:.1f}h exceeds the 3-hour recovery limit. "
                f"Zone mapping only — trade monitoring NOT reconstructed."
            )
            logger.warning("GAP RECOVERY SKIP | %s", msg)
            result.warning_messages.append(msg)
            result.skipped = True
            # Still run zone remapping and SL reconciliation — these are always safe.
            self._remap_all_zones(result)
            self._reconcile_sl_with_mt5(result)
            self._send_gap_alert(gap_secs)
            return result

        # ── Step 3: compute missed bar counts ────────────────────────
        gs = gap_start.timestamp()
        ne = now.timestamp()

        # Prefer per-timeframe anchors from the checkpoint if available;
        # fall back to the loop timestamp for all timeframes.
        h1_anchor  = int(checkpoint.get("last_h1_bar_ts",  gs))
        m30_anchor = int(checkpoint.get("last_m30_bar_ts", gs))
        m5_anchor  = int(checkpoint.get("last_m5_bar_ts",  gs))

        result.h1_bars_missed  = _missed_bars(h1_anchor,  ne, 3600)
        result.m30_bars_missed = _missed_bars(m30_anchor, ne, 1800)
        result.m5_bars_missed  = _missed_bars(m5_anchor,  ne, 300)

        logger.info(
            "GAP RECOVERY | missed bars — H1=%d  M30=%d  M5=%d",
            result.h1_bars_missed, result.m30_bars_missed, result.m5_bars_missed,
        )

        # ── Step 4: zone remapping ───────────────────────────────────
        # Zone mapping is idempotent (full window recompute), so one call
        # per timeframe is enough regardless of how many bars were missed.
        self._remap_all_zones(result)

        # ── Step 5: fetch gap M5 candles ─────────────────────────────
        gap_candles: Dict[str, List[Candle]] = {}
        for sym in self._symbols:
            try:
                candles = self._md.get_candles_range(sym, Timeframe.M5, gap_start, now)
                # Sort and deduplicate by bar timestamp (defensive against MT5 quirks).
                seen: set = set()
                unique: List[Candle] = []
                for c in sorted(candles, key=lambda x: x.time):
                    if c.time not in seen:
                        seen.add(c.time)
                        unique.append(c)
                gap_candles[sym] = unique
                if unique:
                    logger.info(
                        "GAP RECOVERY | %s | fetched %d M5 candles (%s → %s)",
                        sym, len(unique),
                        unique[0].time.strftime("%H:%M"),
                        unique[-1].time.strftime("%H:%M"),
                    )
                else:
                    logger.info("GAP RECOVERY | %s | no M5 candles in gap (market closed?)", sym)
            except Exception as exc:
                logger.warning("GAP RECOVERY | %s | M5 fetch failed: %s", sym, exc)
                gap_candles[sym] = []

        # ── Step 6: SL reconciliation with MT5 reality ───────────────
        # Do this BEFORE candle replay so we start from the correct SL baseline.
        self._reconcile_sl_with_mt5(result)

        # ── Step 7: replay gap candles through monitoring logic ───────
        self._exec._replay_mode = True
        try:
            reconstructed = 0
            for ticket, state in list(self._exec._trade_states.items()):
                sym     = state.symbol
                candles = gap_candles.get(sym, [])
                if not candles:
                    continue
                try:
                    self._reconstruct_trade_state(state, candles)
                    reconstructed += 1
                except Exception as exc:
                    logger.warning(
                        "GAP RECOVERY | ticket=%s | monitoring reconstruction failed: %s",
                        ticket, exc,
                    )
            result.trades_reconstructed = reconstructed
        finally:
            self._exec._replay_mode = False

        # ── Step 8: swing-trail catch-up ─────────────────────────────
        # After rebuilding the m5_window, check if a better SL exists and
        # apply it to MT5 immediately.
        self._catchup_swing_trail(gap_candles, result)

        # ── Step 9: set gap-guard on ExecutionService ─────────────────
        # The last M5 bar timestamp in the gap becomes the staleness boundary.
        # Any signal whose created_at ≤ this time is historical, not live.
        latest_gap_m5: Optional[datetime] = None
        for candles in gap_candles.values():
            if candles and (latest_gap_m5 is None or candles[-1].time > latest_gap_m5):
                latest_gap_m5 = candles[-1].time
        if latest_gap_m5 is not None:
            self._exec._min_signal_time = latest_gap_m5
            logger.info(
                "GAP RECOVERY | gap guard set: signals created_at ≤ %s will be skipped on first live cycle",
                latest_gap_m5.strftime("%Y-%m-%dT%H:%M:%S"),
            )

        logger.info(
            "GAP RECOVERY COMPLETE | gap=%.0fs | H1=%d M30=%d M5=%d bars remapped | "
            "%d trade(s) reconstructed",
            gap_secs,
            result.h1_bars_missed, result.m30_bars_missed, result.m5_bars_missed,
            result.trades_reconstructed,
        )
        return result

    # ── Internal helpers ──────────────────────────────────────────────

    def _remap_all_zones(self, result: GapRecoveryResult) -> None:
        """Re-map H1/M30/M5 zones for all symbols. Safe to call unconditionally."""
        for sym in self._symbols:
            try:
                self._exec.run_zone_mapping(sym)
                self._zs.save_snapshot(
                    sym,
                    self._zr.get_active_zones(sym),
                    self._zr.get_liquidity_pool(sym),
                )
                logger.debug("GAP RECOVERY | %s | H1 zones remapped", sym)
            except Exception as exc:
                logger.warning("GAP RECOVERY | %s | H1 zone remap failed: %s", sym, exc)

            try:
                self._exec.run_m30_zone_mapping(sym)
                logger.debug("GAP RECOVERY | %s | M30 zones remapped", sym)
            except Exception as exc:
                logger.warning("GAP RECOVERY | %s | M30 zone remap failed: %s", sym, exc)

            try:
                self._exec.run_m5_zone_mapping(sym)
                logger.debug("GAP RECOVERY | %s | M5 zones remapped", sym)
            except Exception as exc:
                logger.warning("GAP RECOVERY | %s | M5 zone remap failed: %s", sym, exc)

        result.zones_remapped = True

    def _reconcile_sl_with_mt5(self, result: GapRecoveryResult) -> None:
        """
        Compare the SL stored in each _OpenTradeState against what MT5 actually
        holds.  MT5 is authoritative: if the SL moved (e.g. the user manually
        adjusted it via the terminal), update state.current_sl to match.
        Also reconciles lot_size in case a partial close happened while the bot
        was down (e.g. a human closed part of the position).
        """
        try:
            live_positions = {p["ticket"]: p for p in (self._gw.get_open_positions() or [])}
        except Exception as exc:
            logger.warning("GAP RECOVERY | SL reconciliation skipped (MT5 unavailable): %s", exc)
            return

        for ticket, state in list(self._exec._trade_states.items()):
            live = live_positions.get(ticket)
            if live is None:
                # Trade closed while bot was down — will be pruned by
                # _prune_closed_positions() on the first monitoring cycle.
                logger.info(
                    "GAP RECOVERY | ticket=%s %s closed during gap — will be pruned on first monitoring pass.",
                    ticket, state.symbol,
                )
                continue

            # SL reconciliation
            mt5_sl = float(live.get("sl", state.current_sl))
            if mt5_sl != state.current_sl:
                logger.info(
                    "GAP RECOVERY | ticket=%s %s | SL reconcile: state=%.5f → MT5=%.5f",
                    ticket, state.symbol, state.current_sl, mt5_sl,
                )
                state.current_sl = mt5_sl

            # Volume reconciliation — detect partial closes during downtime
            mt5_vol = float(live.get("volume", state.lot_size))
            if mt5_vol < state.lot_size - 0.001:   # more than rounding tolerance
                logger.info(
                    "GAP RECOVERY | ticket=%s %s | volume reconcile: state=%.2f → MT5=%.2f "
                    "(partial close happened during gap — marking tp1_hit=True)",
                    ticket, state.symbol, state.lot_size, mt5_vol,
                )
                state.lot_size = mt5_vol
                # A volume reduction implies a partial close fired → TP1 was hit.
                if not state.tp1_hit:
                    state.tp1_hit = True

    def _reconstruct_trade_state(
        self,
        state:   "_OpenTradeState",
        candles: List[Candle],
    ) -> None:
        """
        Replay gap M5 candles through the monitoring logic for a single trade.

        Updates:
          - mfe_price       (max favourable excursion seen during the gap)
          - m5_window       (sliding 12-bar window for swing trailing)
          - last_m5_time    (timestamp of the last processed M5 bar)
          - m5_no_mfe       (bars without new MFE — stall counter)

        Does NOT execute TP1/TP2 partial closes, SL moves, or monitor closes.
        Those are handled by the live loop on the first tick after recovery.
        """
        # Skip bars we already processed (bars at or before last_m5_time).
        start_from = state.last_m5_time
        relevant   = [c for c in candles if start_from is None or c.time > start_from]
        if not relevant:
            return

        m1_no_mfe_acc = 0   # proxy for m1_no_mfe between M5 bars

        for candle in relevant:
            # Update MFE from candle high/low (conservative: use the extreme
            # most favourable for the trade direction).
            if state.direction == Direction.BULLISH:
                if candle.high > state.mfe_price:
                    state.mfe_price  = candle.high
                    m1_no_mfe_acc    = 0
                    state.m5_no_mfe  = 0
                else:
                    m1_no_mfe_acc += 1
                    state.m5_no_mfe += 1
            else:
                if candle.low < state.mfe_price:
                    state.mfe_price  = candle.low
                    m1_no_mfe_acc    = 0
                    state.m5_no_mfe  = 0
                else:
                    m1_no_mfe_acc += 1
                    state.m5_no_mfe += 1

            # Maintain the M5 sliding window (max 12 bars).
            state.m5_window.append(candle)
            if len(state.m5_window) > 12:
                state.m5_window.pop(0)

            # Advance the M5 bar timestamp.
            state.last_m5_time = candle.time

        logger.debug(
            "GAP RECOVERY | ticket=%s %s | replayed %d M5 bars | "
            "mfe=%.5f m5_no_mfe=%d m5_win=%d",
            state.ticket, state.symbol, len(relevant),
            state.mfe_price, state.m5_no_mfe, len(state.m5_window),
        )

    def _catchup_swing_trail(
        self,
        gap_candles: Dict[str, List[Candle]],
        result:      GapRecoveryResult,
    ) -> None:
        """
        After m5_window is rebuilt from gap candles, compute the optimal
        swing-trail SL.  If it improves on the current MT5 SL, apply it
        immediately so we don't leave money on the table.
        """
        from src.application.trade_monitor import find_swing_trail_sl

        for ticket, state in list(self._exec._trade_states.items()):
            if not state.tp1_hit:
                # Swing trail only applies after TP1 hit.
                continue
            if len(state.m5_window) < 5:
                # Not enough bars for reliable swing detection.
                continue

            sym      = state.symbol
            digits   = state.digits
            pip_calc = PipCalculator(digits=digits)

            # Get H1 ATR for the trail buffer.
            try:
                h1_bars = self._md.get_candles(sym, Timeframe.H1, 20)
                from src.application.analysis import atr
                h1_atr = atr(h1_bars) if h1_bars else 0.0
            except Exception:
                h1_atr = 0.0

            try:
                new_sl = find_swing_trail_sl(
                    direction=  state.direction,
                    entry=      state.entry,
                    current_sl= state.current_sl,
                    m5_window=  state.m5_window,
                    pip_calc=   pip_calc,
                    atr_price=  h1_atr,
                )
            except Exception as exc:
                logger.debug("GAP RECOVERY | ticket=%s swing trail calc failed: %s", ticket, exc)
                continue

            if new_sl is None:
                continue

            # Validate: new SL must be in the profitable direction.
            sl_improved = (
                (state.direction == Direction.BULLISH and new_sl > state.current_sl) or
                (state.direction == Direction.BEARISH and new_sl < state.current_sl)
            )
            if not sl_improved:
                continue

            logger.info(
                "GAP RECOVERY | ticket=%s %s | swing-trail catch-up: %.5f → %.5f",
                ticket, sym, state.current_sl, new_sl,
            )
            try:
                # Fetch live MT5 position for current TP.
                live_positions = {p["ticket"]: p for p in (self._gw.get_open_positions() or [])}
                live = live_positions.get(ticket)
                if live is None:
                    continue
                tp = float(live.get("tp", state.take_profit))
                ok = self._gw.modify_position(ticket, sl=new_sl, tp=tp)
                if ok:
                    state.current_sl  = new_sl
                    state.sl_trailed  = True
                    logger.info(
                        "GAP RECOVERY | ticket=%s | SL catch-up applied in MT5: %.5f",
                        ticket, new_sl,
                    )
                else:
                    logger.warning(
                        "GAP RECOVERY | ticket=%s | MT5 SL modify failed for catch-up", ticket
                    )
            except Exception as exc:
                logger.warning("GAP RECOVERY | ticket=%s | SL catch-up error: %s", ticket, exc)

    def _send_gap_alert(self, gap_secs: float) -> None:
        """Send an email when the gap exceeds the recovery limit."""
        if not self._email:
            return
        try:
            hours = gap_secs / 3600
            subject = f"ZONE BOT: Long gap detected ({hours:.1f}h) — zones recomputed"
            body = (
                f"The bot was offline for {hours:.1f} hours.\n\n"
                f"Gap exceeded the 3-hour recovery limit, so trade monitoring "
                f"was NOT reconstructed from historical candles.\n\n"
                f"Actions taken automatically:\n"
                f"  ✓ H1 / M30 / M5 zones recomputed from current candles\n"
                f"  ✓ SL values reconciled against live MT5 positions\n"
                f"  ✗ M5 candle replay skipped (gap too large)\n\n"
                f"Action required:\n"
                f"  • Review any open trades in MT5 — MFE tracking and swing-trail\n"
                f"    SL calculations will resume from the current bar forward.\n"
                f"  • If a trade should have been closed by time-exit logic, close it\n"
                f"    manually via the MT5 terminal.\n\n"
                f"Account : {config.TRADING_ID}\n"
                f"Server  : {config.MT5_SERVER}\n"
            )
            self._email.send_report(subject, body)
            logger.info("GAP RECOVERY | alert email sent for %.1fh gap.", hours)
        except Exception as exc:
            logger.warning("GAP RECOVERY | failed to send alert email: %s", exc)
