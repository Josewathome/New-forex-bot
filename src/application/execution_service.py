"""
execution_service.py — Phase 3: Entry + Phase 3.5: Open Trade Guardian

WHAT'S NEW (v4 — live monitor wired)
───────────────────────────────────────────────────────────────────────────────
run_monitoring_cycle() now does two jobs on every call (~60s):

  ① Monitor open trades first (Phase 3.5)
       For each open position on the given symbol:
       • Track MFE from live M1 candles
       • Detect new M5 bar by comparing timestamps
       • Run TradeMonitor.check_live_dual() with M1 bar + M5 window
       • On conviction: trail SL to swing structure via modify_sl()
         (NOT to hard entry — that chokes the trade on a normal retrace)
       • On high conviction: close the position early

  ② Look for new entry signals (existing Phase 3 logic unchanged)

OPEN TRADE STATE
───────────────────────────────────────────────────────────────────────────────
Each open position is tracked in self._trade_states[ticket] which holds:
  • mfe_price       — peak price seen since entry (M1 precision)
  • m1_no_mfe       — M1 bars since last MFE update
  • m5_no_mfe       — M5 bars since last MFE update
  • last_m5_time    — used to detect new M5 bar close
  • m5_window       — rolling last 12 M5 candles
  • original_sl     — the SL at trade entry (never changes)
  • current_sl      — may differ after trailing
  • opposing_zones  — cached at first check (no re-scan every tick)
  • recent_signals  — (signal_type, datetime) list for conviction window

State is removed when the position is no longer in MT5 open positions.
"""
from __future__ import annotations
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import src.config as config
from src.domain.entities import (
    Direction, LiquidityPool, Timeframe, Trade, TradeSignal, TradeStatus, Zone,
)
from src.domain.repositories import IMarketDataRepository, ITradeRepository, IZoneRepository
from src.domain.value_objects import BrokerCost, PipCalculator, RiskParameters
from src.application.analysis import (
    ZoneMapper, atr,
    find_m5_choch_signal,
    find_h4_bias,
    compute_h4_equilibrium,
    score_setup,
    grade_setup,
    calculate_zone_buffer,
    is_in_trading_session,
)
from src.application.news_manager import NewsManager
from src.application.trade_monitor import TradeMonitor, MonitorEvent, find_swing_trail_sl
from src.infrastructure.spread_calculator import SpreadCalculator

logger = logging.getLogger(__name__)

_M5 = timedelta(minutes=5)


# ─────────────────────────────────────────────
#  Per-trade live state container
# ─────────────────────────────────────────────

@dataclass
class _OpenTradeState:
    """
    Everything the monitor needs to evaluate one live position.
    Keyed by MT5 ticket in ExecutionService._trade_states.
    """
    ticket:          int
    symbol:          str
    direction:       Direction
    entry:           float
    original_sl:     float           # entry SL — never changes
    current_sl:      float           # may be trailed
    take_profit:     float
    mfe_price:       float           # M1-precision peak
    m1_no_mfe:       int = 0
    m5_no_mfe:       int = 0
    last_m5_time:    Optional[datetime] = None
    m5_window:       List = field(default_factory=list)   # last 12 M5 candles
    opposing_zones:  List = field(default_factory=list)   # cached at first check
    recent_signals:  List = field(default_factory=list)   # (type, datetime) window
    sl_trailed:      bool = False    # True once SL has been trailed (trail once only)


# ─────────────────────────────────────────────
#  ExecutionService
# ─────────────────────────────────────────────

class ExecutionService:

    def __init__(
        self,
        market_data:       IMarketDataRepository,
        trade_repo:        ITradeRepository,
        zone_repo:         IZoneRepository,
        news_manager:      NewsManager,
        spread_calculator: SpreadCalculator,
        symbols:           List[str],
        risk_pct:          float,
        commission:        float,
        min_rr:            float,
        account_currency:  str,
    ) -> None:
        self._md          = market_data
        self._tr          = trade_repo
        self._zr          = zone_repo
        self._news        = news_manager
        self._spread      = spread_calculator
        self._symbols     = symbols
        self._risk_pct    = risk_pct
        self._commission  = commission
        self._min_rr      = min_rr
        self._currency    = account_currency
        self._zone_mapper = ZoneMapper()

        # Live monitor state — persists across loop iterations
        self._trade_states: Dict[int, _OpenTradeState] = {}

    # ── Phase 1: Zone Mapping ────────────────────────────────────

    def run_zone_mapping(self, symbol: str) -> None:
        h1 = self._md.get_candles(symbol, Timeframe.H1, config.H1_CANDLE_COUNT)
        if not h1:
            logger.warning("No H1 candles for %s", symbol)
            return
        zones, pool = self._zone_mapper.map_zones(h1, symbol, Timeframe.H1)
        for old in self._zr.get_active_zones(symbol):
            self._zr.invalidate_zone(old.id)
        for z in zones:
            self._zr.save_zone(z)
        if pool:
            self._zr.save_liquidity_pool(pool)

    # ── Phase 2 + 3 + 3.5: Monitor open trades → look for entry ──

    def run_monitoring_cycle(self, symbol: str) -> Optional[Trade]:
        now = datetime.now(tz=timezone.utc)

        # ── Step 0: prune stale states ────────────────────────────
        self._prune_closed_positions()

        # ── Step 1: monitor all open trades on this symbol ────────
        if getattr(config, "MONITOR_ENABLED", True):
            self._monitor_open_trades(symbol, now)

        # ── Step 2: news guard ────────────────────────────────────
        if self._news.is_blocked(symbol, now):
            return None
        if not is_in_trading_session(now):
            return None

        # ── Step 3: broker cost guard ────────────────────────────
        broker_cost = self._spread.get_broker_cost(symbol)
        h1_short    = self._md.get_candles(symbol, Timeframe.H1, 20)
        h1_atr      = atr(h1_short) if h1_short else 0.0
        if h1_short and self._news.broker_cost_exceeds_atr(
                symbol, h1_atr, broker_cost, PipCalculator(
                    digits=self._md.get_symbol_digits(symbol))):
            return None

        # ── Step 4: H4 macro context ──────────────────────────────
        h4 = self._md.get_candles(symbol, Timeframe.H4, config.H4_CANDLE_COUNT)
        if not h4:
            return None
        h4_bias             = find_h4_bias(h4)
        h4_high, h4_low, h4_eq = compute_h4_equilibrium(h4)
        ask                 = self._md.get_current_price(symbol)
        if ask == 0.0:
            return None

        # ── Step 5: Zone + pool ───────────────────────────────────
        digits     = self._md.get_symbol_digits(symbol)
        pip_calc   = PipCalculator(digits=digits)
        tick_value = self._md.get_tick_value(symbol)
        zone_buf   = calculate_zone_buffer(h1_atr)
        zones      = self._zr.get_active_zones(symbol)
        pool       = self._zr.get_liquidity_pool(symbol)
        if not pool:
            return None

        for zone in zones:
            setup_score = score_setup(zone.direction, h4_bias, ask, h4_eq)
            if setup_score < config.SETUP_SCORE_THRESHOLD:
                continue
            grade, risk_pct, min_rr = grade_setup(setup_score)

            if not (ask <= zone.high + zone_buf and ask >= zone.low - zone_buf):
                continue
            zone.mark_tapped()

            m5 = self._md.get_candles(symbol, Timeframe.M5, config.M5_CANDLE_COUNT)
            if not m5:
                continue

            signal = find_m5_choch_signal(
                m5_candles=m5, zone=zone, pip_calc=pip_calc,
                spread_pips=broker_cost.spread_pips, liquidity_pool=pool,
                atr_price=h1_atr, zone_buffer=zone_buf, min_rr_override=min_rr,
            )
            if signal is None:
                continue

            open_count = sum(
                1 for t in self._tr.get_open_positions() if t.symbol == symbol
            )
            if open_count >= config.MAX_OPEN_TRADES:
                logger.info("MAX_OPEN_TRADES reached for %s", symbol)
                continue

            trade = self._build_trade(
                signal, broker_cost, pip_calc, tick_value,
                risk_pct_override=risk_pct,
            )
            if trade is None:
                continue

            ticket = self._tr.place_trade(trade)
            if ticket is None:
                logger.error("Trade placement failed for %s", symbol)
                continue

            trade.mt5_ticket = ticket
            trade.status     = TradeStatus.OPEN

            # ── Initialise monitor state for the new trade ────────
            self._trade_states[ticket] = _OpenTradeState(
                ticket=      ticket,
                symbol=      symbol,
                direction=   signal.direction,
                entry=       signal.entry_price,
                original_sl= signal.stop_loss,
                current_sl=  signal.stop_loss,
                take_profit= signal.take_profit,
                mfe_price=   signal.entry_price,
            )

            logger.info(
                "TRADE PLACED ✓ | %s %s | grade=%s | "
                "entry=%.5f SL=%.5f TP=%.5f | lot=%.2f | RR=%.2f",
                symbol, signal.direction.value, grade,
                signal.entry_price, signal.stop_loss, signal.take_profit,
                trade.lot_size, signal.risk_reward,
            )
            return trade

        return None

    # ── Phase 3.5: Monitor open trades ───────────────────────────

    def _monitor_open_trades(self, symbol: str, now: datetime) -> None:
        """
        Check every open position on `symbol` with the TradeMonitor.
        Called from run_monitoring_cycle() before looking for new entries.
        """
        open_positions = [
            p for p in self._tr.get_open_positions()
            if p.symbol == symbol
        ]
        if not open_positions:
            return

        # Fetch candles once for all positions on this symbol
        m1_bars = self._md.get_candles(symbol, Timeframe.M1, 30)
        m5_bars = self._md.get_candles(symbol, Timeframe.M5, 15)
        h1_bars = self._md.get_candles(symbol, Timeframe.H1, 20)
        zones   = self._zr.get_active_zones(symbol)

        digits    = self._md.get_symbol_digits(symbol)
        pip_calc  = PipCalculator(digits=digits)
        h1_atr    = atr(h1_bars) if h1_bars else 0.0
        monitor   = TradeMonitor(pip_calc=pip_calc, atr_price=h1_atr, symbol=symbol)

        for pos in open_positions:
            ticket = pos.mt5_ticket or int(pos.id)

            # Initialise state if this is the first time we see this ticket
            if ticket not in self._trade_states:
                self._trade_states[ticket] = _OpenTradeState(
                    ticket=      ticket,
                    symbol=      symbol,
                    direction=   pos.direction,
                    entry=       pos.entry_price,
                    original_sl= pos.stop_loss,
                    current_sl=  pos.stop_loss,
                    take_profit= pos.take_profit,
                    mfe_price=   pos.entry_price,
                )

            state = self._trade_states[ticket]

            # ── Update MFE from M1 bars ───────────────────────────
            current_price = self._md.get_current_price(symbol)
            if current_price > 0:
                if state.direction == Direction.BULLISH:
                    if current_price > state.mfe_price:
                        state.mfe_price = current_price
                        state.m1_no_mfe = 0
                        state.m5_no_mfe = 0
                    else:
                        state.m1_no_mfe += 1
                else:
                    if current_price < state.mfe_price:
                        state.mfe_price = current_price
                        state.m1_no_mfe = 0
                        state.m5_no_mfe = 0
                    else:
                        state.m1_no_mfe += 1

            # ── Detect new M5 bar ─────────────────────────────────
            new_m5_bar = False
            if m5_bars:
                latest_m5_time = m5_bars[-1].time
                if state.last_m5_time is None or latest_m5_time > state.last_m5_time:
                    new_m5_bar         = True
                    state.last_m5_time = latest_m5_time
                    state.m5_window.append(m5_bars[-1])
                    if len(state.m5_window) > 12:
                        state.m5_window.pop(0)
                    # Check if MFE was updated since last M5 bar
                    if state.m1_no_mfe > 0:
                        state.m5_no_mfe += 1
                    else:
                        state.m5_no_mfe = 0

            # ── Cache opposing zones (scan once per trade) ────────
            if not state.opposing_zones and zones:
                state.opposing_zones = monitor.scan_opposing_zones(
                    state.direction, state.entry, state.take_profit, zones,
                )

            # ── Run monitor ───────────────────────────────────────
            if not m1_bars:
                continue

            events, action, new_sl, should_close = monitor.check_live_dual(
                trade_id=          str(ticket),
                direction=         state.direction,
                entry=             state.entry,
                current_sl=        state.current_sl,
                original_sl=       state.original_sl,
                take_profit=       state.take_profit,
                current_m1=        m1_bars,
                current_m5=        state.m5_window,
                opposing_zones=    state.opposing_zones,
                mfe_price=         state.mfe_price,
                m1_bars_since_mfe= state.m1_no_mfe,
                m5_bars_since_mfe= state.m5_no_mfe,
                new_m5_bar=        new_m5_bar,
                recent_signals=    state.recent_signals,
            )

            # Update rolling signal window
            if events:
                state.recent_signals.extend(
                    (ev.signal_type, ev.signal_time) for ev in events
                )
                # Trim to last CONFIRM_WINDOW_M5 × 5 minutes
                win_mins = getattr(config, "MONITOR_CONFIRM_WINDOW_M5", 3) * 5
                cutoff   = now - timedelta(minutes=win_mins)
                state.recent_signals = [
                    (st, t) for st, t in state.recent_signals if t >= cutoff
                ]

            # Log every event
            for ev in events:
                logger.info(
                    "MONITOR [%s] ticket=%s | %s (%s) | "
                    "MFE=%.1f pips | pips_to_TP=%.1f | %s",
                    symbol, ticket, ev.signal_type, ev.timeframe_source,
                    ev.mfe_pips_at_signal, ev.pips_to_tp, ev.detail,
                )

            # ── Act on monitor decision ───────────────────────────
            if action == "close_now" and should_close:
                logger.warning(
                    "MONITOR CLOSE | %s ticket=%s | conviction threshold met. "
                    "Closing early at %.5f. MFE was %.1f pips.",
                    symbol, ticket, current_price,
                    pip_calc.price_to_pips(abs(state.mfe_price - state.entry)),
                )
                if self._tr.close_trade(ticket):
                    del self._trade_states[ticket]

            elif action == "trail_sl" and new_sl != state.current_sl:
                # Trail SL to swing structure — NOT to entry
                logger.info(
                    "MONITOR TRAIL | %s ticket=%s | "
                    "SL %.5f → %.5f (swing-based, %.1f pips better)",
                    symbol, ticket, state.current_sl, new_sl,
                    pip_calc.price_to_pips(abs(new_sl - state.original_sl)),
                )
                if self._tr.modify_sl(ticket, new_sl):
                    state.current_sl = new_sl
                    state.sl_trailed = True

    # ── Helpers ───────────────────────────────────────────────────

    def _prune_closed_positions(self) -> None:
        """Remove state entries for positions no longer open in MT5."""
        if not self._trade_states:
            return
        open_tickets = {
            p.mt5_ticket or int(p.id)
            for p in self._tr.get_open_positions()
        }
        stale = [t for t in self._trade_states if t not in open_tickets]
        for t in stale:
            del self._trade_states[t]
            logger.debug("Monitor state pruned for closed ticket %s", t)

    def _build_trade(
        self,
        signal:            TradeSignal,
        broker_cost:       BrokerCost,
        pip_calc:          PipCalculator,
        tick_value:        float,
        risk_pct_override: Optional[float] = None,
    ) -> Optional[Trade]:
        balance = self._tr.get_account_balance()
        if balance <= 0:
            logger.error("Balance is 0 — cannot size trade.")
            return None

        effective_risk = risk_pct_override if risk_pct_override is not None \
            else self._risk_pct

        risk_params = RiskParameters(
            account_balance=    balance,
            risk_percent=       effective_risk,
            commission_per_lot= self._commission,
            min_rr_ratio=       self._min_rr,
            account_currency=   self._currency,
        )
        sl_pips  = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
        lot_size = max(0.01, round(
            risk_params.lot_size(sl_pips, tick_value, pip_calc), 2))

        logger.info(
            "Sizing | balance=%.0f %s | risk=%.2f%% | SL=%.1f pips | lots=%.2f",
            balance, self._currency, effective_risk, sl_pips, lot_size,
        )

        return Trade(
            id=          str(uuid.uuid4()),
            symbol=      signal.symbol,
            direction=   signal.direction,
            entry_price= signal.entry_price,
            stop_loss=   signal.stop_loss,
            take_profit= signal.take_profit,
            lot_size=    lot_size,
            status=      TradeStatus.PENDING,
            created_at=  datetime.now(tz=timezone.utc),
            commission=  self._commission * lot_size,
            spread_cost= broker_cost.spread_pips,
        )