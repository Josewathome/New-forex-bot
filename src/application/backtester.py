# src/application/backtester.py (v10 – MFE closure fix + conviction unlock)

"""
backtester.py — v10: Fixes MFE/MAE closure bug, win-rate reporting, conviction gate.

CHANGES vs v9:
  • _simulate(): meta() no longer uses frozen default args for mfe_price/mae_price.
    The old signature `def meta(..., mfe_val=mfe_price, mae_val=mae_price)` froze
    those values at function-definition time (= entry price), so every trade
    reported MFE = 0.0 pips. Now reads from the enclosing scope at call time.
"""
from __future__ import annotations
import csv
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import src.config as config
from src.domain.entities import (
    Candle, Direction, NewsEvent, NoTradeWindow,
    Timeframe, Trade, TradeStatus, Zone, ZoneType,
)
from src.domain.value_objects import BrokerCost, PipCalculator, RiskParameters
from src.application.analysis import (
    ZoneMapper, atr,
    find_m5_choch_signal,
    find_m1_predictive_signal,
    find_m5_precision_entry,
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
    read_m30_zone_story,
    read_m5_zone_story,
)
from src.application.diagnostics import DiagnosticsCollector
from src.application.trade_monitor import TradeMonitor, MonitorEvent, find_swing_trail_sl, find_runner_protection_sl
from src.infrastructure.news_client import ForexNewsClient
from src.infrastructure.spread_calculator import FALLBACK_SPREAD_PIPS, DEFAULT_FALLBACK_PIPS

logger = logging.getLogger(__name__)
_M5 = timedelta(minutes=5)


@dataclass
class BacktestResult:
    trade:              Trade
    outcome:            str
    pnl_usd:            float
    pips_gain:          float
    news_affected:      bool
    mfe_pips:           float = 0.0
    pips_to_tp_at_mfe:  float = 0.0
    duration_minutes:   float = 0.0
    reversal_zone_type: Optional[str] = None
    balance_before:     float = 0.0
    balance_after:      float = 0.0
    monitor_events:         List[MonitorEvent] = field(default_factory=list)
    monitor_events_count:   int = 0
    first_monitor_signal:   str = ""
    sl_trailed:             bool = False
    trail_sl_value:         float = 0.0
    early_close:            bool = False
    tp1_price:              float = 0.0
    tp1_hit:                bool = False
    tp2_price:              float = 0.0
    tp2_hit:                bool = False
    mfe:                    float = 0.0
    mae:                    float = 0.0
    efficiency:             float = 0.0
    realized_pnl:           float = 0.0
    remaining_volume:       float = 0.0
    trade_score:            int = 0
    kill_zone:              bool = False


class Backtester:

    def __init__(
        self,
        news_client:     ForexNewsClient,
        account:         float,
        risk_pct:        float,
        commission:      float,
        currency:        str,
        tick_values:     Optional[Dict[str, float]] = None,
        commission_map:  Optional[Dict[str, float]] = None,
    ) -> None:
        self._news           = news_client
        self._account        = account
        self._risk_pct       = risk_pct
        self._commission     = commission
        self._currency       = currency
        self._mapper         = ZoneMapper()
        self._tick_values    = tick_values or {}
        self._commission_map = commission_map or {}
        # Streak protection: track consecutive net-negative losses per symbol.
        self._loss_streak:    Dict[str, int]      = {}
        self._streak_pause:   Dict[str, datetime] = {}

    def _get_tick_value(self, symbol: str) -> float:
        if symbol in self._tick_values and self._tick_values[symbol] > 0:
            return self._tick_values[symbol]
        usd = config.TICK_VALUE_USD.get(symbol, 10.0)
        return usd * (config.KES_PER_USD if self._currency == "KES" else 1.0)

    def _fetch_news_chunked(self, currencies, start_date, end_date, chunk_days=7):
        all_evts, current = [], datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        while current <= end_dt:
            chunk_end = min(current + timedelta(days=chunk_days - 1), end_dt)
            all_evts.extend(self._news.get_range_events(
                currencies=currencies,
                start_date=current.strftime("%Y-%m-%d"),
                end_date=chunk_end.strftime("%Y-%m-%d"),
            ))
            current = chunk_end + timedelta(days=1)
        logger.info("News: %d events", len(all_evts))
        return all_evts

    def run(self, candle_data, start_date, end_date, diag=None):
        logger.info("Backtest v10 (MFE fix + conviction unlock): %s → %s", start_date, end_date)
        results, equity = [], self._account

        if diag:
            for sym, td in candle_data.items():
                diag.record_mt5_data(sym, td.get(Timeframe.H1, []),
                                     td.get(Timeframe.M1, []))

        currencies = list({c for cs in config.SYMBOL_CURRENCIES.values() for c in cs})
        news_events = self._fetch_news_chunked(currencies, start_date, end_date)
        no_trade_windows = self._build_windows(news_events)

        if diag:
            hi = [e for e in news_events if e.impact.lower() == "high"]
            diag.record_news_fetch("MyFxBook", start_date, end_date,
                                   currencies, len(news_events), len(hi))
            for w in no_trade_windows:
                diag.record_no_trade_window(w.currency, w.event_name,
                    w.start.strftime("%Y-%m-%d %H:%M"),
                    w.end.strftime("%Y-%m-%d %H:%M"))

        sym_data: Dict[str, dict] = {}
        for sym, td in candle_data.items():
            h1  = td.get(Timeframe.H1, [])
            m5  = td.get(Timeframe.M5, []) or td.get(Timeframe.M1, [])
            m1  = td.get(Timeframe.M1, [])
            h4  = td.get(Timeframe.H4, [])
            d1  = td.get(Timeframe.D1, [])
            m30 = td.get(Timeframe.M30, [])
            if h1 and m5:
                sym_data[sym] = {"h1": h1, "m5": m5, "m1": m1, "h4": h4, "d1": d1, "m30": m30}

        H1W = 100
        s_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        e_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        all_evts = []
        for sym, d in sym_data.items():
            for idx in range(H1W, len(d["h1"])):
                bt = d["h1"][idx].time
                if s_dt <= bt <= e_dt:
                    all_evts.append((bt, sym, idx))
        all_evts.sort(key=lambda x: (x[0], x[1]))

        logger.info("Processing %d H1 events …", len(all_evts))
        for bar_time, sym, h1_idx in all_evts:
            d = sym_data[sym]
            r = self._process_h1_bar(sym, d["h1"], d["m5"], d["m1"], d["h4"], d["d1"],
                                     d.get("m30", []),
                                     h1_idx, no_trade_windows, equity, diag)
            if r:
                equity += r.pnl_usd
                results.append(r)

        self.print_summary(results, equity, start_date, end_date)
        self.analyze_trade_features(results)
        return results

    def _process_h1_bar(self, symbol, h1, m5, m1, h4, d1, m30,
                        h1_idx, ntw, equity, diag):
        digits = config.SYMBOL_DIGITS.get(symbol, 5)
        tv = self._get_tick_value(symbol)
        pip_calc = PipCalculator(digits=digits)
        h1_win = h1[h1_idx - 100: h1_idx]
        zones, pool = self._mapper.map_zones(h1_win, symbol, Timeframe.H1)
        bar_time = h1[h1_idx].time

        # ── Consecutive loss streak protection ────────────────────────
        # After MAX_CONSECUTIVE_LOSSES real losses (net-negative P&L) on a symbol,
        # pause entries for LOSS_STREAK_PAUSE_HOURS.  GBPUSD had 3 consecutive
        # -114/-113/-112 losses in Nov; USDCHF had 4 streaks.  The losing
        # streak signals the market has invalidated our zone bias — sit out.
        pause_until = self._streak_pause.get(symbol)
        if pause_until and bar_time < pause_until:
            if diag: diag.record_rejection(symbol, "streak_pause")
            return None

        # Past-only window: use the 60 most recent M5 bars that closed BEFORE
        # bar_time.  The previous 6-hour forward window (bar_time to bar_time+6h)
        # gave the signal finders future candles that do not exist in live trading,
        # inflating the backtested signal rate and masking live underperformance.
        # 60 bars = 5 hours of M5 context, matching config.M5_CANDLE_COUNT.
        m5_win = [c for c in m5 if c.time < bar_time][-60:]

        if diag:
            diag.record_h1_bar(
                symbol=symbol,
                m1_count=len(m5_win) * 5,
                ob_count=sum(1 for z in zones if z.zone_type.value == "order_block"),
                fvg_count=sum(1 for z in zones if z.zone_type.value == "fvg"),
                has_pool=pool is not None,
            )
        if len(m5_win) < 3 or not zones or not pool:
            return None

        h4_win = [c for c in h4 if c.time < bar_time][-config.H4_CANDLE_COUNT:]
        h4_bias = find_h4_bias(h4_win) if len(h4_win) >= 10 else None
        h4_eq = compute_h4_equilibrium(h4_win)[2] if len(h4_win) >= 4 else None

        # H4 zone map — OBs/FVGs on H4 used for confluence scoring
        h4_zones, _ = self._mapper.map_zones(h4_win, symbol, Timeframe.H4) if len(h4_win) >= 20 else ([], None)

        # D1 macro trend — use all D1 bars up to (but not including) the current bar_time
        d1_win   = [c for c in d1 if c.time < bar_time][-getattr(config, "D1_CANDLE_COUNT", 30):]
        d1_bias  = find_d1_bias(d1_win) if len(d1_win) >= getattr(config, "D1_EQUILIBRIUM_LOOKBACK", 15) else None

        bar_px = h1[h1_idx].open
        # Use the most recent 14 H1 bars for ATR — matches execution_service.py
        # which uses atr(h1_short[-14:]).  The old full-window ATR inflated the
        # FVG size filter and SL buffer after high-volatility sessions, causing
        # backtest FVG counts and SL distances to diverge from live behaviour.
        h1_atr = atr(h1_win[-15:]) if len(h1_win) >= 15 else atr(h1_win)
        zbuf = calculate_zone_buffer(h1_atr)

        if self._in_news(bar_time, symbol, ntw):
            if diag: diag.record_rejection(symbol, "news_block")
            return None
        if not is_in_trading_session(bar_time):
            if diag: diag.record_rejection(symbol, "outside_session")
            return None

        # Per-symbol commission: use commission_map when available (XAUUSD ≠ forex pairs).
        sym_commission = self._commission_map.get(symbol, self._commission)
        bc = BrokerCost(
            spread_pips=FALLBACK_SPREAD_PIPS.get(symbol, DEFAULT_FALLBACK_PIPS),
            commission_usd=sym_commission,
            tick_value=tv,
        )

        # ── M30 zone map (historical, for confluence scoring) ─────────────
        # Use M30 bars in the 30-bar window before bar_time.
        m30_lookback = 60
        m30_zone_win = [c for c in m30 if c.time < bar_time][-m30_lookback:]
        m30_zones_bt, _ = (self._mapper.map_zones(m30_zone_win, symbol, Timeframe.M30)
                           if len(m30_zone_win) >= 10 else ([], None))

        # ── M5 zone map (historical, for confluence scoring) ──────────────
        # Use the most recent M5 bars before bar_time (not the future 6h window).
        m5_zone_win = [c for c in m5 if c.time < bar_time][-60:]
        m5_zones_bt, _ = (self._mapper.map_zones(m5_zone_win, symbol, Timeframe.M5)
                          if len(m5_zone_win) >= 10 else ([], None))

        # Past-only window: use the 60 most recent M1 bars before bar_time,
        # matching the live system which fetches config.M1_CANDLE_COUNT (60)
        # closed M1 bars from MT5.  Forward M1 bars must not be included here.
        m1_win = [c for c in m1 if c.time < bar_time][-60:]

        sym_min_score = config.SYMBOL_MIN_SCORE.get(symbol, config.SETUP_SCORE_THRESHOLD)

        for zone in zones:
            # ── Trend pre-filters ──────────────────────────────────
            if getattr(config, "REQUIRE_H4_BIAS", True) and h4_bias is None:
                if diag: diag.record_rejection(symbol, "h4_ranging")
                break  # applies to all zones for this bar

            if (getattr(config, "BLOCK_COUNTER_D1", True)
                    and d1_bias is not None
                    and d1_bias != zone.direction):
                if diag: diag.record_rejection(symbol, "counter_d1")
                continue

            h4_confluent  = find_htf_zone_confluence(zone, h4_zones)
            m30_confluent = find_m30_zone_confluence(zone, m30_zones_bt)
            m5_confluent  = find_m5_zone_confluence(zone, m5_zones_bt)
            setup_score = score_setup(
                zone.direction, h4_bias, bar_px, h4_eq, d1_bias,
                h4_confluent, m30_confluent, m5_confluent,
            )
            if setup_score < sym_min_score:
                if diag: diag.record_rejection(symbol, "below_score_threshold")
                continue
            grade, rpct, min_rr = grade_setup(setup_score)

            # Displacement timeframe (minutes) for the fill-window guard below.
            # signal.created_at is the displacement bar's OPEN; live cannot see
            # the signal until that bar CLOSES (created_at + this many minutes).
            # Default 5 covers the M5 ChoCh and the zone-flip/opposing paths
            # (both produce M5 displacement); the M1 predictive path overrides
            # to 1 on success below.  This is a fill-timing guard only — no
            # strategy, scoring, or signal-generation logic is affected.
            disp_tf_min = 5

            # Re-evaluate zone at the moment price arrives.
            healthy, reject_reason = evaluate_zone_health(
                zone, h1_win, m5_win, h1_atr)

            if not healthy:
                if reject_reason == "m5_momentum_against_zone":
                    # Institutional pressure runs AGAINST our zone.
                    # Try to trade WITH that institution instead.
                    opp = find_opposing_opportunity(
                        rejected_zone=zone,
                        m5_candles=m5_win,
                        pip_calc=pip_calc,
                        spread_pips=bc.spread_pips,
                        liquidity_pool=pool,
                        h1_atr=h1_atr,
                        zone_buffer=zbuf,
                        min_rr=min_rr,
                        diag=diag,
                    )
                    if opp is None:
                        if diag: diag.record_rejection(symbol, "flip_no_signal")
                        continue
                    signal, zone = opp   # trade the reversal instead
                else:
                    if diag: diag.record_rejection(symbol, reject_reason)
                    continue
            else:
                # ── M30 story gate (matches live execution_service.py) ────────
                # Block when the last M30 bar inside the zone closed against zone
                # direction — a 30-minute institutional statement that outweighs
                # any M5/M1 signal we might find.
                m30_story_win = [c for c in m30 if c.time < bar_time][-getattr(config, "M30_STORY_LOOKBACK", 8):]
                if m30_story_win:
                    m30_st = read_m30_zone_story(zone, m30_story_win, h1_atr)
                    if m30_st == "against":
                        if diag: diag.record_rejection(symbol, "m30_story_against")
                        continue

                # ── M5 zone story gate (matches live execution_service.py) ─────
                # Block when M5 bars inside the zone show distribution — closes
                # drifting away from TP direction with elevated bearish volume.
                m5_st = read_m5_zone_story(zone, m5_win, h1_atr)
                if m5_st == "distributing":
                    if diag: diag.record_rejection(symbol, "m5_story_distributing")
                    continue

                # ── M5 precision entry zone (mirrors execution_service) ──
                precision_zone_bt = None
                if getattr(config, "USE_M5_PRECISION_ZONE", True):
                    precision_zone_bt = find_m5_precision_entry(
                        zone, m5_win, pip_calc, h1_atr
                    )

                # ── M1 predictive signal (primary) ────────────────
                # m5_bar_open_time: the forming M5 bar's open is the last
                # closed M5 candle's time + 5 minutes (mirrors live logic).
                m5_bar_open_bt = (
                    m5_win[-1].time + timedelta(minutes=5) if m5_win else None
                )
                signal = None
                if m1_win:
                    signal = find_m1_predictive_signal(
                        m1_candles=m1_win,
                        zone=zone,
                        pip_calc=pip_calc,
                        spread_pips=bc.spread_pips,
                        liquidity_pool=pool,
                        current_m5=m5_win[-1] if m5_win else None,
                        atr_price=h1_atr,
                        zone_buffer=zbuf,
                        min_rr_override=min_rr,
                        m30_zones=m30_zones_bt,
                        m5_candles=m5_win,
                        m5_bar_open_time=m5_bar_open_bt,
                    )
                    if signal is not None:
                        disp_tf_min = 1   # M1 displacement closes 1 min after its open
                # ── M5 ChoCh (fallback) ───────────────────────────
                if signal is None:
                    signal = find_m5_choch_signal(
                        m5_candles=m5_win, zone=zone, pip_calc=pip_calc,
                        spread_pips=bc.spread_pips, liquidity_pool=pool,
                        atr_price=h1_atr, diag=diag,
                        zone_buffer=zbuf, min_rr_override=min_rr,
                        m30_zones=m30_zones_bt,
                        precision_zone=precision_zone_bt,
                    )
                if signal is None:
                    continue

            if self._in_news(signal.created_at, symbol, ntw):
                if diag: diag.record_rejection(symbol, "news_block")
                continue

            sl_pips = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
            if not check_min_sl_pips(symbol, sl_pips):
                if diag: diag.record_rejection(symbol, "sl_too_tight")
                continue

            # Trade Scoring — use signal.direction so flipped trades score correctly.
            htf_aligned = (h4_bias == signal.direction)
            liquidity_sweep = (zone.zone_type == ZoneType.ORDER_BLOCK and zone.touch_count == 0)
            fvg_clean = (zone.zone_type == ZoneType.FAIR_VALUE_GAP and zone.size > h1_atr * 0.5)
            strong_displacement = (signal.volume_ratio >= 1.5)
            # is_kill_zone is kept only as an analytics tag on the trade record
            # below (kill_zone=) — it no longer feeds score_trade (SCORE_KILL_ZONE
            # was 0: backtest showed kill-zone trades perform 2.2x worse).
            in_kill_zone = is_kill_zone(signal.created_at)

            trade_score = score_trade(
                htf_aligned, liquidity_sweep, fvg_clean,
                strong_displacement,
            )
            if trade_score < config.TRADE_SCORE_THRESHOLD:
                if diag: diag.record_rejection(symbol, "low_trade_score")
                continue

            risk_pct = dynamic_risk(trade_score)

            # Dynamic TP — same logic as live execution_service (no static override).
            # Use 65-bar baseline to match execution_service.py which fetches 65 H1 bars
            # and uses all of them as the baseline_atr window.
            baseline_atr = atr(h1_win[-65:]) if len(h1_win) >= 65 else atr(h1_win)
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
            # Clamp TP2 to the liquidity pool.  When TP2 ratio pushes BEYOND
            # the pool, MT5's full take_profit fires first — the partial close
            # never triggers.  Skip TP2 so the runner runs straight to the pool.
            # Use strict > / < so that TP2 == pool is still allowed to fire.
            if signal.direction == Direction.BULLISH and tp2_price > signal.take_profit:
                tp2_price = None
            elif signal.direction == Direction.BEARISH and tp2_price < signal.take_profit:
                tp2_price = None

            # Position size.
            # tv (tick_value) is already in account currency per pip per lot —
            # MT5 normalises it to the account currency before returning it.
            # Do NOT multiply by KES_PER_USD; that would double-convert on a KES account.
            # math.floor truncates to 2 dp so we never exceed the risk budget.
            rp = RiskParameters(account_balance=equity, risk_percent=risk_pct,
                                commission_per_lot=self._commission,
                                min_rr_ratio=min_rr,
                                account_currency=self._currency)
            lot = max(0.01, min(math.floor(rp.lot_size(sl_pips, tv, pip_calc) * 100) / 100, 100.0))
            ram = equity * (risk_pct / 100)

            max_m1 = config.MAX_FORWARD_M1_BARS
            # LOOK-AHEAD FIX: the fill/simulation window must start no earlier
            # than the displacement bar's CLOSE — the earliest moment the signal
            # could exist in live.  signal.created_at is the displacement bar's
            # OPEN, so scanning from there previously let the backtest fill on
            # price action during (or before) the displacement candle — price
            # that live could not have acted on yet.  Shift the window start to
            # created_at + displacement duration.
            fill_start = signal.created_at + timedelta(minutes=disp_tf_min)
            fm1 = [c for c in m1 if c.time >= fill_start][:max_m1]
            fm5 = [c for c in m5 if c.time >= fill_start][:max_m1 // 5]

            monitor = TradeMonitor(pip_calc=pip_calc, atr_price=h1_atr, symbol=symbol)

            # ── News slippage simulation ───────────────────────────
            # Apply NEWS_SLIPPAGE_PIPS to the simulated entry whenever the
            # signal fires within NEWS_BUFFER_MINUTES of a news window.
            # This makes backtests reflect real-world fill degradation on
            # event-adjacent setups rather than assuming perfect fills.
            news_affected = self._near_news(signal.created_at, symbol, ntw)
            slippage_pips = 0.0
            sim_entry = signal.entry_price
            if news_affected:
                slippage_pips = getattr(config, "NEWS_SLIPPAGE_PIPS", 5.0)
                slippage_price = pip_calc.pips_to_price(slippage_pips)
                # Slippage works against the trade: worsens fill price
                sim_entry = (signal.entry_price + slippage_price
                             if signal.direction == Direction.BULLISH
                             else signal.entry_price - slippage_price)
                logger.debug(
                    "BT slippage | %s | news_adjacent=True | "
                    "entry %.5f → %.5f (+%.1f pips)",
                    symbol, signal.entry_price, sim_entry, slippage_pips,
                )

            outcome, pnl, meta, mon_evts = self._simulate(
                signal=signal, entry=sim_entry,
                future_m1=fm1, future_m5=fm5,
                risk_amt=ram, lot_size=lot, tick_value=tv,
                pip_calc=pip_calc, zones=zones, monitor=monitor,
                tp1_price=tp1_price, tp2_price=tp2_price,
                trade_score=trade_score, tp1_ratio=tp1_ratio, tp2_ratio=tp2_ratio,
                grade=grade, commission=sym_commission,
            )

            if diag:
                diag.record_outcome(symbol, outcome)
                diag.record_trade_result(
                    symbol=symbol, outcome=outcome,
                    mfe_pips=meta.get("mfe_pips", 0.0),
                    pips_to_tp_at_mfe=meta.get("pips_to_tp_at_mfe", 0.0),
                    duration_minutes=meta.get("duration_minutes", 0.0),
                    reversal_zone_type=meta.get("reversal_zone_type"),
                    balance_before=equity, balance_after=equity + pnl,
                    opened_at=signal.created_at.isoformat(),
                )
                for ev in mon_evts:
                    ev.trade_outcome = outcome
                    ev.was_correct = outcome in ("LOSS", "MONITOR_CLOSE")
                    diag.record_monitor_event(ev)

            trail_ev = next((e for e in mon_evts if e.action_taken == "trailed_sl"), None)
            sl_trailed = trail_ev is not None
            early_cl = any(e.action_taken == "closed_early" for e in mon_evts)
            first_s = mon_evts[0].signal_type if mon_evts else ""
            bb, ba = equity, equity + pnl
            tid = f"bt_{h1_idx}_{zone.id[:8]}"

            trade = Trade(
                id=tid, symbol=signal.symbol, direction=signal.direction,
                entry_price=sim_entry, stop_loss=signal.stop_loss,
                take_profit=signal.take_profit, lot_size=lot,
                status=TradeStatus.CLOSED, created_at=signal.created_at,
                closed_at=meta.get("closed_at"), pnl=pnl,
                commission=sym_commission * lot,
                news_affected=news_affected, slippage_pips=slippage_pips,
                mfe_price=meta.get("mfe_price"),
                mfe_pips=meta.get("mfe_pips"),
                pips_to_tp_at_mfe=meta.get("pips_to_tp_at_mfe"),
                duration_minutes=meta.get("duration_minutes"),
                reversal_zone_type=meta.get("reversal_zone_type"),
                balance_before=bb, balance_after=ba,
                tp1_hit=meta.get("tp1_hit", False),
                tp2_hit=meta.get("tp2_hit", False),
                initial_risk=get_initial_risk(signal.entry_price, signal.stop_loss),
                realized_pnl=meta.get("realized_pnl", 0.0),
                remaining_volume=meta.get("remaining_volume", lot),
                exit_reason=meta.get("exit_reason", ""),
                mfe=meta.get("mfe", 0.0),
                mae=meta.get("mae", 0.0),
            )

            logger.info(
                "BT | %s %s | %s | MFE=%.1f | TP1=%s TP2=%s | eff=%.1f%% | score=%d",
                symbol, signal.direction.value, outcome,
                meta.get("mfe_pips", 0),
                "HIT" if meta.get("tp1_hit") else ("SET" if tp1_price else "OFF"),
                "HIT" if meta.get("tp2_hit") else ("SET" if tp2_price else "OFF"),
                meta.get("efficiency", 0.0),
                trade_score,
            )

            # ── Loss streak protection update ──────────────────────────────────
            if pnl < 0:
                streak = self._loss_streak.get(symbol, 0) + 1
                self._loss_streak[symbol] = streak
                max_streak = getattr(config, "MAX_CONSECUTIVE_LOSSES", 2)
                if streak >= max_streak:
                    pause_h = getattr(config, "LOSS_STREAK_PAUSE_HOURS", 4)
                    self._streak_pause[symbol] = bar_time + timedelta(hours=pause_h)
                    logger.info(
                        "BT | streak_pause | %s | %d consecutive losses → pause until %s",
                        symbol, streak, self._streak_pause[symbol].isoformat(),
                    )
            else:
                self._loss_streak[symbol] = 0

            return BacktestResult(
                trade=trade, outcome=outcome, pnl_usd=pnl,
                pips_gain=pip_calc.price_to_pips(abs(
                    signal.take_profit - sim_entry
                    if outcome in ("WIN_FULL",) else signal.stop_loss - sim_entry)),
                news_affected=news_affected,
                mfe_pips=meta.get("mfe_pips", 0.0),
                pips_to_tp_at_mfe=meta.get("pips_to_tp_at_mfe", 0.0),
                duration_minutes=meta.get("duration_minutes", 0.0),
                reversal_zone_type=meta.get("reversal_zone_type"),
                balance_before=bb, balance_after=ba,
                monitor_events=mon_evts, monitor_events_count=len(mon_evts),
                first_monitor_signal=first_s,
                sl_trailed=sl_trailed,
                trail_sl_value=trail_ev.new_sl if trail_ev else 0.0,
                early_close=early_cl,
                tp1_price=tp1_price or 0.0,
                tp1_hit=meta.get("tp1_hit", False),
                tp2_price=tp2_price or 0.0,
                tp2_hit=meta.get("tp2_hit", False),
                mfe=meta.get("mfe", 0.0),
                mae=meta.get("mae", 0.0),
                efficiency=meta.get("efficiency", 0.0),
                realized_pnl=meta.get("realized_pnl", 0.0),
                remaining_volume=meta.get("remaining_volume", lot),
                trade_score=trade_score,
                kill_zone=in_kill_zone,
            )

        return None

    def _simulate(
        self,
        signal, entry,
        future_m1, future_m5,
        risk_amt, lot_size, tick_value,
        pip_calc, zones, monitor,
        tp1_price: Optional[float] = None,
        tp2_price: Optional[float] = None,
        trade_score: int = 0,
        tp1_ratio: float = 1.8,
        tp2_ratio: float = 3.5,
        grade: str = "C",
        commission: Optional[float] = None,
    ):
        # When BACKTEST_USE_M5_ONLY=True, always simulate on M5 regardless of M1
        # availability.  MT5 retains M1 for ~60 days, giving 31.5% coverage on a
        # 6-month test — the other 68.5% silently used M5 anyway, making the
        # mixed result unreliable.  M5-only is slower but honest and consistent.
        force_m5 = getattr(config, "BACKTEST_USE_M5_ONLY", False)
        use_m1 = bool(future_m1) and not force_m5
        bars = future_m1 if use_m1 else future_m5
        if not bars:
            return "OPEN", 0.0, {
                "mfe_price": entry, "mfe_pips": 0.0,
                "pips_to_tp_at_mfe": 0.0, "closed_at": signal.created_at,
                "duration_minutes": 0.0, "reversal_zone_type": None,
                "tp1_hit": False, "tp2_hit": False,
                "realized_pnl": 0.0, "remaining_volume": lot_size,
                "exit_reason": "no_bars", "mfe": 0.0, "mae": 0.0,
                "efficiency": 0.0,
            }, []

        is_bull = signal.direction == Direction.BULLISH
        original_sl = signal.stop_loss
        virtual_sl = original_sl
        mfe_price = entry
        mae_price = entry
        all_evts: List[MonitorEvent] = []
        recent_sigs: List[Tuple] = []

        sl_dist = abs(entry - original_sl)
        sl_pips = pip_calc.price_to_pips(sl_dist)
        # commission is already in account currency (KES for this account).
        # Use per-symbol commission when provided; fall back to instance default.
        eff_commission = commission if commission is not None else self._commission
        comm = eff_commission * lot_size

        # ── Limit-order fill check ─────────────────────────────────────
        # The signal entry is a FVG limit price (below current price for
        # bulls, above for bears). The displacement candle already moved
        # price AWAY from entry, so price must retrace before the order
        # fills.  Without this check, every signal is treated as an
        # immediate fill — creating phantom wins whenever price runs to TP
        # without ever touching the limit level.
        fill_bar_idx: Optional[int] = None
        for _fi, _fb in enumerate(bars):
            if is_bull and _fb.low <= entry:
                fill_bar_idx = _fi
                break
            elif not is_bull and _fb.high >= entry:
                fill_bar_idx = _fi
                break

        if fill_bar_idx is None:
            # Price never retraced to the limit — order expired unfilled.
            return "NOT_FILLED", 0.0, {
                "mfe_price": entry, "mfe_pips": 0.0,
                "pips_to_tp_at_mfe": 0.0, "closed_at": signal.created_at,
                "duration_minutes": 0.0, "reversal_zone_type": None,
                "tp1_hit": False, "tp2_hit": False,
                "realized_pnl": 0.0, "remaining_volume": lot_size,
                "exit_reason": "limit_expired", "mfe": 0.0, "mae": 0.0,
                "efficiency": 0.0,
            }, []

        # Trim bars so simulation starts only once the limit fills.
        bars = bars[fill_bar_idx:]
        if not bars:
            return "NOT_FILLED", 0.0, {
                "mfe_price": entry, "mfe_pips": 0.0,
                "pips_to_tp_at_mfe": 0.0, "closed_at": signal.created_at,
                "duration_minutes": 0.0, "reversal_zone_type": None,
                "tp1_hit": False, "tp2_hit": False,
                "realized_pnl": 0.0, "remaining_volume": lot_size,
                "exit_reason": "limit_expired", "mfe": 0.0, "mae": 0.0,
                "efficiency": 0.0,
            }, []

        if grade == "A+":
            tp1_pct = getattr(config, "TIERED_TP1_CLOSE_PCT_A_PLUS", 0.40)
        elif grade == "B":
            tp1_pct = getattr(config, "TIERED_TP1_CLOSE_PCT_B", 0.50)
        else:
            tp1_pct = getattr(config, "TIERED_TP1_CLOSE_PCT", 0.60)
        tp2_pct = getattr(config, "TIERED_TP2_CLOSE_PCT", 0.25)
        runner_pct = 1.0 - tp1_pct - tp2_pct
        if runner_pct < 0:
            runner_pct = 0.0

        remaining_vol = lot_size
        realized_pnl = 0.0
        tp1_hit = False
        tp2_hit = False
        exit_reason = ""
        start_time = bars[0].time

        m5_idx = 0
        m5_window: List[Candle] = []
        m5_no_mfe = 0
        m5_had_mfe = True
        m1_bars_since_mfe = 0

        opposing = monitor.scan_opposing_zones(
            signal.direction, entry, signal.take_profit, zones)

        def ppnl(close_px, vol=lot_size):
            raw_move = abs(close_px - entry)
            pips_move = pip_calc.price_to_pips(raw_move)
            direction_mult = 1 if (is_bull and close_px > entry) or (not is_bull and close_px < entry) else -1
            return direction_mult * (pips_move / max(sl_pips, 0.001)) * risk_amt * (vol / lot_size) - comm * (vol / lot_size)

        # ── BUG FIX: meta() no longer uses mfe_price/mae_price as frozen default
        # arguments. In Python, default argument values are evaluated ONCE at
        # function-definition time — so the old signature
        #   def meta(..., mfe_val=mfe_price, mae_val=mae_price)
        # captured mfe_price=entry and mae_price=entry permanently, making every
        # trade report MFE = 0.0 pips regardless of how far price actually moved.
        # The fix: remove those defaults and read the variables from the enclosing
        # scope at the moment meta() is called.
        def meta(bar, rzt=None, exit_reason_str="", realized=None, remaining=None):
            _mfe      = mfe_price          # read live from enclosing scope
            _mae      = mae_price          # read live from enclosing scope
            _realized = realized_pnl  if realized  is None else realized
            _remain   = remaining_vol if remaining is None else remaining
            mp = pip_calc.price_to_pips(abs(_mfe - entry))
            lp = pip_calc.price_to_pips(abs(signal.take_profit - _mfe))
            dm = (bar.time - start_time).total_seconds() / 60
            max_possible = risk_amt * tp2_ratio - comm
            efficiency = (_realized / max_possible * 100) if max_possible > 0 else 0.0
            return {
                "mfe_price": _mfe, "mfe_pips": mp,
                "pips_to_tp_at_mfe": lp, "closed_at": bar.time,
                "duration_minutes": dm, "reversal_zone_type": rzt,
                "tp1_hit": tp1_hit, "tp2_hit": tp2_hit,
                "realized_pnl": _realized, "remaining_volume": _remain,
                "exit_reason": exit_reason_str,
                "mfe": mp, "mae": pip_calc.price_to_pips(abs(_mae - entry)),
                "efficiency": efficiency,
            }

        for i, bar in enumerate(bars):
            # ── Update MFE / MAE ──────────────────────────────────
            if is_bull:
                if bar.high > mfe_price:
                    mfe_price = bar.high
                    m1_bars_since_mfe = 0
                    m5_no_mfe = 0
                    m5_had_mfe = True
                else:
                    m1_bars_since_mfe += 1
                if bar.low < mae_price:
                    mae_price = bar.low
            else:
                if bar.low < mfe_price:
                    mfe_price = bar.low
                    m1_bars_since_mfe = 0
                    m5_no_mfe = 0
                    m5_had_mfe = True
                else:
                    m1_bars_since_mfe += 1
                if bar.high > mae_price:
                    mae_price = bar.high

            # ── M5 cursor ─────────────────────────────────────────
            new_m5 = False
            if use_m1:
                while m5_idx < len(future_m5) and bar.time >= future_m5[m5_idx].time + _M5:
                    if not m5_had_mfe:
                        m5_no_mfe += 1
                    m5_had_mfe = False
                    m5_window.append(future_m5[m5_idx])
                    if len(m5_window) > 12:
                        m5_window.pop(0)
                    m5_idx += 1
                    new_m5 = True
            else:
                m5_window.append(bar)
                if len(m5_window) > 12:
                    m5_window.pop(0)
                new_m5 = True

            # ── TP1 ───────────────────────────────────────────────
            if tp1_price and not tp1_hit:
                tp1_reached = (is_bull and bar.high >= tp1_price) or (not is_bull and bar.low <= tp1_price)
                if tp1_reached:
                    # Floor (not round) — matches live execution_service behaviour.
                    close_vol = max(0.01, math.floor(remaining_vol * tp1_pct * 100) / 100)
                    profit_partial = ppnl(tp1_price, close_vol)
                    realized_pnl += profit_partial
                    remaining_vol -= close_vol
                    tp1_hit = True
                    profit_lock_dist = sl_dist * config.TP1_PROFIT_LOCK_R
                    if is_bull:
                        virtual_sl = entry + profit_lock_dist
                    else:
                        virtual_sl = entry - profit_lock_dist

            # ── TP2 ───────────────────────────────────────────────
            if tp1_hit and not tp2_hit and tp2_price and remaining_vol > 0:
                tp2_reached = (is_bull and bar.high >= tp2_price) or (not is_bull and bar.low <= tp2_price)
                if tp2_reached:
                    close_vol = max(0.01, math.floor(remaining_vol * tp2_pct * 100) / 100)
                    profit_partial = ppnl(tp2_price, close_vol)
                    realized_pnl += profit_partial
                    remaining_vol -= close_vol
                    tp2_hit = True
                    # Smart runner SL: scan M5 swings above TP1, use TP1 as floor.
                    if tp1_price:
                        virtual_sl = find_runner_protection_sl(
                            direction=signal.direction,
                            current_sl=virtual_sl,
                            floor_sl=tp1_price,
                            m5_window=m5_window,
                            pip_calc=pip_calc,
                            atr_price=monitor._atr,
                        )

            # ── TP1→TP2 corridor swing trail ──────────────────────
            # After TP1, the SL sits at the profit-lock level (entry+0.3R)
            # with no further adjustment until TP2 fires.  The monitor's
            # breakeven_trigger is gated by current_sl==original_sl so it
            # won't fire here.  This ratchets toward the nearest M5 swing
            # that is strictly better than the current SL, capturing more
            # profit if TP2 is never reached.  Floor = current SL so the
            # profit-lock guarantee is preserved.
            if tp1_hit and not tp2_hit and new_m5 and m5_window:
                mid_sl = find_runner_protection_sl(
                    direction=signal.direction,
                    current_sl=virtual_sl,
                    floor_sl=virtual_sl,
                    m5_window=m5_window,
                    pip_calc=pip_calc,
                    atr_price=monitor._atr,
                )
                if mid_sl != virtual_sl:
                    virtual_sl = mid_sl

            # ── Post-TP2 runner ratchet ───────────────────────────
            if tp2_hit and new_m5 and tp1_price and m5_window:
                virtual_sl = find_runner_protection_sl(
                    direction=signal.direction,
                    current_sl=virtual_sl,
                    floor_sl=tp1_price,
                    m5_window=m5_window,
                    pip_calc=pip_calc,
                    atr_price=monitor._atr,
                )

            # ── TP3: runner to liquidity pool ─────────────────────
            # Mirrors the MT5 take_profit order that auto-closes the
            # remaining volume when price reaches the liquidity target.
            # Without this the backtester never closes the runner on a
            # full-target hit — those trades continue until SL or timeout,
            # systematically under-reporting runner profits.
            # Fires when: (a) both TP1+TP2 hit and runner remains, or
            #             (b) TP1 hit but TP2 was skipped (pool < TP2 level).
            if remaining_vol > 0:
                _can_tp3 = tp2_hit or (tp1_hit and tp2_price is None)
                if _can_tp3:
                    tp3_reached = (
                        (is_bull and bar.high >= signal.take_profit) or
                        (not is_bull and bar.low <= signal.take_profit)
                    )
                    if tp3_reached:
                        profit_runner = ppnl(signal.take_profit, remaining_vol)
                        realized_pnl += profit_runner
                        remaining_vol = 0.0
                        return (
                            "WIN_FULL", realized_pnl,
                            meta(bar, exit_reason_str="TP3"),
                            all_evts,
                        )

            # ── SL check ──────────────────────────────────────────
            hit_sl = (is_bull and bar.low <= virtual_sl) or (not is_bull and bar.high >= virtual_sl)
            if hit_sl:
                if remaining_vol > 0:
                    loss_remaining = ppnl(virtual_sl, remaining_vol)
                    realized_pnl += loss_remaining
                    remaining_vol = 0.0
                exit_reason = "SL"
                total_pnl = realized_pnl
                if tp1_hit and tp2_hit:
                    outcome = "WIN_FULL"
                elif tp1_hit:
                    outcome = "WIN_PARTIAL"
                elif total_pnl > 0:
                    # SL was trailed above entry by monitor before TP1 hit;
                    # net result is profitable — not a loss.
                    outcome = "WIN_PARTIAL"
                else:
                    outcome = "LOSS"
                return outcome, total_pnl, meta(bar, exit_reason_str=exit_reason), all_evts

            # ── Monitor ───────────────────────────────────────────
            if not getattr(config, "MONITOR_ENABLED", True):
                continue

            evts, action, new_sl, should_close = monitor.update_dual(
                trade_id=signal.zone.id[:8],
                direction=signal.direction,
                entry=entry,
                current_sl=virtual_sl,
                original_sl=original_sl,
                take_profit=signal.take_profit,
                m1_bar=bar,
                m5_window=m5_window,
                opposing_zones=opposing,
                mfe_price=mfe_price,
                m1_bars_since_mfe=m1_bars_since_mfe,
                m5_bars_since_mfe=m5_no_mfe,
                new_m5_bar=new_m5,
                recent_signals=recent_sigs,
                tp1_hit=tp1_hit,
            )
            all_evts.extend(evts)

            if evts:
                now = bar.time
                win_mins = getattr(config, "MONITOR_CONFIRM_WINDOW_M5", 3) * 5
                cutoff = now - timedelta(minutes=win_mins)
                recent_sigs.extend((ev.signal_type, now) for ev in evts)
                recent_sigs = [(st, t) for st, t in recent_sigs if t >= cutoff]

            # mfe_stall exit is handled entirely inside trade_monitor.update_dual().
            # No duplicate logic here — live and backtest use the same code path.

            if action == "trail_sl" and new_sl != virtual_sl:
                virtual_sl = new_sl

            # Time‑based exit
            duration = (bar.time - start_time).total_seconds() / 60
            if duration > config.MAX_TRADE_DURATION_MINUTES:
                should_close = True
                action = "close_now"
                exit_reason = "timeout"

            if should_close:
                if remaining_vol > 0:
                    close_pnl = ppnl(bar.close, remaining_vol)
                    realized_pnl += close_pnl
                    remaining_vol = 0.0
                exit_reason = exit_reason or "MONITOR_CLOSE"
                total_pnl = realized_pnl
                outcome = "MONITOR_CLOSE"
                return outcome, total_pnl, meta(bar, exit_reason_str=exit_reason), all_evts

        # End of bars
        if remaining_vol > 0:
            close_pnl = ppnl(bars[-1].close, remaining_vol)
            realized_pnl += close_pnl
        total_pnl = realized_pnl
        outcome = "OPEN"
        return outcome, total_pnl, meta(bars[-1], exit_reason_str="END_OF_DATA"), all_evts

    def _build_windows(self, events):
        windows = []
        for e in events:
            impact = getattr(e, "impact", "low").lower()
            if impact == "high":
                buf = timedelta(minutes=config.HIGH_IMPACT_BUFFER_MINS)
            elif impact == "medium":
                buf = timedelta(minutes=config.MEDIUM_IMPACT_BUFFER_MINS)
            else:
                continue
            # Skip zero-width windows (MEDIUM_IMPACT_BUFFER_MINS = 0)
            if buf.total_seconds() == 0:
                continue
            windows.append(NoTradeWindow(
                start=e.event_time - buf, end=e.event_time + buf,
                currency=e.currency, event_name=e.event_name))
        return windows

    def _in_news(self, t, symbol, windows):
        ccy = config.SYMBOL_CURRENCIES.get(symbol, [])
        return any(w.currency in ccy and w.is_blocking(t) for w in windows)

    def _near_news(self, t, symbol, windows) -> bool:
        """
        Return True if *t* falls within NEWS_BUFFER_MINUTES of any news window
        boundary (i.e. the trade is news-adjacent even if not strictly blocked).
        Used to decide whether to apply NEWS_SLIPPAGE_PIPS in the backtester.
        """
        buf = timedelta(minutes=getattr(config, "NEWS_BUFFER_MINUTES", 15))
        ccy = config.SYMBOL_CURRENCIES.get(symbol, [])
        for w in windows:
            if w.currency not in ccy:
                continue
            # Widen the window by the buffer on both sides
            if (w.start - buf) <= t <= (w.end + buf):
                return True
        return False

    def print_summary(self, results, final_eq, start_date, end_date):
        wf = sum(1 for r in results if r.outcome == "WIN_FULL")
        wp = sum(1 for r in results if r.outcome == "WIN_PARTIAL")
        los = sum(1 for r in results if r.outcome == "LOSS")
        mc = sum(1 for r in results if r.outcome == "MONITOR_CLOSE")
        opn = sum(1 for r in results if r.outcome == "OPEN")
        nf = sum(1 for r in results if r.outcome == "NOT_FILLED")
        res = wf + wp + los + mc
        wr = (wf + wp) / res * 100 if res else 0
        avg_eff = sum(r.efficiency for r in results if r.efficiency > 0) / max(1, len(results))
        avg_score = sum(r.trade_score for r in results) / max(1, len(results))

        logger.info("═" * 65)
        logger.info("  BACKTEST v10  %s → %s", start_date, end_date)
        logger.info("  WIN_FULL=%d  WIN_PARTIAL=%d  LOSS=%d  MC=%d  OPEN=%d  NOT_FILLED=%d",
                    wf, wp, los, mc, opn, nf)
        logger.info("  Win rate (W_FULL+W_PARTIAL): %.1f%%  |  total resolved: %d", wr, res)
        logger.info("  Equity: %.0f %s  |  P&L: %+.0f", final_eq, self._currency, final_eq - self._account)
        logger.info("  Avg profit efficiency: %.1f%%", avg_eff)
        logger.info("  Avg trade score: %.1f", avg_score)
        tp1_n  = sum(1 for r in results if r.tp1_hit)
        tp2_n  = sum(1 for r in results if r.tp2_hit)
        tp3_n  = sum(1 for r in results if r.trade.exit_reason == "TP3")
        n      = len(results) or 1
        logger.info("  TP1 hit: %d / %d (%.0f%%)  TP2: %d / %d (%.0f%%)  TP3 (pool): %d / %d (%.0f%%)",
                    tp1_n, len(results), tp1_n / n * 100,
                    tp2_n, len(results), tp2_n / n * 100,
                    tp3_n, len(results), tp3_n / n * 100)
        avg_win = (sum(r.pnl_usd for r in results if r.pnl_usd > 0) /
                   max(1, sum(1 for r in results if r.pnl_usd > 0)))
        avg_loss = (sum(r.pnl_usd for r in results if r.pnl_usd < 0) /
                    max(1, sum(1 for r in results if r.pnl_usd < 0)))
        logger.info("  Avg win: %.0f %s  |  Avg loss: %.0f %s  |  Profit factor: %.2f",
                    avg_win, self._currency, avg_loss, self._currency,
                    abs(avg_win / avg_loss) if avg_loss != 0 else float("inf"))
        logger.info("═" * 65)

    def analyze_trade_features(self, results):
        if not results:
            return
        score_groups = {}
        kill_zone_groups = {True: [], False: []}
        for r in results:
            score = r.trade_score
            if score not in score_groups:
                score_groups[score] = []
            score_groups[score].append(r.pnl_usd)
            kill_zone_groups[r.kill_zone].append(r.pnl_usd)

        logger.info("═" * 65)
        logger.info("SELF‑LEARNING FEEDBACK")
        logger.info("Profit by trade score:")
        for score in sorted(score_groups.keys()):
            pnls = score_groups[score]
            avg = sum(pnls) / len(pnls)
            logger.info("  Score %d: %d trades, avg P&L = %.0f", score, len(pnls), avg)
        logger.info("Profit by kill zone:")
        for kz, pnls in kill_zone_groups.items():
            if pnls:
                avg = sum(pnls) / len(pnls)
                logger.info("  Kill zone %s: %d trades, avg P&L = %.0f", kz, len(pnls), avg)
        logger.info("═" * 65)

    def export_csv(self, results, filepath):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "id","symbol","direction","entry","sl","tp","lots",
                "outcome","pnl","pips","news_affected","slippage_pips","opened_at",
                "mfe_pips","pips_to_tp_at_mfe","duration_minutes",
                "reversal_zone_type","balance_before","balance_after","closed_at",
                "tp1_price","tp1_hit","tp2_price","tp2_hit",
                "mfe","mae","efficiency","realized_pnl","remaining_volume","exit_reason",
                "monitor_events_count","first_monitor_signal",
                "sl_trailed","trail_sl_value","early_close",
                "be_events_m1","zone_events_m1","choch_events_m5","stall_events_m5",
                "trade_score","kill_zone",
            ])
            for r in results:
                t = r.trade
                es = r.monitor_events
                w.writerow([
                    t.id, t.symbol, t.direction.value,
                    f"{t.entry_price:.5f}", f"{t.stop_loss:.5f}",
                    f"{t.take_profit:.5f}", f"{t.lot_size:.2f}",
                    r.outcome, f"{r.pnl_usd:.2f}", f"{r.pips_gain:.1f}",
                    r.news_affected, t.slippage_pips, t.created_at.isoformat(),
                    f"{r.mfe_pips:.1f}", f"{r.pips_to_tp_at_mfe:.1f}",
                    f"{r.duration_minutes:.0f}", r.reversal_zone_type or "",
                    f"{r.balance_before:.0f}", f"{r.balance_after:.0f}",
                    t.closed_at.isoformat() if t.closed_at else "",
                    f"{r.tp1_price:.5f}" if r.tp1_price else "", r.tp1_hit,
                    f"{r.tp2_price:.5f}" if r.tp2_price else "", r.tp2_hit,
                    f"{r.mfe:.1f}", f"{r.mae:.1f}", f"{r.efficiency:.1f}",
                    f"{r.realized_pnl:.2f}", f"{r.remaining_volume:.2f}", t.exit_reason,
                    r.monitor_events_count, r.first_monitor_signal,
                    r.sl_trailed,
                    f"{r.trail_sl_value:.5f}" if r.trail_sl_value else "",
                    r.early_close,
                    sum(1 for e in es if e.signal_type=="breakeven_trigger"),
                    sum(1 for e in es if e.signal_type=="opposing_zone"),
                    sum(1 for e in es if e.signal_type=="reverse_choch"),
                    sum(1 for e in es if e.signal_type=="mfe_stall"),
                    r.trade_score, r.kill_zone,
                ])
        logger.info("CSV → %s (%d rows)", filepath, len(results))