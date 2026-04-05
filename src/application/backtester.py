"""
backtester.py — Phase 4: Historical Simulation  (v5 — Smart Monitor)

KEY CHANGE FROM v4: swing-trail instead of hard entry BE
───────────────────────────────────────────────────────────────────────────────
When the monitor fires a trail action, the virtual SL is moved to the most
recent M5 swing low (for long) or swing high (for short) that sits above the
original SL — NOT to the raw entry price.

This prevents the simulation from penalising normal retraces.  A trade that
reaches 25 pips profit, retraces to 8 pips (still above a swing low at 12
pips), then continues to TP — is recorded as WIN, not BREAKEVEN.

CONVICTION THRESHOLDS
───────────────────────────────────────────────────────────────────────────────
  MONITOR_TRAIL_CONVICTION = 1  → trail SL to swing (low conviction)
  MONITOR_CLOSE_CONVICTION = 3  → close early (high conviction)

The simulation uses the same conviction rules as live trading so backtest
results directly predict live behaviour.

DUAL TIMEFRAME SIMULATION
───────────────────────────────────────────────────────────────────────────────
Primary loop: M1 bars (SL/TP/MFE checked every minute)
M5 cursor:    advances when M1 time passes a M5 close, enables structural checks
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
    Timeframe, Trade, TradeStatus, Zone,
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
)
from src.application.diagnostics import DiagnosticsCollector
from src.application.trade_monitor import TradeMonitor, MonitorEvent, find_swing_trail_sl
from src.infrastructure.news_client import ForexNewsClient

logger = logging.getLogger(__name__)

_M5 = timedelta(minutes=5)


# ─────────────────────────────────────────────
#  Result container
# ─────────────────────────────────────────────

@dataclass
class BacktestResult:
    trade:              Trade
    outcome:            str        # WIN|LOSS|OPEN|MONITOR_CLOSE
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
    monitor_events_count:   int  = 0
    first_monitor_signal:   str  = ""
    sl_trailed:             bool = False   # SL was moved to swing structure
    trail_sl_value:         float = 0.0   # what it was moved to
    early_close:            bool = False


# ─────────────────────────────────────────────
#  Backtester
# ─────────────────────────────────────────────

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

    # ── run() ────────────────────────────────────────────────────

    def run(self, candle_data, start_date, end_date, diag=None):
        logger.info("Backtest v5-smart: %s → %s", start_date, end_date)
        results, equity = [], self._account

        if diag:
            for sym, td in candle_data.items():
                diag.record_mt5_data(sym, td.get(Timeframe.H1, []),
                                     td.get(Timeframe.M1, []))

        currencies       = list({c for cs in config.SYMBOL_CURRENCIES.values() for c in cs})
        news_events      = self._fetch_news_chunked(currencies, start_date, end_date)
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

        H1W  = 100
        s_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        e_dt = datetime.strptime(end_date,   "%Y-%m-%d").replace(tzinfo=timezone.utc)

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
            r = self._process_h1_bar(
                sym, d["h1"], d["m5"], d["m1"], d["h4"],
                h1_idx, no_trade_windows, equity, diag)
            if r:
                equity += r.pnl_usd
                results.append(r)

        self.print_summary(results, equity, start_date, end_date)
        return results

    # ── Per-bar ───────────────────────────────────────────────────

    def _process_h1_bar(self, symbol, h1, m5, m1, h4,
                        h1_idx, ntw, equity, diag):
        digits    = config.SYMBOL_DIGITS.get(symbol, 5)
        tv        = self._get_tick_value(symbol)
        pip_calc  = PipCalculator(digits=digits)
        h1_win    = h1[h1_idx - 100: h1_idx]
        zones, pool = self._mapper.map_zones(h1_win, symbol, Timeframe.H1)
        bar_time  = h1[h1_idx].time

        m5_win = [c for c in m5
                  if bar_time <= c.time < bar_time + timedelta(hours=3)]

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

        h4_win  = [c for c in h4 if c.time < bar_time][-config.H4_CANDLE_COUNT:]
        h4_bias = find_h4_bias(h4_win) if len(h4_win) >= 10 else None
        h4_eq   = compute_h4_equilibrium(h4_win)[2] if len(h4_win) >= 4 else None
        bar_px  = h1[h1_idx].open
        h1_atr  = atr(h1_win)
        zbuf    = calculate_zone_buffer(h1_atr)

        if self._in_news(bar_time, symbol, ntw):
            if diag: diag.record_rejection(symbol, "news_block")
            return None
        if not is_in_trading_session(bar_time):
            if diag: diag.record_rejection(symbol, "outside_session")
            return None

        bc = BrokerCost(spread_pips=1.0, commission_usd=self._commission,
                        tick_value=tv)

        for zone in zones:
            sc = score_setup(zone.direction, h4_bias, bar_px, h4_eq)
            if sc < config.SETUP_SCORE_THRESHOLD:
                if diag: diag.record_rejection(symbol, "below_score_threshold")
                continue
            grade, rpct, min_rr = grade_setup(sc)

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

            rp  = RiskParameters(account_balance=equity, risk_percent=rpct,
                                  commission_per_lot=self._commission,
                                  min_rr_ratio=min_rr,
                                  account_currency=self._currency)
            slp = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
            lot = max(0.01, min(round(rp.lot_size(slp, tv, pip_calc), 2), 100.0))
            ram = equity * (rpct / 100)

            max_m1 = config.MAX_FORWARD_M1_BARS
            fm1 = [c for c in m1 if c.time >= signal.created_at][:max_m1]
            fm5 = [c for c in m5 if c.time >= signal.created_at][:max_m1 // 5]

            monitor = TradeMonitor(pip_calc=pip_calc, atr_price=h1_atr, symbol=symbol)

            outcome, pnl, meta, mon_evts = self._simulate(
                signal=signal, entry=signal.entry_price,
                future_m1=fm1, future_m5=fm5,
                risk_amt=ram, lot_size=lot, tick_value=tv,
                pip_calc=pip_calc, zones=zones, monitor=monitor,
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
                    ev.was_correct   = outcome in ("LOSS", "MONITOR_CLOSE")
                    diag.record_monitor_event(ev)

            trail_ev = next((e for e in mon_evts
                             if e.action_taken == "trailed_sl"), None)
            sl_trailed  = trail_ev is not None
            trail_val   = trail_ev.new_sl if trail_ev else 0.0
            early_close = any(e.action_taken == "closed_early" for e in mon_evts)
            first_s     = mon_evts[0].signal_type if mon_evts else ""
            bb, ba      = equity, equity + pnl
            tid         = f"bt_{h1_idx}_{zone.id[:8]}"

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
            )

            logger.info(
                "BT | %s %s | %s | MFE=%.1f | left=%.1f | "
                "trail=%s(%.5f) | early=%s | mon=%d",
                symbol, signal.direction.value, outcome,
                meta.get("mfe_pips", 0), meta.get("pips_to_tp_at_mfe", 0),
                sl_trailed, trail_val, early_close, len(mon_evts),
            )

            return BacktestResult(
                trade=trade, outcome=outcome, pnl_usd=pnl,
                pips_gain=pip_calc.price_to_pips(abs(
                    signal.take_profit - signal.entry_price
                    if outcome == "WIN" else signal.stop_loss - signal.entry_price)),
                news_affected=False,
                mfe_pips=meta.get("mfe_pips", 0.0),
                pips_to_tp_at_mfe=meta.get("pips_to_tp_at_mfe", 0.0),
                duration_minutes=meta.get("duration_minutes", 0.0),
                reversal_zone_type=meta.get("reversal_zone_type"),
                balance_before=bb, balance_after=ba,
                monitor_events=mon_evts, monitor_events_count=len(mon_evts),
                first_monitor_signal=first_s,
                sl_trailed=sl_trailed, trail_sl_value=trail_val,
                early_close=early_close,
            )

        return None

    # ── Simulation loop ───────────────────────────────────────────

    def _simulate(
        self,
        signal, entry,
        future_m1, future_m5,
        risk_amt, lot_size, tick_value,
        pip_calc, zones, monitor,
    ):
        """
        M1 primary walk with M5 cursor.
        Virtual SL starts at signal.stop_loss.
        Monitor can trail it to a swing structure level.
        Monitor can close early on high conviction.
        Falls back to M5-only if no M1 data.
        """
        use_m1 = bool(future_m1)
        bars   = future_m1 if use_m1 else future_m5
        if not bars:
            return "OPEN", 0.0, {
                "mfe_price": entry, "mfe_pips": 0.0,
                "pips_to_tp_at_mfe": 0.0, "closed_at": signal.created_at,
                "duration_minutes": 0.0, "reversal_zone_type": None,
            }, []

        is_bull     = signal.direction == Direction.BULLISH
        virtual_sl  = signal.stop_loss    # may be trailed (NOT to entry — to swing)
        original_sl = signal.stop_loss
        mfe_price   = entry
        all_evts: List[MonitorEvent] = []
        recent_sigs: List[Tuple] = []

        sl_pips   = pip_calc.price_to_pips(abs(entry - signal.stop_loss))
        comm      = self._commission * lot_size
        win_pnl   =  risk_amt * signal.risk_reward - comm
        loss_pnl  = -(risk_amt + comm)

        # M5 cursor state
        m5_idx    = 0
        m5_window: List[Candle] = []
        m5_no_mfe = 0
        m5_had_mfe = True   # did MFE update in the last complete M5 bar?

        # Pre-scan opposing zones
        opposing = monitor.scan_opposing_zones(
            signal.direction, entry, signal.take_profit, zones)

        def ppnl(close_px):
            pp   = pip_calc.price_to_pips(abs(close_px - entry))
            good = ((is_bull and close_px > entry) or
                    (not is_bull and close_px < entry))
            return (1 if good else -1) * (pp / max(sl_pips, 0.001)) * risk_amt - comm

        def meta(bar, rzt=None):
            mp = pip_calc.price_to_pips(abs(mfe_price - entry))
            lp = pip_calc.price_to_pips(abs(signal.take_profit - mfe_price))
            dm = (bar.time - signal.created_at).total_seconds() / 60
            return {"mfe_price": mfe_price, "mfe_pips": mp,
                    "pips_to_tp_at_mfe": lp, "closed_at": bar.time,
                    "duration_minutes": dm, "reversal_zone_type": rzt}

        for i, bar in enumerate(bars):
            # Advance M5 cursor
            new_m5 = False
            if use_m1:
                while m5_idx < len(future_m5) and \
                        bar.time >= future_m5[m5_idx].time + _M5:
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

            # Update MFE
            if is_bull and bar.high > mfe_price:
                mfe_price = bar.high; m5_no_mfe = 0; m5_had_mfe = True
            elif not is_bull and bar.low < mfe_price:
                mfe_price = bar.low;  m5_no_mfe = 0; m5_had_mfe = True

            # Check virtual SL / TP
            hit_sl = (bar.low  <= virtual_sl if is_bull else bar.high >= virtual_sl)
            hit_tp = (bar.high >= signal.take_profit if is_bull
                      else bar.low <= signal.take_profit)

            if hit_sl:
                rzt = _find_rzt(mfe_price, zones, signal.direction)
                return "LOSS", loss_pnl, meta(bar, rzt), all_evts
            if hit_tp:
                return "WIN", win_pnl, meta(bar), all_evts

            # Monitor
            if not getattr(config, "MONITOR_ENABLED", True):
                continue

            m1_since = i  # approximate
            evts, action, new_sl, should_close = monitor.update_dual(
                trade_id=          signal.zone.id[:8],
                direction=         signal.direction,
                entry=             entry,
                current_sl=        virtual_sl,
                original_sl=       original_sl,
                take_profit=       signal.take_profit,
                m1_bar=            bar,
                m5_window=         m5_window,
                opposing_zones=    opposing,
                mfe_price=         mfe_price,
                m1_bars_since_mfe= m1_since,
                m5_bars_since_mfe= m5_no_mfe,
                new_m5_bar=        new_m5,
                recent_signals=    recent_sigs,
            )
            all_evts.extend(evts)

            # Update conviction window
            if evts:
                now = bar.time
                win_mins = getattr(config, "MONITOR_CONFIRM_WINDOW_M5", 3) * 5
                cutoff   = now - timedelta(minutes=win_mins)
                recent_sigs.extend((ev.signal_type, now) for ev in evts)
                recent_sigs = [(st, t) for st, t in recent_sigs if t >= cutoff]

            # Apply trail (swing, not entry)
            if action == "trail_sl" and new_sl != virtual_sl:
                virtual_sl = new_sl

            if should_close:
                return "MONITOR_CLOSE", ppnl(bar.close), meta(bar), all_evts

        return "OPEN", 0.0, meta(bars[-1]), all_evts

    # ── Helpers ───────────────────────────────────────────────────

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
            windows.append(NoTradeWindow(
                start=e.event_time - buf, end=e.event_time + buf,
                currency=e.currency, event_name=e.event_name))
        return windows

    def _in_news(self, t, symbol, windows):
        ccy = config.SYMBOL_CURRENCIES.get(symbol, [])
        return any(w.currency in ccy and w.is_blocking(t) for w in windows)

    # ── Summary + CSV ─────────────────────────────────────────────

    def print_summary(self, results, final_eq, start_date, end_date):
        wins = sum(1 for r in results if r.outcome == "WIN")
        loss = sum(1 for r in results if r.outcome == "LOSS")
        mc   = sum(1 for r in results if r.outcome == "MONITOR_CLOSE")
        opn  = sum(1 for r in results if r.outcome == "OPEN")
        res  = wins + loss + mc
        wr   = wins / res * 100 if res else 0
        trailed = sum(1 for r in results if r.sl_trailed)
        all_e   = [ev for r in results for ev in r.monitor_events]

        losers   = [r for r in results if r.outcome == "LOSS"]
        avg_mfe  = sum(r.mfe_pips for r in losers) / len(losers) if losers else 0
        avg_left = sum(r.pips_to_tp_at_mfe for r in losers) / len(losers) if losers else 0

        logger.info("═" * 60)
        logger.info("  BACKTEST v5-smart  %s → %s", start_date, end_date)
        logger.info("  W:%d L:%d MC:%d O:%d  WR:%.1f%%",
                    wins, loss, mc, opn, wr)
        logger.info("  Equity: %.0f %s  P&L: %+.0f",
                    final_eq, self._currency, final_eq - self._account)
        logger.info("  SL trailed to swing: %d trades", trailed)
        logger.info("  Loss MFE avg: %.1f  left avg: %.1f", avg_mfe, avg_left)
        logger.info("  Monitor events: BE=%d Zone=%d ChoCh=%d Stall=%d",
                    sum(1 for e in all_e if e.signal_type == "breakeven_trigger"),
                    sum(1 for e in all_e if e.signal_type == "opposing_zone"),
                    sum(1 for e in all_e if e.signal_type == "reverse_choch"),
                    sum(1 for e in all_e if e.signal_type == "mfe_stall"))
        logger.info("═" * 60)

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
                "monitor_events_count","first_monitor_signal",
                "sl_trailed","trail_sl_value","early_close",
                "be_events_m1","zone_events_m1","choch_events_m5","stall_events_m5",
            ])
            for r in results:
                t  = r.trade
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
                    r.monitor_events_count, r.first_monitor_signal,
                    r.sl_trailed, f"{r.trail_sl_value:.5f}" if r.trail_sl_value else "",
                    r.early_close,
                    sum(1 for e in es if e.signal_type=="breakeven_trigger"),
                    sum(1 for e in es if e.signal_type=="opposing_zone"),
                    sum(1 for e in es if e.signal_type=="reverse_choch"),
                    sum(1 for e in es if e.signal_type=="mfe_stall"),
                ])
        logger.info("CSV → %s (%d rows)", filepath, len(results))


def _find_rzt(mfe_price, zones, direction):
    opp = Direction.BEARISH if direction == Direction.BULLISH else Direction.BULLISH
    for z in zones:
        if z.direction == opp and z.is_active and z.low <= mfe_price <= z.high:
            return z.zone_type.value
    return "no_zone"