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
from src.application.diagnostics import DiagnosticsCollector
from src.application.trade_monitor import TradeMonitor, MonitorEvent, find_swing_trail_sl
from src.infrastructure.news_client import ForexNewsClient

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
        news_client:  ForexNewsClient,
        account:      float,
        risk_pct:     float,
        commission:   float,
        currency:     str,
        tick_values:  Optional[Dict[str, float]] = None,
    ) -> None:
        self._news        = news_client
        self._account     = account
        self._risk_pct    = risk_pct
        self._commission  = commission
        self._currency    = currency
        self._mapper      = ZoneMapper()
        self._tick_values = tick_values or {}

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
            h1 = td.get(Timeframe.H1, [])
            m5 = td.get(Timeframe.M5, []) or td.get(Timeframe.M1, [])
            m1 = td.get(Timeframe.M1, [])
            h4 = td.get(Timeframe.H4, [])
            if h1 and m5:
                sym_data[sym] = {"h1": h1, "m5": m5, "m1": m1, "h4": h4}

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
            r = self._process_h1_bar(sym, d["h1"], d["m5"], d["m1"], d["h4"],
                                     h1_idx, no_trade_windows, equity, diag)
            if r:
                equity += r.pnl_usd
                results.append(r)

        self.print_summary(results, equity, start_date, end_date)
        self.analyze_trade_features(results)
        return results

    def _process_h1_bar(self, symbol, h1, m5, m1, h4,
                        h1_idx, ntw, equity, diag):
        digits = config.SYMBOL_DIGITS.get(symbol, 5)
        tv = self._get_tick_value(symbol)
        pip_calc = PipCalculator(digits=digits)
        h1_win = h1[h1_idx - 100: h1_idx]
        zones, pool = self._mapper.map_zones(h1_win, symbol, Timeframe.H1)
        bar_time = h1[h1_idx].time

        m5_win = [c for c in m5 if bar_time <= c.time < bar_time + timedelta(hours=3)]

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
        bar_px = h1[h1_idx].open
        h1_atr = atr(h1_win)
        zbuf = calculate_zone_buffer(h1_atr)

        if self._in_news(bar_time, symbol, ntw):
            if diag: diag.record_rejection(symbol, "news_block")
            return None
        if not is_in_trading_session(bar_time):
            if diag: diag.record_rejection(symbol, "outside_session")
            return None

        bc = BrokerCost(spread_pips=1.0, commission_usd=self._commission, tick_value=tv)

        sym_min_score = config.SYMBOL_MIN_SCORE.get(symbol, config.SETUP_SCORE_THRESHOLD)

        for zone in zones:
            setup_score = score_setup(zone.direction, h4_bias, bar_px, h4_eq)
            if setup_score < sym_min_score:
                if diag: diag.record_rejection(symbol, "below_score_threshold")
                continue
            grade, rpct, min_rr = grade_setup(setup_score)

            signal = find_m5_choch_signal(
                m5_candles=m5_win, zone=zone, pip_calc=pip_calc,
                spread_pips=bc.spread_pips, liquidity_pool=pool,
                atr_price=h1_atr, diag=diag,
                zone_buffer=zbuf, min_rr_override=min_rr,
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

            # Trade Scoring
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
                if diag: diag.record_rejection(symbol, "low_trade_score")
                continue

            risk_pct = dynamic_risk(trade_score)

            # Dynamic TP
            baseline_atr = atr(h1_win[-50:]) if len(h1_win) >= 50 else h1_atr
            tp1_ratio, tp2_ratio = compute_dynamic_tp(
                signal.direction, signal.entry_price, signal.stop_loss,
                h1_atr, baseline_atr
            )
            # Override with config values
            tp1_ratio = config.TIERED_TP1_RATIO
            tp2_ratio = config.TIERED_TP2_RATIO

            tp1_price = (signal.entry_price + tp1_ratio * abs(signal.entry_price - signal.stop_loss)
                         if signal.direction == Direction.BULLISH
                         else signal.entry_price - tp1_ratio * abs(signal.entry_price - signal.stop_loss))
            tp2_price = (signal.entry_price + tp2_ratio * abs(signal.entry_price - signal.stop_loss)
                         if signal.direction == Direction.BULLISH
                         else signal.entry_price - tp2_ratio * abs(signal.entry_price - signal.stop_loss))

            # Position size
            rp = RiskParameters(account_balance=equity, risk_percent=risk_pct,
                                commission_per_lot=self._commission,
                                min_rr_ratio=min_rr,
                                account_currency=self._currency)
            lot = max(0.01, min(round(rp.lot_size(sl_pips, tv, pip_calc), 2), 100.0))
            ram = equity * (risk_pct / 100)

            max_m1 = config.MAX_FORWARD_M1_BARS
            fm1 = [c for c in m1 if c.time >= signal.created_at][:max_m1]
            fm5 = [c for c in m5 if c.time >= signal.created_at][:max_m1 // 5]

            monitor = TradeMonitor(pip_calc=pip_calc, atr_price=h1_atr, symbol=symbol)

            outcome, pnl, meta, mon_evts = self._simulate(
                signal=signal, entry=signal.entry_price,
                future_m1=fm1, future_m5=fm5,
                risk_amt=ram, lot_size=lot, tick_value=tv,
                pip_calc=pip_calc, zones=zones, monitor=monitor,
                tp1_price=tp1_price, tp2_price=tp2_price,
                trade_score=trade_score, tp1_ratio=tp1_ratio, tp2_ratio=tp2_ratio,
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
                entry_price=signal.entry_price, stop_loss=signal.stop_loss,
                take_profit=signal.take_profit, lot_size=lot,
                status=TradeStatus.CLOSED, created_at=signal.created_at,
                closed_at=meta.get("closed_at"), pnl=pnl,
                commission=self._commission * lot,
                news_affected=False, slippage_pips=0.0,
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

            return BacktestResult(
                trade=trade, outcome=outcome, pnl_usd=pnl,
                pips_gain=pip_calc.price_to_pips(abs(
                    signal.take_profit - signal.entry_price
                    if outcome in ("WIN_FULL",) else signal.stop_loss - signal.entry_price)),
                news_affected=False,
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
    ):
        use_m1 = bool(future_m1)
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
        comm = self._commission * lot_size

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
                    close_vol = round(remaining_vol * tp1_pct, 2)
                    close_vol = max(0.01, close_vol)
                    profit_partial = ppnl(tp1_price, close_vol)
                    realized_pnl += profit_partial
                    remaining_vol -= close_vol
                    tp1_hit = True
                    profit_lock_dist = sl_dist * 0.3
                    if is_bull:
                        virtual_sl = entry + profit_lock_dist
                    else:
                        virtual_sl = entry - profit_lock_dist

            # ── TP2 ───────────────────────────────────────────────
            if tp1_hit and not tp2_hit and tp2_price:
                tp2_reached = (is_bull and bar.high >= tp2_price) or (not is_bull and bar.low <= tp2_price)
                if tp2_reached:
                    close_vol = round(remaining_vol * tp2_pct, 2)
                    close_vol = max(0.01, close_vol)
                    profit_partial = ppnl(tp2_price, close_vol)
                    realized_pnl += profit_partial
                    remaining_vol -= close_vol
                    tp2_hit = True

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
            )
            all_evts.extend(evts)

            if evts:
                now = bar.time
                win_mins = getattr(config, "MONITOR_CONFIRM_WINDOW_M5", 3) * 5
                cutoff = now - timedelta(minutes=win_mins)
                recent_sigs.extend((ev.signal_type, now) for ev in evts)
                recent_sigs = [(st, t) for st, t in recent_sigs if t >= cutoff]

            # Stall exit with conditions (profit > min ratio, not strong trend)
            stall_event = next((e for e in evts if e.signal_type == "mfe_stall"), None)
            if stall_event and getattr(config, "STALL_EXIT_REQUIRES_TP1", True):
                current_profit_ratio = (abs(bar.close - entry) / sl_dist) if sl_dist > 0 else 0
                min_profit_ratio = getattr(config, "MIN_PROFIT_RATIO_TO_CLOSE", 1.5)
                if tp1_hit and not is_strong_trend(m5_window, signal.direction) and current_profit_ratio >= min_profit_ratio:
                    should_close = True
                    action = "close_now"
                    exit_reason = "stall_exit"

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

    def print_summary(self, results, final_eq, start_date, end_date):
        wf = sum(1 for r in results if r.outcome == "WIN_FULL")
        wp = sum(1 for r in results if r.outcome == "WIN_PARTIAL")
        los = sum(1 for r in results if r.outcome == "LOSS")
        mc = sum(1 for r in results if r.outcome == "MONITOR_CLOSE")
        opn = sum(1 for r in results if r.outcome == "OPEN")
        res = wf + wp + los + mc
        wr = (wf + wp) / res * 100 if res else 0
        avg_eff = sum(r.efficiency for r in results if r.efficiency > 0) / max(1, len(results))
        avg_score = sum(r.trade_score for r in results) / max(1, len(results))

        logger.info("═" * 65)
        logger.info("  BACKTEST v10  %s → %s", start_date, end_date)
        logger.info("  WIN_FULL=%d  WIN_PARTIAL=%d  LOSS=%d  MC=%d  OPEN=%d",
                    wf, wp, los, mc, opn)
        logger.info("  Win rate (W_FULL+W_PARTIAL): %.1f%%  |  total resolved: %d", wr, res)
        logger.info("  Equity: %.0f %s  |  P&L: %+.0f", final_eq, self._currency, final_eq - self._account)
        logger.info("  Avg profit efficiency: %.1f%%", avg_eff)
        logger.info("  Avg trade score: %.1f", avg_score)
        logger.info("  TP1 hit: %d / %d trades (%.0f%%)",
                    sum(1 for r in results if r.tp1_hit), len(results),
                    sum(1 for r in results if r.tp1_hit) / len(results) * 100 if results else 0)
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