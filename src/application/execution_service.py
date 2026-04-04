"""
execution_service.py — Phase 3: The "Hands".

UPGRADED (v3):
  • H4 macro bias filter   — now a SCORER, not a kill-switch
  • Premium / discount     — now adds score points, not a hard block
  • Weighted Scoring System — A+ / B / C setup grades with tiered risk
  • ATR zone buffer        — fuzzy zone tap detection (no more point-perfect failure)
  • M5 ChoCh execution     — Change of Character with grade-specific RR floor
  • Dynamic ATR SL buffer  — passed through to analysis layer

Settings come from config.py.
"""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
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
    is_in_trading_session,  # NEW: session gate
)
from src.application.news_manager import NewsManager
from src.infrastructure.spread_calculator import SpreadCalculator

logger = logging.getLogger(__name__)


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

    # ── Phase 1: Zone Mapping ──────────────────────────────────────

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

    # ── Phase 2 + 3: Monitor & Execute ────────────────────────────

    def run_monitoring_cycle(self, symbol: str) -> Optional[Trade]:
        now        = datetime.now(tz=timezone.utc)
        digits     = self._md.get_symbol_digits(symbol)
        pip_calc   = PipCalculator(digits=digits)
        tick_value = self._md.get_tick_value(symbol)

        # ── Guard 1: news block ───────────────────────────────────
        if self._news.is_blocked(symbol, now):
            return None

        # ── Guard 1b: trading session filter ─────────────────────
        # Only trade during London (08-16 UTC) and New York (13-21 UTC).
        # Asian-session chop and dead-zone noise are filtered here.
        if not is_in_trading_session(now):
            return None

        # ── Guard 2: broker cost vs ATR ───────────────────────────
        broker_cost = self._spread.get_broker_cost(symbol)
        h1_short    = self._md.get_candles(symbol, Timeframe.H1, 20)
        h1_atr      = atr(h1_short) if h1_short else 0.0
        if h1_short:
            if self._news.broker_cost_exceeds_atr(symbol, h1_atr, broker_cost, pip_calc):
                return None

        # ── Guard 3: H4 macro context (now informational, not a kill switch) ──
        h4 = self._md.get_candles(symbol, Timeframe.H4, config.H4_CANDLE_COUNT)
        if not h4:
            logger.warning("No H4 candles for %s — skipping cycle", symbol)
            return None

        h4_bias                 = find_h4_bias(h4)
        h4_high, h4_low, h4_eq = compute_h4_equilibrium(h4)

        # ── Guard 4: current price ────────────────────────────────
        ask = self._md.get_current_price(symbol)
        if ask == 0.0:
            return None

        # ── ATR zone buffer ───────────────────────────────────────
        # Expands zone edges by 10% of ATR to catch institution front-runs.
        zone_buffer = calculate_zone_buffer(h1_atr)

        # ── Zone + pool ───────────────────────────────────────────
        zones = self._zr.get_active_zones(symbol)
        pool  = self._zr.get_liquidity_pool(symbol)
        if not pool:
            return None

        for zone in zones:
            # ── Score the setup ───────────────────────────────────
            setup_score             = score_setup(zone.direction, h4_bias, ask, h4_eq)

            # Hard threshold — skip setups below minimum quality
            if setup_score < config.SETUP_SCORE_THRESHOLD:
                logger.debug("BELOW_THRESHOLD | %s | score=%d < %d",
                             symbol, setup_score, config.SETUP_SCORE_THRESHOLD)
                continue

            grade, risk_pct, min_rr = grade_setup(setup_score)

            # Log what grade this setup earned
            logger.debug(
                "SCORE | %s | %s zone | H4=%s | price_vs_eq=%s | score=%d → grade=%s | risk=%.2f%%",
                symbol, zone.direction.value,
                h4_bias.value if h4_bias else "RANGING",
                "DISCOUNT" if ask <= h4_eq else "PREMIUM",
                setup_score, grade, risk_pct,
            )

            # ── Zone tap check (with ATR buffer) ──────────────────
            if not (ask <= zone.high + zone_buffer and ask >= zone.low - zone_buffer):
                continue

            zone.mark_tapped()
            logger.info(
                "Zone tapped | %s | %s | H=%.5f L=%.5f | grade=%s | "
                "H4_bias=%s | price_vs_eq=%s | score=%d",
                symbol, zone.direction.value, zone.high, zone.low, grade,
                h4_bias.value if h4_bias else "RANGING",
                "DISCOUNT" if ask <= h4_eq else "PREMIUM",
                setup_score,
            )

            # ── Fetch M5 candles ──────────────────────────────────
            m5 = self._md.get_candles(symbol, Timeframe.M5, config.M5_CANDLE_COUNT)
            if not m5:
                continue

            # ── M5 ChoCh signal (grade-aware RR floor + ATR buffer) ─
            signal = find_m5_choch_signal(
                m5_candles=      m5,
                zone=            zone,
                pip_calc=        pip_calc,
                spread_pips=     broker_cost.spread_pips,
                liquidity_pool=  pool,
                atr_price=       h1_atr,
                zone_buffer=     zone_buffer,
                min_rr_override= min_rr,
            )
            if signal is None:
                continue

            open_count = sum(1 for t in self._tr.get_open_positions() if t.symbol == symbol)
            if open_count >= config.MAX_OPEN_TRADES:
                logger.info("MAX_OPEN_TRADES reached for %s", symbol)
                continue

            trade = self._build_trade(signal, broker_cost, pip_calc, tick_value,
                                      risk_pct_override=risk_pct)
            if trade is None:
                continue

            ticket = self._tr.place_trade(trade)
            if ticket is None:
                logger.error("Trade placement failed for %s", symbol)
                continue

            trade.mt5_ticket = ticket
            trade.status     = TradeStatus.OPEN
            logger.info(
                "TRADE PLACED ✓ | %s %s | grade=%s | entry=%.5f SL=%.5f TP=%.5f | "
                "lot=%.2f | RR=%.2f | risk=%.2f%% | H4=%s",
                symbol, signal.direction.value, grade,
                signal.entry_price, signal.stop_loss, signal.take_profit,
                trade.lot_size, signal.risk_reward, risk_pct,
                h4_bias.value if h4_bias else "RANGING",
            )
            return trade

        return None

    # ── Helpers ───────────────────────────────────────────────────

    def _build_trade(
        self,
        signal:           TradeSignal,
        broker_cost:      BrokerCost,
        pip_calc:         PipCalculator,
        tick_value:       float,
        risk_pct_override: Optional[float] = None,
    ) -> Optional[Trade]:
        balance = self._tr.get_account_balance()
        if balance <= 0:
            logger.error("Account balance is 0 — cannot size trade.")
            return None

        # Use grade-specific risk % if provided, otherwise fall back to
        # the constructor default (config.RISK_PERCENT).
        effective_risk = risk_pct_override if risk_pct_override is not None else self._risk_pct

        risk_params = RiskParameters(
            account_balance=    balance,
            risk_percent=       effective_risk,
            commission_per_lot= self._commission,
            min_rr_ratio=       self._min_rr,
            account_currency=   self._currency,
        )
        sl_pips  = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
        lot_size = max(0.01, round(risk_params.lot_size(sl_pips, tick_value, pip_calc), 2))

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