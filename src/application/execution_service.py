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
    find_m1_predictive_signal,
    find_h4_bias,
    find_d1_bias,
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
    evaluate_zone_health,
    find_opposing_opportunity,
    find_htf_zone_confluence,
    find_m30_zone_confluence,
    find_m5_zone_confluence,
    read_m5_zone_story,
    read_m30_zone_story,
    find_m5_precision_entry,
    get_current_m5_bar_open,
)
from src.application.news_manager import NewsManager
from src.application.trade_monitor import TradeMonitor, MonitorEvent, find_swing_trail_sl, find_runner_protection_sl
from src.infrastructure.spread_calculator import SpreadCalculator
from src.infrastructure.trade_journal import TradeJournal

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
    accumulated_pips: float = 0.0   # pips banked from TP1/TP2 partial closes
    digits:           int   = 5     # symbol digit count for PipCalculator reconstruction
    grade:            str   = "?"   # setup grade recorded in the journal


@dataclass
class EntryCandidate:
    """All data needed to execute a trade, collected during evaluation."""
    symbol:      str
    signal:      TradeSignal
    zone:        Zone
    setup_score: int
    trade_score: int
    grade:       str
    risk_pct:    float
    tp1_price:   float
    tp2_price:   Optional[float]
    broker_cost: BrokerCost
    pip_calc:    PipCalculator
    tick_value:  float
    digits:      int
    h1_atr:      float
    is_phase2:   bool = False


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
        journal:           Optional[TradeJournal] = None,
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
        self._journal     = journal
        self._zone_mapper = ZoneMapper()
        self._trade_states: Dict[int, _OpenTradeState] = {}
        self._loss_streak:  Dict[str, int]      = {}
        self._streak_pause: Dict[str, datetime] = {}
        # M30 and M5 zones are stored in lightweight per-symbol dicts here
        # rather than in the zone_repo (which holds H1 zones used for entry).
        # They are read-only during signal evaluation — only replaced on each
        # M30/M5 bar close.  Dict key = symbol, value = List[Zone].
        self._m30_zones: Dict[str, List[Zone]] = {}
        self._m5_zones:  Dict[str, List[Zone]] = {}

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

    def run_m30_zone_mapping(self, symbol: str) -> None:
        """
        Re-map M30 OBs and FVGs on every 30-minute bar close.

        Results are stored in self._m30_zones[symbol] — a lightweight
        in-memory list that is read during signal evaluation to check
        whether the active H1 zone has M30 confirmation.  No zone_repo
        writes: M30 zones are supplementary context, not entry zones.
        """
        if not getattr(config, "M30_ZONE_MAPPING_ENABLED", True):
            return
        m30 = self._md.get_candles(symbol, Timeframe.M30,
                                   getattr(config, "M30_CANDLE_COUNT", 60))
        if not m30:
            return
        zones, _ = self._zone_mapper.map_zones(m30, symbol, Timeframe.M30)
        self._m30_zones[symbol] = zones
        logger.debug("M30 zones remapped | %s | %d zones", symbol, len(zones))

    def run_m5_zone_mapping(self, symbol: str) -> None:
        """
        Re-map M5 OBs and FVGs on every 5-minute bar close.

        Results are stored in self._m5_zones[symbol].  M5 zones are the
        tightest structural layer — when an M5 OB/FVG sits inside the H1
        zone at the moment price arrives, the level has very recent bar-level
        confirmation and the M1 entry is higher probability.
        """
        if not getattr(config, "M5_ZONE_MAPPING_ENABLED", True):
            return
        m5 = self._md.get_candles(symbol, Timeframe.M5, config.M5_CANDLE_COUNT)
        if not m5:
            return
        zones, _ = self._zone_mapper.map_zones(m5, symbol, Timeframe.M5)
        self._m5_zones[symbol] = zones
        logger.debug("M5 zones remapped | %s | %d zones", symbol, len(zones))

    # ── Phase helpers ─────────────────────────────────────────────────

    def open_count_for_symbol(self, symbol: str) -> int:
        return sum(1 for p in self._tr.get_open_positions() if p.symbol == symbol)

    def has_tp1_hit_for_symbol(self, symbol: str) -> bool:
        """True when at least one open trade on symbol has hit TP1 (proven winner)."""
        return any(
            state.tp1_hit
            for state in self._trade_states.values()
            if state.symbol == symbol
        )

    # ── Phase 1: monitoring ───────────────────────────────────────────

    def run_monitoring_only(self, symbol: str) -> None:
        """Prune closed positions and run the trade-monitor for symbol."""
        now = datetime.now(tz=timezone.utc)
        self._prune_closed_positions()
        if getattr(config, "MONITOR_ENABLED", True):
            self._monitor_open_trades(symbol, now)

    # ── Phase 1: signal evaluation (no execution) ─────────────────────

    def collect_entry_candidate(
        self, symbol: str, is_phase2: bool = False
    ) -> Optional[EntryCandidate]:
        """
        Evaluate all zones for symbol and return the highest-qualifying
        EntryCandidate without placing a trade.  Returns None when no
        signal qualifies or preconditions (news / session / data) fail.
        """
        now = datetime.now(tz=timezone.utc)

        if self._news.is_blocked(symbol, now):
            return None
        if not is_in_trading_session(now):
            return None

        # Streak protection: skip entry if we hit MAX_CONSECUTIVE_LOSSES recently
        pause_until = self._streak_pause.get(symbol)
        if pause_until and now < pause_until:
            logger.debug("streak_pause | %s | blocked until %s", symbol, pause_until.isoformat())
            return None

        broker_cost = self._spread.get_broker_cost(symbol)
        # 100 H1 bars: 14 for ATR warmup + 50 for baseline_atr + headroom.
        h1_short   = self._md.get_candles(symbol, Timeframe.H1, 100)
        h1_atr     = atr(h1_short) if h1_short else 0.0
        digits     = self._md.get_symbol_digits(symbol)
        pip_calc   = PipCalculator(digits=digits)
        tick_value = self._md.get_tick_value(symbol)

        if h1_short and self._news.broker_cost_exceeds_atr(
                symbol, h1_atr, broker_cost, pip_calc):
            return None

        h4 = self._md.get_candles(symbol, Timeframe.H4, config.H4_CANDLE_COUNT)
        if not h4:
            return None

        h4_bias         = find_h4_bias(h4)
        _, _, h4_eq     = compute_h4_equilibrium(h4)

        # H4 zone map — OBs/FVGs on H4 for confluence scoring
        h4_zones, _ = self._zone_mapper.map_zones(h4, symbol, Timeframe.H4) if len(h4) >= 20 else ([], None)

        # ── D1 macro trend ─────────────────────────────────────────
        d1         = self._md.get_candles(symbol, Timeframe.D1, config.D1_CANDLE_COUNT)
        d1_bias    = find_d1_bias(d1) if d1 else None
        ask             = self._md.get_current_price(symbol)
        if ask == 0.0:
            return None

        zone_buf = calculate_zone_buffer(h1_atr)
        zones    = self._zr.get_active_zones(symbol)
        pool     = self._zr.get_liquidity_pool(symbol)
        if not pool:
            return None

        sym_min_score = config.SYMBOL_MIN_SCORE.get(symbol, config.SETUP_SCORE_THRESHOLD)
        baseline_atr  = atr(h1_short[-50:]) if len(h1_short) >= 50 else h1_atr

        for zone in zones:
            # ── Trend pre-filters ──────────────────────────────────
            # Block entries when H4 has no clear direction — ranging
            # markets produce low-quality setups that commission destroys.
            if getattr(config, "REQUIRE_H4_BIAS", True) and h4_bias is None:
                logger.debug("H4 RANGING | %s | no trend — skipping", symbol)
                break  # same for every zone on this symbol; no need to loop

            # Block setups that go against the D1 macro trend.
            # Full hard-block if BLOCK_COUNTER_D1=True.
            # When False, counter-D1 trades still require a minimum score
            # of COUNTER_D1_MIN_SCORE (default 7) — they must have H4+H4-zone
            # confluence to compensate for trading against the daily trend.
            if d1_bias is not None and d1_bias != zone.direction:
                if getattr(config, "BLOCK_COUNTER_D1", False):
                    logger.debug("COUNTER-D1 HARD BLOCK | %s | zone=%s d1=%s",
                                 symbol, zone.direction.value, d1_bias.value)
                    continue
                # Soft gate: pre-score the setup to check if it clears the bar
                _pre_h4c  = find_htf_zone_confluence(zone, h4_zones)
                _pre_m30c = find_m30_zone_confluence(zone, self._m30_zones.get(symbol, []))
                _pre_m5c  = find_m5_zone_confluence(zone,  self._m5_zones.get(symbol,  []))
                _pre_score = score_setup(
                    zone.direction, h4_bias, ask, h4_eq, d1_bias,
                    _pre_h4c, _pre_m30c, _pre_m5c,
                )
                _counter_d1_min = getattr(config, "COUNTER_D1_MIN_SCORE", 7)
                if _pre_score < _counter_d1_min:
                    logger.debug(
                        "COUNTER-D1 SOFT BLOCK | %s | zone=%s d1=%s score=%d < %d",
                        symbol, zone.direction.value, d1_bias.value,
                        _pre_score, _counter_d1_min,
                    )
                    continue

            h4_confluent  = find_htf_zone_confluence(zone, h4_zones)
            m30_confluent = find_m30_zone_confluence(zone, self._m30_zones.get(symbol, []))
            m5_confluent  = find_m5_zone_confluence(zone,  self._m5_zones.get(symbol,  []))

            setup_score = score_setup(
                zone.direction, h4_bias, ask, h4_eq, d1_bias,
                h4_confluent, m30_confluent, m5_confluent,
            )
            if setup_score < sym_min_score:
                logger.debug("SCORE BELOW SYM MIN | %s | %d < %d (H4=%s M30=%s M5=%s)",
                             symbol, setup_score, sym_min_score,
                             h4_confluent, m30_confluent, m5_confluent)
                continue

            # grade / min_rr used for ChoCh RR gate; risk_pct overwritten by dynamic_risk()
            grade, _, min_rr = grade_setup(setup_score)

            if not (ask <= zone.high + zone_buf and ask >= zone.low - zone_buf):
                continue
            zone.mark_tapped()

            m5 = self._md.get_candles(symbol, Timeframe.M5, config.M5_CANDLE_COUNT)
            if not m5:
                continue

            # Initialise story/precision state so references are always valid
            # even when the healthy-branch is skipped (zone flip path).
            precision_zone = None
            m5_story       = "neutral"

            # Re-evaluate zone at the moment price arrives.
            healthy, reject_reason = evaluate_zone_health(
                zone, h1_short, m5, h1_atr)

            if not healthy:
                if reject_reason in ("m5_momentum_against_zone", "m5_zone_distributing"):
                    # Institutional pressure is moving AGAINST our zone.
                    # Try to trade WITH that institution instead of fighting it.
                    opp = find_opposing_opportunity(
                        rejected_zone=zone,
                        m5_candles=m5,
                        pip_calc=pip_calc,
                        spread_pips=broker_cost.spread_pips,
                        liquidity_pool=pool,
                        h1_atr=h1_atr,
                        zone_buffer=zone_buf,
                        min_rr=min_rr,
                    )
                    if opp is None:
                        logger.info("ZONE FLIP | %s %s | %s — no opposing setup",
                                    symbol, zone.id[:8], reject_reason)
                        continue
                    signal, zone = opp   # trade the reversal instead
                else:
                    logger.info("ZONE STALE | %s %s | %s — skipping",
                                symbol, zone.id[:8], reject_reason)
                    continue
            else:
                # ── M30 story gate ────────────────────────────────
                # Read what the M30 bars are saying about this H1 zone
                # BEFORE looking for entries.  A M30 bar closing against
                # the zone direction is a 30-minute institutional statement
                # that outweighs any M5 or M1 signal we might find.
                m30_bars = self._md.get_candles(symbol, Timeframe.M30,
                                                getattr(config, "M30_STORY_LOOKBACK", 8))
                if m30_bars:
                    m30_story = read_m30_zone_story(zone, m30_bars, h1_atr)
                    if m30_story == "against":
                        logger.info(
                            "M30 STORY AGAINST | %s %s | M30 bar closed against zone — skip",
                            symbol, zone.id[:8],
                        )
                        continue

                # ── M5 zone story gate ────────────────────────────
                # Check the sub-bar story INSIDE the zone using M5 bars.
                # If M5 bars are distributing (closing lower and lower with
                # volume) the zone is being used as supply, not demand.
                # This is a finer check than evaluate_zone_health check 5
                # because it uses all M5 bars inside the zone, not just the
                # last 4 recent ones.
                m5_story = read_m5_zone_story(zone, m5, h1_atr)
                if m5_story == "distributing":
                    logger.info(
                        "M5 STORY DISTRIBUTING | %s %s | M5 bars showing distribution — skip",
                        symbol, zone.id[:8],
                    )
                    continue

                # ── M5 precision entry zone ───────────────────────
                # Find the M5 OB / FVG INSIDE the H1 zone — this is the
                # real institutional cluster, not the H1 zone boundary.
                # When found, it is passed to both signal finders so the
                # SL is anchored at the M5 OB low rather than the H1 OB low.
                precision_zone = find_m5_precision_entry(zone, m5, pip_calc, h1_atr)
                if precision_zone:
                    logger.debug(
                        "M5 PRECISION ZONE | %s | %.5f–%.5f inside H1 zone %.5f–%.5f",
                        symbol, precision_zone[0], precision_zone[1], zone.low, zone.high,
                    )

                # ── M1 Predictive Signal (primary) ────────────────
                m1 = self._md.get_candles(symbol, Timeframe.M1, config.M1_CANDLE_COUNT)
                current_m5_candle = m5[-1] if m5 else None
                signal = None

                if m1:
                    signal = find_m1_predictive_signal(
                        m1_candles=     m1,
                        zone=           zone,
                        pip_calc=       pip_calc,
                        spread_pips=    broker_cost.spread_pips,
                        liquidity_pool= pool,
                        current_m5=     current_m5_candle,
                        atr_price=      h1_atr,
                        zone_buffer=    zone_buf,
                        min_rr_override=min_rr,
                        m30_zones=      self._m30_zones.get(symbol, []),
                        m5_candles=     m5,
                    )
                    if signal:
                        logger.info(
                            "M1 PREDICTIVE ENTRY | %s %s | RR=%.2f | "
                            "entry=%.5f SL=%.5f (zone.mid=%.5f) | "
                            "m5_story=%s precision=%s",
                            symbol, zone.direction.value,
                            signal.risk_reward, signal.entry_price,
                            signal.stop_loss, zone.mid,
                            m5_story,
                            f"{precision_zone[0]:.5f}-{precision_zone[1]:.5f}"
                            if precision_zone else "none",
                        )

                # ── M5 ChoCh Signal (fallback) ────────────────────
                if signal is None:
                    signal = find_m5_choch_signal(
                        m5_candles=m5, zone=zone, pip_calc=pip_calc,
                        spread_pips=broker_cost.spread_pips, liquidity_pool=pool,
                        atr_price=h1_atr, zone_buffer=zone_buf,
                        min_rr_override=min_rr,
                        m30_zones=self._m30_zones.get(symbol, []),
                        precision_zone=precision_zone,
                    )

                if signal is None:
                    continue

            sl_pips = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
            if not check_min_sl_pips(symbol, sl_pips):
                logger.info("REJECTED | %s | SL=%.1f pips below minimum", symbol, sl_pips)
                continue

            # ── Trade Scoring ──────────────────────────────────────
            # Use signal.direction (not zone.direction) so flipped trades score correctly.
            htf_aligned         = (h4_bias == signal.direction)
            liquidity_sweep     = (zone.zone_type == ZoneType.ORDER_BLOCK and zone.touch_count == 0)
            fvg_clean           = (zone.zone_type == ZoneType.FAIR_VALUE_GAP and zone.size > h1_atr * 0.5)
            strong_displacement = (signal.volume_ratio >= 1.5)
            in_kill_zone        = is_kill_zone(signal.created_at)

            trade_score = score_trade(
                htf_aligned, liquidity_sweep, fvg_clean,
                strong_displacement, in_kill_zone,
            )
            if trade_score < config.TRADE_SCORE_THRESHOLD:
                logger.info("REJECTED | %s | low trade score %d < %d",
                            symbol, trade_score, config.TRADE_SCORE_THRESHOLD)
                continue

            risk_pct = dynamic_risk(trade_score)

            tp1_ratio, tp2_ratio = compute_dynamic_tp(
                signal.direction, signal.entry_price, signal.stop_loss,
                h1_atr, baseline_atr,
            )
            tp1_price = (
                signal.entry_price + tp1_ratio * abs(signal.entry_price - signal.stop_loss)
                if signal.direction == Direction.BULLISH
                else signal.entry_price - tp1_ratio * abs(signal.entry_price - signal.stop_loss)
            )
            tp2_price = (
                signal.entry_price + tp2_ratio * abs(signal.entry_price - signal.stop_loss)
                if signal.direction == Direction.BULLISH
                else signal.entry_price - tp2_ratio * abs(signal.entry_price - signal.stop_loss)
            )
            # Clamp TP2 to the liquidity pool.  If TP2 ratio pushes beyond
            # the pool, MT5's full take_profit fires first — the partial close
            # never triggers.  Skip TP2 so the runner goes straight to the pool.
            if signal.direction == Direction.BULLISH and tp2_price >= signal.take_profit:
                tp2_price = None
            elif signal.direction == Direction.BEARISH and tp2_price <= signal.take_profit:
                tp2_price = None

            return EntryCandidate(
                symbol=symbol,      signal=signal,       zone=zone,
                setup_score=setup_score,                 trade_score=trade_score,
                grade=grade,        risk_pct=risk_pct,
                tp1_price=tp1_price, tp2_price=tp2_price,
                broker_cost=broker_cost, pip_calc=pip_calc,
                tick_value=tick_value,   digits=digits,
                h1_atr=h1_atr,      is_phase2=is_phase2,
            )

        return None

    # ── Phase 1 & 2: execution ────────────────────────────────────────

    def execute_entry_candidate(self, candidate: EntryCandidate) -> Optional[Trade]:
        """Build, margin-check, and place the trade for a pre-evaluated candidate."""
        symbol = candidate.symbol
        signal = candidate.signal

        trade = self._build_trade(
            signal, candidate.broker_cost, candidate.pip_calc,
            candidate.tick_value, risk_pct_override=candidate.risk_pct,
        )
        if trade is None:
            return None

        req_margin  = self._tr.get_required_margin(trade)
        free_margin = self._tr.get_free_margin()
        safety      = getattr(config, "MARGIN_SAFETY_FACTOR", 2.0)
        if req_margin is not None and free_margin < req_margin * safety:
            logger.warning(
                "MARGIN SKIP | %s | trade needs %.2f × %.1f safety = %.2f "
                "but free margin is only %.2f — skipping.",
                symbol, req_margin, safety, req_margin * safety, free_margin,
            )
            return None
        if req_margin is None:
            logger.debug("MARGIN CHECK unavailable for %s — proceeding (MT5 will enforce).", symbol)

        ticket = self._tr.place_trade(trade)
        if ticket is None:
            logger.error("Trade placement failed for %s", symbol)
            return None

        trade.mt5_ticket      = ticket
        trade.status          = TradeStatus.OPEN
        trade.initial_risk    = get_initial_risk(signal.entry_price, signal.stop_loss)
        trade.remaining_volume = trade.lot_size

        self._trade_states[ticket] = _OpenTradeState(
            ticket=ticket,
            symbol=symbol,
            direction=signal.direction,
            entry=signal.entry_price,
            original_sl=signal.stop_loss,
            current_sl=signal.stop_loss,
            take_profit=signal.take_profit,
            tp1_price=candidate.tp1_price,
            tp2_price=candidate.tp2_price,
            lot_size=trade.lot_size,
            initial_risk=trade.initial_risk,
            mfe_price=signal.entry_price,
            created_at=datetime.now(tz=timezone.utc),
            digits=candidate.digits,
            grade=candidate.grade,
        )
        if self._journal:
            try:
                self._journal.record_open(
                    ticket=ticket,    symbol=symbol,
                    direction=signal.direction.value,
                    entry=signal.entry_price, sl=signal.stop_loss,
                    tp=signal.take_profit,    lot_size=trade.lot_size,
                    grade=candidate.grade,
                )
            except Exception as exc:
                logger.warning("Journal record_open failed ticket=%s: %s", ticket, exc)

        phase_tag = "P2" if candidate.is_phase2 else "P1"
        logger.info(
            "TRADE ✓ [%s] | %s %s | grade=%s | score=%d | "
            "entry=%.5f SL=%.5f TP1=%.5f TP2=%.5f TP_final=%.5f | "
            "lot=%.2f | RR=%.2f",
            phase_tag, symbol, signal.direction.value, candidate.grade, candidate.trade_score,
            signal.entry_price, signal.stop_loss,
            candidate.tp1_price or 0, candidate.tp2_price or 0, signal.take_profit,
            trade.lot_size, signal.risk_reward,
        )
        return trade

    # ── Legacy wrapper (used by backtest / one-symbol callers) ────────

    def run_monitoring_cycle(self, symbol: str) -> Optional[Trade]:
        """Single-symbol wrapper: monitor + evaluate + execute in one call."""
        self.run_monitoring_only(symbol)
        if len(self._tr.get_open_positions()) >= config.MAX_OPEN_TRADES:
            return None
        if self.open_count_for_symbol(symbol) >= getattr(config, "MAX_TRADES_PER_SYMBOL", 2):
            return None
        candidate = self.collect_entry_candidate(symbol)
        if candidate is None:
            return None
        return self.execute_entry_candidate(candidate)

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
                # Clamp TP2 to not exceed MT5's take_profit (the liquidity pool).
                if tp2 is not None and pos.take_profit:
                    if pos.direction == Direction.BULLISH and tp2 >= pos.take_profit:
                        tp2 = None
                    elif pos.direction == Direction.BEARISH and tp2 <= pos.take_profit:
                        tp2 = None
                self._trade_states[ticket] = _OpenTradeState(
                    ticket=ticket, symbol=symbol, direction=pos.direction,
                    entry=pos.entry_price, original_sl=pos.stop_loss,
                    current_sl=pos.stop_loss, take_profit=pos.take_profit,
                    tp1_price=tp1, tp2_price=tp2,
                    lot_size=pos.lot_size,
                    initial_risk=get_initial_risk(pos.entry_price, pos.stop_loss),
                    mfe_price=pos.entry_price,
                    created_at=pos.created_at,
                    digits=digits,
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
                    if state.grade == "A+":
                        close_pct = getattr(config, "TIERED_TP1_CLOSE_PCT_A_PLUS", 0.40)
                    elif state.grade == "B":
                        close_pct = getattr(config, "TIERED_TP1_CLOSE_PCT_B", 0.50)
                    else:
                        close_pct = getattr(config, "TIERED_TP1_CLOSE_PCT", 0.60)
                    # Floor to avoid closing more lots than intended (round() can overshoot).
                    # If the resulting partial vol == the full position (lot too small to split),
                    # close everything and mark tp2_hit too so we don't attempt a phantom TP2.
                    computed_vol = math.floor(state.lot_size * close_pct * 100) / 100
                    close_vol = max(0.01, computed_vol)
                    if close_vol >= state.lot_size:
                        close_vol = state.lot_size  # full close — lot too small to split
                        state.tp2_hit = True         # suppress phantom TP2 attempt
                    logger.info(
                        "TP1 HIT | %s ticket=%s | closing %.2f lots @ ~%.5f",
                        symbol, ticket, close_vol, price,
                    )
                    closed = self._tr.partial_close_trade(ticket, close_vol, comment="ZoneBot TP1")
                    if closed:
                        state.tp1_hit = True
                        if state.tp1_price:
                            state.accumulated_pips += pip_calc.price_to_pips(
                                abs(state.tp1_price - state.entry))
                        remaining = self._tr.get_remaining_volume(ticket)
                        if remaining is not None:
                            state.lot_size = remaining
                        else:
                            logger.warning(
                                "TP1 volume fallback | ticket=%s | MT5 position not found after partial close "
                                "— using arithmetic estimate (%.2f - %.2f = %.2f lots)",
                                ticket, state.lot_size, close_vol, max(0.01, state.lot_size - close_vol),
                            )
                            state.lot_size = max(0.01, state.lot_size - close_vol)
                        profit_lock_dist = state.initial_risk * config.TP1_PROFIT_LOCK_R
                        if state.direction == Direction.BULLISH:
                            new_sl = state.entry + profit_lock_dist
                        else:
                            new_sl = state.entry - profit_lock_dist
                        self._tr.modify_sl(ticket, new_sl)
                        state.current_sl = new_sl
                        logger.info("Profit-lock SL moved to %.5f (entry + %.1fR)",
                                    new_sl, config.TP1_PROFIT_LOCK_R)
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
                    closed = self._tr.partial_close_trade(ticket, close_vol, comment="ZoneBot TP2")
                    if closed:
                        state.tp2_hit = True
                        if state.tp2_price:
                            state.accumulated_pips += pip_calc.price_to_pips(
                                abs(state.tp2_price - state.entry))
                        remaining = self._tr.get_remaining_volume(ticket)
                        if remaining is not None:
                            state.lot_size = remaining
                        else:
                            logger.warning(
                                "TP2 volume fallback | ticket=%s | MT5 position not found after partial close "
                                "— using arithmetic estimate (%.2f - %.2f = %.2f lots)",
                                ticket, state.lot_size, close_vol, max(0.01, state.lot_size - close_vol),
                            )
                            state.lot_size = max(0.01, state.lot_size - close_vol)
                        # After TP2: find the best structure-based SL for the runner.
                        # Scans M5 swings above TP1 — uses TP1 as the guaranteed floor.
                        # This avoids blindly jumping to TP1 when a swing level
                        # between TP2 and current price offers tighter, smarter protection.
                        if state.tp1_price:
                            runner_sl = find_runner_protection_sl(
                                direction=state.direction,
                                current_sl=state.current_sl,
                                floor_sl=state.tp1_price,
                                m5_window=state.m5_window,
                                pip_calc=pip_calc,
                                atr_price=h1_atr,
                            )
                            self._tr.modify_sl(ticket, runner_sl)
                            state.current_sl = runner_sl
                            logger.info(
                                "TP2 runner SL | %s ticket=%s | SL → %.5f "
                                "(swing-based, TP1 floor=%.5f)",
                                symbol, ticket, runner_sl, state.tp1_price,
                            )

            # ── TP1→TP2 corridor swing trail ──────────────────────
            # After TP1 the SL sits at the profit-lock level (entry+0.3R)
            # with no further movement until TP2 fires.  The monitor's
            # breakeven_trigger is gated by current_sl==original_sl so it
            # won't fire here.  Ratchet toward the nearest M5 swing that is
            # strictly better than the current SL so we capture more profit
            # if TP2 is never reached.  Floor = current SL (no regression).
            if state.tp1_hit and not state.tp2_hit and new_m5 and state.m5_window:
                mid_sl = find_runner_protection_sl(
                    direction=state.direction,
                    current_sl=state.current_sl,
                    floor_sl=state.current_sl,
                    m5_window=state.m5_window,
                    pip_calc=pip_calc,
                    atr_price=h1_atr,
                )
                if mid_sl != state.current_sl:
                    if self._tr.modify_sl(ticket, mid_sl):
                        logger.info(
                            "TP1→TP2 trail | %s ticket=%s | SL %.5f → %.5f",
                            symbol, ticket, state.current_sl, mid_sl,
                        )
                        state.current_sl = mid_sl

            # ── Post-TP2 runner ratchet ───────────────────────────
            # Every new M5 bar after TP2: re-evaluate swing structure and
            # tighten the runner SL if a better pivot has formed above TP1.
            # Floor = tp1_price so the guarantee never regresses.
            if state.tp2_hit and new_m5 and state.tp1_price and state.m5_window:
                runner_sl = find_runner_protection_sl(
                    direction=state.direction,
                    current_sl=state.current_sl,
                    floor_sl=state.tp1_price,
                    m5_window=state.m5_window,
                    pip_calc=pip_calc,
                    atr_price=h1_atr,
                )
                if runner_sl != state.current_sl:
                    if self._tr.modify_sl(ticket, runner_sl):
                        logger.info(
                            "Runner ratchet | %s ticket=%s | SL %.5f → %.5f",
                            symbol, ticket, state.current_sl, runner_sl,
                        )
                        state.current_sl = runner_sl

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
                tp1_hit=           state.tp1_hit,
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
                    direction_sign = 1 if state.direction == Direction.BULLISH else -1
                    final_pips = pip_calc.price_to_pips(abs(price - state.entry)) * direction_sign
                    total_pips = state.accumulated_pips + final_pips
                    if state.tp2_hit:
                        _outcome = "WIN_FULL"
                    elif state.tp1_hit or total_pips > 0:
                        _outcome = "WIN_PARTIAL"
                    else:
                        _outcome = "MONITOR_CLOSE"
                    self._journal_close(ticket, total_pips, _outcome)
                    self._update_streak(symbol, total_pips)
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
        # Covers SL hits, manual closes from MT5 terminal, and bot-managed closes.
        stale = [t for t in self._trade_states if t not in open_tickets]
        for t in stale:
            state = self._trade_states[t]
            pip_calc = PipCalculator(digits=state.digits)
            # Best estimate: MT5 closed at or near the current SL level.
            close_est = state.current_sl
            direction_sign = 1 if state.direction == Direction.BULLISH else -1
            final_pips = pip_calc.price_to_pips(abs(close_est - state.entry)) * direction_sign
            total_pips = state.accumulated_pips + final_pips
            if state.tp2_hit:
                _outcome = "WIN_FULL"
            elif state.tp1_hit or total_pips > 0:
                _outcome = "WIN_PARTIAL"
            else:
                _outcome = "LOSS"
            self._journal_close(t, total_pips, _outcome)
            self._update_streak(state.symbol, total_pips)
            del self._trade_states[t]

    def _update_streak(self, symbol: str, total_pips: float) -> None:
        now = datetime.now(tz=timezone.utc)
        if total_pips < 0:
            streak = self._loss_streak.get(symbol, 0) + 1
            self._loss_streak[symbol] = streak
            max_streak = getattr(config, "MAX_CONSECUTIVE_LOSSES", 2)
            if streak >= max_streak:
                pause_h = getattr(config, "LOSS_STREAK_PAUSE_HOURS", 4)
                self._streak_pause[symbol] = now + timedelta(hours=pause_h)
                logger.warning(
                    "streak_pause | %s | %d consecutive losses → pause until %s",
                    symbol, streak, self._streak_pause[symbol].isoformat(),
                )
        else:
            self._loss_streak[symbol] = 0

    def _journal_close(self, ticket: int, pips: float, outcome: str) -> None:
        if self._journal:
            try:
                self._journal.record_close(ticket, round(pips, 1), outcome)
            except Exception as exc:
                logger.warning("Journal record_close failed ticket=%s: %s", ticket, exc)

    def _build_trade(self, signal, broker_cost, pip_calc, tick_value,
                     risk_pct_override=None):
        balance = self._tr.get_account_balance()
        if balance <= 0:
            return None
        eff_risk = risk_pct_override if risk_pct_override is not None else self._risk_pct
        # Use per-symbol commission from broker_cost (XAUUSD differs from forex pairs on HFM).
        sym_commission = broker_cost.commission_usd
        risk_params = RiskParameters(
            account_balance=balance, risk_percent=eff_risk,
            commission_per_lot=sym_commission,
            min_rr_ratio=self._min_rr, account_currency=self._currency,
        )
        sl_pips = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
        # Floor (not round) to 2 decimal places so we never exceed the calculated risk.
        lot_size = max(0.01, math.floor(risk_params.lot_size(sl_pips, tick_value, pip_calc) * 100) / 100)

        final_tp = signal.take_profit

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
            commission=  sym_commission * lot_size,
            spread_cost= broker_cost.spread_pips,
        )