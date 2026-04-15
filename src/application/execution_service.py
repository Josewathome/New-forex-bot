"""
execution_service.py — Phase 3 + 3.5: Entry + Open Trade Guardian  (v7 — Adaptive TP, Scoring, Time Exit)
"""
from __future__ import annotations
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import src.config as config
from src.domain.entities import (
    Direction, LiquidityPool, Timeframe, Trade, TradeSignal, TradeStatus, Zone,ZoneType, Candle
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
    compute_tp1_price,
    compute_tp2_price,
    get_initial_risk,
    check_min_sl_pips,
    compute_dynamic_tp,
    score_trade,
    dynamic_risk,
    is_strong_trend,
    is_kill_zone,
)
from src.application.news_manager import NewsManager
from src.application.trade_monitor import TradeMonitor, MonitorEvent, find_swing_trail_sl
from src.infrastructure.spread_calculator import SpreadCalculator

logger = logging.getLogger(__name__)
_M5 = timedelta(minutes=5)


@dataclass
class _OpenTradeState:
    ticket:          int
    symbol:          str
    direction:       Direction
    entry:           float
    original_sl:     float
    current_sl:      float
    take_profit:     float
    tp1_price:       Optional[float]
    tp2_price:       Optional[float]
    tp1_hit:         bool = False
    tp2_hit:         bool = False
    lot_size:        float = 0.0
    initial_risk:    float = 0.0
    mfe_price:       float = 0.0
    m1_no_mfe:       int = 0
    m5_no_mfe:       int = 0
    last_m5_time:    Optional[datetime] = None
    m5_window:       List = field(default_factory=list)
    opposing_zones:  List = field(default_factory=list)
    recent_signals:  List = field(default_factory=list)
    sl_trailed:      bool = False
    created_at:      Optional[datetime] = None


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
        self._trade_states: Dict[int, _OpenTradeState] = {}

    def run_zone_mapping(self, symbol: str) -> None:
        h1 = self._md.get_candles(symbol, Timeframe.H1, config.H1_CANDLE_COUNT)
        if not h1:
            return
        zones, pool = self._zone_mapper.map_zones(h1, symbol, Timeframe.H1)
        for old in self._zr.get_active_zones(symbol):
            self._zr.invalidate_zone(old.id)
        for z in zones:
            self._zr.save_zone(z)
        if pool:
            self._zr.save_liquidity_pool(pool)

    def run_monitoring_cycle(self, symbol: str) -> Optional[Trade]:
        now = datetime.now(tz=timezone.utc)
        self._prune_closed_positions()

        if getattr(config, "MONITOR_ENABLED", True):
            self._monitor_open_trades(symbol, now)

        if self._news.is_blocked(symbol, now):
            return None
        if not is_in_trading_session(now):
            return None

        broker_cost = self._spread.get_broker_cost(symbol)
        # Fetch 100 H1 bars: 14 for ATR warmup + 50 for baseline_atr + headroom.
        # Previously only 20 were fetched, making the >=50 baseline check always false
        # and leaving compute_dynamic_tp() permanently locked to the static ratios.
        h1_short    = self._md.get_candles(symbol, Timeframe.H1, 100)
        h1_atr      = atr(h1_short) if h1_short else 0.0
        digits      = self._md.get_symbol_digits(symbol)
        pip_calc    = PipCalculator(digits=digits)
        tick_value  = self._md.get_tick_value(symbol)

        if h1_short and self._news.broker_cost_exceeds_atr(
                symbol, h1_atr, broker_cost, pip_calc):
            return None

        h4 = self._md.get_candles(symbol, Timeframe.H4, config.H4_CANDLE_COUNT)
        if not h4:
            return None

        h4_bias             = find_h4_bias(h4)
        _, _, h4_eq         = compute_h4_equilibrium(h4)
        ask                 = self._md.get_current_price(symbol)
        if ask == 0.0:
            return None

        zone_buf = calculate_zone_buffer(h1_atr)
        zones    = self._zr.get_active_zones(symbol)
        pool     = self._zr.get_liquidity_pool(symbol)
        if not pool:
            return None

        sym_min_score = config.SYMBOL_MIN_SCORE.get(symbol, config.SETUP_SCORE_THRESHOLD)

        for zone in zones:
            setup_score = score_setup(zone.direction, h4_bias, ask, h4_eq)
            if setup_score < sym_min_score:
                logger.debug("SCORE BELOW SYM MIN | %s | %d < %d",
                             symbol, setup_score, sym_min_score)
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
                atr_price=h1_atr, zone_buffer=zone_buf,
                min_rr_override=min_rr,
            )
            if signal is None:
                continue

            sl_pips = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
            if not check_min_sl_pips(symbol, sl_pips):
                logger.info("REJECTED | %s | SL=%.1f pips below minimum", symbol, sl_pips)
                continue

            open_count = sum(1 for t in self._tr.get_open_positions() if t.symbol == symbol)
            if open_count >= config.MAX_OPEN_TRADES:
                continue

            # ── Trade Scoring ─────────────────────────────────────
            htf_aligned = (h4_bias == zone.direction)
            liquidity_sweep = (zone.zone_type == ZoneType.ORDER_BLOCK and zone.touch_count == 0)
            fvg_clean = (zone.zone_type == ZoneType.FAIR_VALUE_GAP and zone.size > h1_atr * 0.5)
            strong_displacement = (signal.volume_ratio >= 1.5)
            in_kill_zone = is_kill_zone(signal.created_at)

            trade_score = score_trade(
                htf_aligned, liquidity_sweep, fvg_clean,
                strong_displacement, in_kill_zone
            )
            if trade_score < config.TRADE_SCORE_THRESHOLD:
                logger.info("REJECTED | %s | low trade score %d < %d",
                            symbol, trade_score, config.TRADE_SCORE_THRESHOLD)
                continue

            # ── Dynamic Risk ──────────────────────────────────────
            risk_pct = dynamic_risk(trade_score)

            # ── Dynamic TP ────────────────────────────────────────
            baseline_atr = atr(h1_short[-50:]) if len(h1_short) >= 50 else h1_atr
            tp1_ratio, tp2_ratio = compute_dynamic_tp(
                signal.direction, signal.entry_price, signal.stop_loss,
                h1_atr, baseline_atr
            )
            tp1_price = (signal.entry_price + tp1_ratio * abs(signal.entry_price - signal.stop_loss)
                         if signal.direction == Direction.BULLISH
                         else signal.entry_price - tp1_ratio * abs(signal.entry_price - signal.stop_loss))
            tp2_price = (signal.entry_price + tp2_ratio * abs(signal.entry_price - signal.stop_loss)
                         if signal.direction == Direction.BULLISH
                         else signal.entry_price - tp2_ratio * abs(signal.entry_price - signal.stop_loss))

            trade = self._build_trade(signal, broker_cost, pip_calc, tick_value,
                                      risk_pct_override=risk_pct,
                                      tp1_price=tp1_price, tp2_price=tp2_price)
            if trade is None:
                continue

            ticket = self._tr.place_trade(trade)
            if ticket is None:
                logger.error("Trade placement failed for %s", symbol)
                continue

            trade.mt5_ticket = ticket
            trade.status = TradeStatus.OPEN
            trade.initial_risk = get_initial_risk(signal.entry_price, signal.stop_loss)
            trade.remaining_volume = trade.lot_size

            self._trade_states[ticket] = _OpenTradeState(
                ticket=ticket,
                symbol=symbol,
                direction=signal.direction,
                entry=signal.entry_price,
                original_sl=signal.stop_loss,
                current_sl=signal.stop_loss,
                take_profit=signal.take_profit,
                tp1_price=tp1_price,
                tp2_price=tp2_price,
                lot_size=trade.lot_size,
                initial_risk=trade.initial_risk,
                mfe_price=signal.entry_price,
                created_at=datetime.now(tz=timezone.utc),
            )

            logger.info(
                "TRADE ✓ | %s %s | grade=%s | score=%d | "
                "entry=%.5f SL=%.5f TP1=%.5f TP2=%.5f TP_final=%.5f | "
                "lot=%.2f | RR=%.2f",
                symbol, signal.direction.value, grade, trade_score,
                signal.entry_price, signal.stop_loss,
                tp1_price or 0, tp2_price or 0, signal.take_profit,
                trade.lot_size, signal.risk_reward,
            )
            return trade

        return None

    def _monitor_open_trades(self, symbol: str, now: datetime) -> None:
        open_positions = [p for p in self._tr.get_open_positions() if p.symbol == symbol]
        if not open_positions:
            return

        m1_bars = self._md.get_candles(symbol, Timeframe.M1, 30)
        m5_bars = self._md.get_candles(symbol, Timeframe.M5, 15)
        h1_bars = self._md.get_candles(symbol, Timeframe.H1, 20)
        zones   = self._zr.get_active_zones(symbol)
        digits  = self._md.get_symbol_digits(symbol)
        pip_calc = PipCalculator(digits=digits)
        h1_atr   = atr(h1_bars) if h1_bars else 0.0
        monitor  = TradeMonitor(pip_calc=pip_calc, atr_price=h1_atr, symbol=symbol)

        for pos in open_positions:
            ticket = pos.mt5_ticket or int(pos.id)

            if ticket not in self._trade_states:
                tp1 = compute_tp1_price(pos.direction, pos.entry_price, pos.stop_loss)
                tp2 = compute_tp2_price(pos.direction, pos.entry_price, pos.stop_loss)
                self._trade_states[ticket] = _OpenTradeState(
                    ticket=ticket, symbol=symbol, direction=pos.direction,
                    entry=pos.entry_price, original_sl=pos.stop_loss,
                    current_sl=pos.stop_loss, take_profit=pos.take_profit,
                    tp1_price=tp1, tp2_price=tp2,
                    lot_size=pos.lot_size,
                    initial_risk=get_initial_risk(pos.entry_price, pos.stop_loss),
                    mfe_price=pos.entry_price,
                    created_at=pos.created_at,
                )

            state = self._trade_states[ticket]
            price = self._md.get_current_price(symbol)
            if price <= 0:
                continue

            # Update MFE
            if state.direction == Direction.BULLISH:
                if price > state.mfe_price:
                    state.mfe_price = price
                    state.m1_no_mfe = 0
                    state.m5_no_mfe = 0
                else:
                    state.m1_no_mfe += 1
            else:
                if price < state.mfe_price:
                    state.mfe_price = price
                    state.m1_no_mfe = 0
                    state.m5_no_mfe = 0
                else:
                    state.m1_no_mfe += 1

            # Detect new M5 bar
            new_m5 = False
            if m5_bars:
                latest_m5 = m5_bars[-1].time
                if state.last_m5_time is None or latest_m5 > state.last_m5_time:
                    new_m5 = True
                    state.last_m5_time = latest_m5
                    state.m5_window.append(m5_bars[-1])
                    if len(state.m5_window) > 12:
                        state.m5_window.pop(0)
                    if state.m1_no_mfe > 0:
                        state.m5_no_mfe += 1
                    else:
                        state.m5_no_mfe = 0

            # Cache opposing zones
            if not state.opposing_zones and zones:
                state.opposing_zones = monitor.scan_opposing_zones(
                    state.direction, state.entry, state.take_profit, zones)

            # ── TP1 handling ──────────────────────────────────────
            if (state.tp1_price and not state.tp1_hit):
                tp1_reached = (
                    (state.direction == Direction.BULLISH and price >= state.tp1_price) or
                    (state.direction == Direction.BEARISH and price <= state.tp1_price)
                )
                if tp1_reached:
                    close_pct = getattr(config, "TIERED_TP1_CLOSE_PCT", 0.50)
                    # Floor to avoid closing more lots than intended (round() can overshoot)
                    close_vol = max(0.01, math.floor(state.lot_size * close_pct * 100) / 100)
                    logger.info(
                        "TP1 HIT | %s ticket=%s | closing %.2f lots @ ~%.5f",
                        symbol, ticket, close_vol, price,
                    )
                    closed = self._tr.partial_close_trade(ticket, close_vol)
                    if closed:
                        state.tp1_hit = True
                        state.lot_size -= close_vol
                        profit_lock_dist = state.initial_risk * 0.3
                        if state.direction == Direction.BULLISH:
                            new_sl = state.entry + profit_lock_dist
                        else:
                            new_sl = state.entry - profit_lock_dist
                        self._tr.modify_sl(ticket, new_sl)
                        state.current_sl = new_sl
                        logger.info("Profit‑lock SL moved to %.5f (entry + 0.3R)", new_sl)
                    else:
                        logger.error("Partial close failed for TP1 ticket=%s", ticket)

            # ── TP2 handling ──────────────────────────────────────
            elif state.tp1_hit and not state.tp2_hit and state.tp2_price:
                tp2_reached = (
                    (state.direction == Direction.BULLISH and price >= state.tp2_price) or
                    (state.direction == Direction.BEARISH and price <= state.tp2_price)
                )
                if tp2_reached:
                    close_pct = getattr(config, "TIERED_TP2_CLOSE_PCT", 0.25)
                    # Floor to avoid closing more lots than intended
                    close_vol = max(0.01, math.floor(state.lot_size * close_pct * 100) / 100)
                    logger.info(
                        "TP2 HIT | %s ticket=%s | closing %.2f lots @ ~%.5f",
                        symbol, ticket, close_vol, price,
                    )
                    closed = self._tr.partial_close_trade(ticket, close_vol)
                    if closed:
                        state.tp2_hit = True
                        state.lot_size -= close_vol

            # ── Monitor checks ────────────────────────────────────
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
                new_m5_bar=        new_m5,
                recent_signals=    state.recent_signals,
            )

            if events:
                win_mins = getattr(config, "MONITOR_CONFIRM_WINDOW_M5", 3) * 5
                cutoff   = now - timedelta(minutes=win_mins)
                state.recent_signals.extend((ev.signal_type, ev.signal_time) for ev in events)
                state.recent_signals = [(st, t) for st, t in state.recent_signals if t >= cutoff]

            for ev in events:
                logger.info("MONITOR [%s] ticket=%s | %s (%s) | MFE=%.1fp | left=%.1fp | %s",
                            symbol, ticket, ev.signal_type, ev.timeframe_source,
                            ev.mfe_pips_at_signal, ev.pips_to_tp, ev.detail)

            # ── Stall exit condition (only after TP1, not strong trend) ──
            # Stall exit condition (only after TP1, not strong trend, and profit > min ratio)
            stall_event = next((e for e in events if e.signal_type == "mfe_stall"), None)
            if stall_event and getattr(config, "STALL_EXIT_REQUIRES_TP1", True):
                current_price = self._md.get_current_price(symbol)
                sl_dist = abs(state.entry - state.original_sl)
                if sl_dist > 0:
                    current_profit_ratio = abs(current_price - state.entry) / sl_dist
                else:
                    current_profit_ratio = 0
                min_profit_ratio = getattr(config, "MIN_PROFIT_RATIO_TO_CLOSE", 1.5)
                if state.tp1_hit and not self._is_strong_trend(state.m5_window, state.direction) and current_profit_ratio >= min_profit_ratio:
                    should_close = True
                    action = "close_now"
                    logger.info("Stall exit after TP1, profit=%.1fR", current_profit_ratio)

            # ── Time‑based exit ───────────────────────────────────
            if state.created_at:
                duration = (now - state.created_at).total_seconds() / 60
                if duration > config.MAX_TRADE_DURATION_MINUTES:
                    should_close = True
                    action = "close_now"
                    logger.info("Time‑based exit after %.0f minutes", duration)

            if action == "close_now" and should_close:
                logger.warning("MONITOR CLOSE | %s ticket=%s | Closing early. MFE=%.1fp",
                               symbol, ticket,
                               pip_calc.price_to_pips(abs(state.mfe_price - state.entry)))
                if self._tr.close_trade(ticket):
                    self._log_trade_summary(pos, state, pip_calc)
                    del self._trade_states[ticket]

            elif action == "trail_sl" and new_sl != state.current_sl:
                logger.info("MONITOR TRAIL | %s ticket=%s | SL %.5f → %.5f",
                            symbol, ticket, state.current_sl, new_sl)
                if self._tr.modify_sl(ticket, new_sl):
                    state.current_sl = new_sl
                    state.sl_trailed = True

    def _is_strong_trend(self, m5_window: List[Candle], direction: Direction) -> bool:
        if len(m5_window) < 4:
            return False
        last_three = m5_window[-3:]
        if direction == Direction.BULLISH:
            return (last_three[0].close > last_three[0].open and
                    last_three[1].close > last_three[0].high and
                    last_three[2].close > last_three[1].high)
        else:
            return (last_three[0].close < last_three[0].open and
                    last_three[1].close < last_three[0].low and
                    last_three[2].close < last_three[1].low)

    def _log_trade_summary(self, trade: Trade, state: _OpenTradeState, pip_calc: PipCalculator) -> None:
        if trade.initial_risk <= 0:
            return
        sl_pips = pip_calc.price_to_pips(trade.initial_risk)
        mfe_pips = pip_calc.price_to_pips(abs(state.mfe_price - trade.entry_price))
        realized_pnl_pips = (trade.realized_pnl / trade.initial_risk) * sl_pips if trade.initial_risk > 0 else 0
        efficiency = (realized_pnl_pips / mfe_pips * 100) if mfe_pips > 0 else 0
        logger.info(
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"TRADE SUMMARY\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Symbol: {trade.symbol}\n"
            f"Entry: {trade.entry_price:.5f}\n"
            f"SL: {trade.stop_loss:.5f}\n"
            f"TP1 hit: {trade.tp1_hit}\n"
            f"TP2 hit: {trade.tp2_hit}\n"
            f"Max Profit (MFE): {mfe_pips:.1f} pips\n"
            f"Captured Profit: {realized_pnl_pips:.1f} pips\n"
            f"Efficiency: {efficiency:.1f}%%\n"
            f"Exit Reason: {trade.exit_reason}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    def _prune_closed_positions(self) -> None:
        if not self._trade_states:
            return
        open_tickets = {p.mt5_ticket or int(p.id) for p in self._tr.get_open_positions()}
        stale = [t for t in self._trade_states if t not in open_tickets]
        for t in stale:
            del self._trade_states[t]

    def _build_trade(self, signal, broker_cost, pip_calc, tick_value,
                     risk_pct_override=None, tp1_price=None, tp2_price=None):
        balance = self._tr.get_account_balance()
        if balance <= 0:
            return None
        eff_risk = risk_pct_override if risk_pct_override is not None else self._risk_pct
        risk_params = RiskParameters(
            account_balance=balance, risk_percent=eff_risk,
            commission_per_lot=self._commission,
            min_rr_ratio=self._min_rr, account_currency=self._currency,
        )
        sl_pips = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
        # Floor (not round) to 2 decimal places so we never exceed the calculated risk.
        # round() would push e.g. 0.125 → 0.13 lots, overspending by ~4%.
        lot_size = max(0.01, math.floor(risk_params.lot_size(sl_pips, tick_value, pip_calc) * 100) / 100)

        # Use provided TP1/TP2 if given, else compute from signal
        final_tp = signal.take_profit
        if tp1_price is not None and tp2_price is not None:
            # For live, we set TP to TP2 (full target) and handle TP1 via monitor
            pass

        return Trade(
            id=          str(uuid.uuid4()),
            symbol=      signal.symbol,
            direction=   signal.direction,
            entry_price= signal.entry_price,
            stop_loss=   signal.stop_loss,
            take_profit= final_tp,
            lot_size=    lot_size,
            status=      TradeStatus.PENDING,
            created_at=  datetime.now(tz=timezone.utc),
            commission=  self._commission * lot_size,
            spread_cost= broker_cost.spread_pips,
        )