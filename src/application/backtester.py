"""
backtester.py — Phase 4: Historical Simulation.

UPGRADED (v3):
  • Uses M5 candles + find_m5_choch_signal()  (primary path)
  • Weighted Scoring System replaces hard H4 bias / premium-discount kills
    — bias_conflict and premium_skip now reduce risk % instead of skipping
  • ATR zone buffer passed to find_m5_choch_signal() (fuzzy tap detection)
  • Grade-aware risk sizing: A+ = 1.0%, B = 0.5%, C = 0.25%
  • Grade-aware RR floor: C-grade scalps use COUNTER_TREND_MIN_RR (1.5)
  • Dynamic ATR-based SL buffer passed through from H1 ATR
  • Still feeds DiagnosticsCollector for the full report

Walk-forward backtest. Feeds all data into DiagnosticsCollector
so the full diagnostic report can be written at the end.
"""
from __future__ import annotations
import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import src.config as config
from src.domain.entities import (
    Candle, Direction, LiquidityPool, NewsEvent, NoTradeWindow,
    Timeframe, Trade, TradeStatus,
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
    is_in_trading_session,  # NEW: session gate
)
from src.application.diagnostics import DiagnosticsCollector
from src.infrastructure.news_client import ForexNewsClient

logger = logging.getLogger(__name__)


class BacktestResult:
    def __init__(self, trade, outcome, pnl_usd, pips_gain, news_affected):
        self.trade         = trade
        self.outcome       = outcome
        self.pnl_usd       = pnl_usd
        self.pips_gain     = pips_gain
        self.news_affected = news_affected


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
        usd_val = config.TICK_VALUE_USD.get(symbol, 10.0)
        scale   = config.KES_PER_USD if self._currency == "KES" else 1.0
        return usd_val * scale

    # ── News Fetching (chunked) ───────────────────────────────────

    def _fetch_news_chunked(
        self,
        currencies: List[str],
        start_date: str,
        end_date:   str,
        chunk_days: int = 7,
    ) -> List[NewsEvent]:
        all_events: List[NewsEvent] = []
        current    = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt     = datetime.strptime(end_date,   "%Y-%m-%d")
        total_chunks = 0

        logger.info("Fetching MyFxBook news in %d-day chunks: %s → %s",
                    chunk_days, start_date, end_date)

        while current <= end_dt:
            chunk_end       = min(current + timedelta(days=chunk_days - 1), end_dt)
            chunk_start_str = current.strftime("%Y-%m-%d")
            chunk_end_str   = chunk_end.strftime("%Y-%m-%d")
            chunk_events    = self._news.get_range_events(
                currencies=currencies,
                start_date=chunk_start_str,
                end_date=  chunk_end_str,
            )
            logger.info("  Chunk %s → %s : %d events",
                        chunk_start_str, chunk_end_str, len(chunk_events))
            all_events.extend(chunk_events)
            total_chunks += 1
            current = chunk_end + timedelta(days=1)

        high_count   = sum(1 for e in all_events if e.impact.lower() == "high")
        medium_count = sum(1 for e in all_events if e.impact.lower() == "medium")
        logger.info("MyFxBook total: %d events across %d chunks "
                    "(%d high-impact, %d medium-impact)",
                    len(all_events), total_chunks, high_count, medium_count)
        return all_events

    # ── Main Entry Point ──────────────────────────────────────────

    def run(
        self,
        candle_data: Dict[str, Dict[Timeframe, List[Candle]]],
        start_date:  str,
        end_date:    str,
        diag:        Optional[DiagnosticsCollector] = None,
    ) -> List[BacktestResult]:

        logger.info("Backtest: %s → %s", start_date, end_date)
        results = []
        equity  = self._account

        if diag:
            for symbol, tf_data in candle_data.items():
                h1 = tf_data.get(Timeframe.H1, [])
                m1 = tf_data.get(Timeframe.M1, [])   # kept for coverage report
                diag.record_mt5_data(symbol, h1, m1)

        all_currencies = list({c for cs in config.SYMBOL_CURRENCIES.values() for c in cs})
        news_events    = self._fetch_news_chunked(
            currencies=all_currencies,
            start_date=start_date,
            end_date=  end_date,
        )
        no_trade_windows = self._build_windows(news_events)

        if diag:
            high_impact   = [e for e in news_events if e.impact.lower() == "high"]
            medium_impact = [e for e in news_events if e.impact.lower() == "medium"]
            diag.record_news_fetch(
                api_name=     "MyFxBook",
                start_date=   start_date,
                end_date=     end_date,
                currencies=   all_currencies,
                total_events= len(news_events),
                high_impact=  len(high_impact),
            )
            logger.info(
                "News loaded: %d total | %d high-impact (±%dm) | %d medium-impact (±%dm)",
                len(news_events), len(high_impact),   config.HIGH_IMPACT_BUFFER_MINS,
                len(medium_impact), config.MEDIUM_IMPACT_BUFFER_MINS,
            )
            for w in no_trade_windows:
                diag.record_no_trade_window(
                    currency=   w.currency,
                    event_name= w.event_name,
                    start=      w.start.strftime("%Y-%m-%d %H:%M"),
                    end=        w.end.strftime("%Y-%m-%d %H:%M"),
                )

        logger.info("Loaded %d no-trade windows (high + medium impact).",
                    len(no_trade_windows))

        # ── Simulate each symbol ──────────────────────────────────
        for symbol, tf_data in candle_data.items():
            h1 = tf_data.get(Timeframe.H1, [])
            m5 = tf_data.get(Timeframe.M5, [])   # ← NEW: M5 replaces M1 as primary
            h4 = tf_data.get(Timeframe.H4, [])   # ← NEW: H4 for bias filter

            if not h1 or not m5:
                # Fall back to M1 data if M5 is not available in candle_data
                m5_fallback = tf_data.get(Timeframe.M1, [])
                if not h1 or not m5_fallback:
                    logger.warning("Skipping %s — missing H1 or M5 data.", symbol)
                    continue
                logger.warning(
                    "%s: M5 data not found — falling back to M1 for this run. "
                    "Re-fetch with Timeframe.M5 for best results.", symbol
                )
                m5 = m5_fallback

            sym_results = self._simulate_symbol(
                symbol, h1, m5, h4, no_trade_windows, equity, diag,
            )
            results.extend(sym_results)
            for r in sym_results:
                equity += r.pnl_usd

        self.print_summary(results, equity, start_date, end_date)
        return results

    # ── Per-symbol Walk-Forward ───────────────────────────────────

    def _simulate_symbol(
        self,
        symbol:    str,
        h1:        List[Candle],
        m5:        List[Candle],
        h4:        List[Candle],
        windows:   List[NoTradeWindow],
        equity:    float,
        diag:      Optional[DiagnosticsCollector],
    ) -> List[BacktestResult]:

        results    = []
        digits     = config.SYMBOL_DIGITS.get(symbol, 5)
        tick_value = self._get_tick_value(symbol)
        pip_calc   = PipCalculator(digits=digits)
        H1_WINDOW  = 100
        H4_WINDOW  = config.H4_CANDLE_COUNT

        for h1_idx in range(H1_WINDOW, len(h1)):
            h1_window   = h1[h1_idx - H1_WINDOW: h1_idx]
            zones, pool = self._mapper.map_zones(h1_window, symbol, Timeframe.H1)

            h1_bar_time   = h1[h1_idx].time
            next_bar_time = (h1[h1_idx + 1].time if h1_idx + 1 < len(h1)
                             else h1_bar_time + timedelta(hours=1))

            # ── M5 window — 3-hour look-forward ──────────────────
            M5_LOOK_FORWARD = timedelta(hours=3)
            m5_window = [
                c for c in m5
                if h1_bar_time <= c.time < h1_bar_time + M5_LOOK_FORWARD
            ]

            if diag:
                diag.record_h1_bar(
                    symbol=    symbol,
                    m1_count=  len(m5_window) * 5,
                    ob_count=  len([z for z in zones if z.zone_type.value == "order_block"]),
                    fvg_count= len([z for z in zones if z.zone_type.value == "fvg"]),
                    has_pool=  pool is not None,
                )

            if len(m5_window) < 3 or not zones or not pool:
                continue

            # ── H4 context (informational, not a kill switch) ─────
            h4_window = [c for c in h4 if c.time < h1_bar_time][-H4_WINDOW:]
            h4_bias   = find_h4_bias(h4_window) if len(h4_window) >= 10 else None
            h4_eq     = (
                compute_h4_equilibrium(h4_window)[2]
                if len(h4_window) >= 4 else None
            )

            # Approximate current price as the open of the H1 bar
            bar_price = h1[h1_idx].open

            # ── H1 ATR + zone buffer ──────────────────────────────
            h1_atr      = atr(h1_window)
            zone_buffer = calculate_zone_buffer(h1_atr)

            # ── News block ────────────────────────────────────────
            if self._in_news_window(h1_bar_time, symbol, windows):
                logger.debug("NEWS BLOCK (backtest) | %s | bar=%s",
                             symbol, h1_bar_time.strftime("%Y-%m-%d %H:%M"))
                if diag:
                    diag.record_rejection(symbol, "news_block")
                continue

            # ── Session filter ────────────────────────────────────
            # Only trade London (08-16 UTC) and New York (13-21 UTC).
            if not is_in_trading_session(h1_bar_time):
                if diag:
                    diag.record_rejection(symbol, "outside_session")
                continue

            broker_cost = BrokerCost(
                spread_pips=    1.0,
                commission_usd= self._commission,
                tick_value=     tick_value,
            )

            for zone in zones:
                # ── Score the setup ───────────────────────────────
                setup_score             = score_setup(zone.direction, h4_bias, bar_price, h4_eq)

                # Hard threshold — skip setups below minimum quality
                if setup_score < config.SETUP_SCORE_THRESHOLD:
                    if diag:
                        diag.record_rejection(symbol, "below_score_threshold")
                    continue

                grade, risk_pct, min_rr = grade_setup(setup_score)

                if diag:
                    # Record what used to be hard-rejected for comparison
                    if h4_bias is not None and zone.direction != h4_bias:
                        diag.record_rejection(symbol, f"grade_C_bias_{grade}")
                    elif h4_eq is not None:
                        if zone.direction == Direction.BULLISH and bar_price > h4_eq:
                            diag.record_rejection(symbol, f"grade_B_premium_{grade}")
                        elif zone.direction == Direction.BEARISH and bar_price < h4_eq:
                            diag.record_rejection(symbol, f"grade_B_discount_{grade}")

                signal = find_m5_choch_signal(
                    m5_candles=      m5_window,
                    zone=            zone,
                    pip_calc=        pip_calc,
                    spread_pips=     broker_cost.spread_pips,
                    liquidity_pool=  pool,
                    atr_price=       h1_atr,
                    diag=            diag,
                    zone_buffer=     zone_buffer,
                    min_rr_override= min_rr,
                )
                if signal is None:
                    continue

                if self._in_news_window(signal.created_at, symbol, windows):
                    logger.debug("NEWS BLOCK (signal) | %s | signal=%s",
                                 symbol, signal.created_at.strftime("%Y-%m-%d %H:%M"))
                    if diag:
                        diag.record_rejection(symbol, "news_block")
                    continue

                risk_params = RiskParameters(
                    account_balance=    equity,
                    risk_percent=       risk_pct,   # grade-aware risk
                    commission_per_lot= self._commission,
                    min_rr_ratio=       min_rr,
                    account_currency=   self._currency,
                )
                sl_pips  = pip_calc.price_to_pips(abs(signal.entry_price - signal.stop_loss))
                lot_size = max(0.01, min(
                    round(risk_params.lot_size(sl_pips, tick_value, pip_calc), 2), 100.0
                ))
                risk_amt = equity * (risk_pct / 100)

                entry     = signal.entry_price
                future_m5 = [c for c in m5 if c.time >= signal.created_at][:config.MAX_FORWARD_M1_BARS]
                outcome, pnl = self._simulate_outcome(signal, entry, future_m5, risk_amt)

                if diag:
                    diag.record_outcome(symbol, outcome)

                logger.info(
                    "BACKTEST TRADE | %s %s | grade=%s | score=%d | risk=%.2f%% | "
                    "RR=%.2f | entry=%.5f | %s",
                    symbol, signal.direction.value, grade, setup_score,
                    risk_pct, signal.risk_reward, entry, outcome,
                )

                trade = Trade(
                    id=f"bt_{h1_idx}_{zone.id[:8]}", symbol=signal.symbol,
                    direction=signal.direction, entry_price=entry,
                    stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                    lot_size=lot_size, status=TradeStatus.CLOSED,
                    created_at=signal.created_at, pnl=pnl,
                    commission=self._commission * lot_size,
                    news_affected=False, slippage_pips=0.0,
                )
                equity += pnl
                results.append(BacktestResult(
                    trade=trade, outcome=outcome, pnl_usd=pnl,
                    pips_gain=pip_calc.price_to_pips(abs(
                        signal.take_profit - entry if outcome == "WIN" else signal.stop_loss - entry
                    )),
                    news_affected=False,
                ))
                break  # one trade per H1 bar per symbol

        return results

    def _simulate_outcome(self, signal, entry, future_bars, risk_amt) -> Tuple[str, float]:
        for c in future_bars:
            if signal.direction == Direction.BULLISH:
                if c.low  <= signal.stop_loss:
                    return "LOSS", -(risk_amt + self._commission)
                if c.high >= signal.take_profit:
                    return "WIN",  risk_amt * signal.risk_reward - self._commission
            else:
                if c.high >= signal.stop_loss:
                    return "LOSS", -(risk_amt + self._commission)
                if c.low  <= signal.take_profit:
                    return "WIN",  risk_amt * signal.risk_reward - self._commission
        return "OPEN", 0.0

    def _build_windows(self, events: List[NewsEvent]) -> List[NoTradeWindow]:
        windows = []
        for e in events:
            impact = e.impact.lower() if hasattr(e, "impact") else "low"
            if impact == "high":
                buf = timedelta(minutes=config.HIGH_IMPACT_BUFFER_MINS)
            elif impact == "medium":
                buf = timedelta(minutes=config.MEDIUM_IMPACT_BUFFER_MINS)
            else:
                continue
            windows.append(NoTradeWindow(
                start=      e.event_time - buf,
                end=        e.event_time + buf,
                currency=   e.currency,
                event_name= e.event_name,
            ))
        return windows

    def _in_news_window(self, t: datetime, symbol: str, windows: List[NoTradeWindow]) -> bool:
        currencies = config.SYMBOL_CURRENCIES.get(symbol, [])
        return any(w.currency in currencies and w.is_blocking(t) for w in windows)

    # ── Summary + CSV ─────────────────────────────────────────────

    def print_summary(
        self,
        results:    List[BacktestResult],
        final_eq:   float,
        start_date: str,
        end_date:   str,
    ) -> None:
        wins   = sum(1 for r in results if r.outcome == "WIN")
        losses = sum(1 for r in results if r.outcome == "LOSS")
        opens  = sum(1 for r in results if r.outcome == "OPEN")
        total  = len(results)
        wr     = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        logger.info("═" * 50)
        logger.info("  BACKTEST SUMMARY  %s → %s", start_date, end_date)
        logger.info("  Trades: %d  |  W: %d  L: %d  O: %d  |  WR: %.1f%%",
                    total, wins, losses, opens, wr)
        logger.info("  Final equity: %.0f %s  (started %.0f)",
                    final_eq, self._currency, self._account)
        logger.info("  P&L: %.0f %s", final_eq - self._account, self._currency)
        logger.info("═" * 50)

    def export_csv(self, results: List[BacktestResult], filepath: str) -> None:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "symbol", "direction",
                "entry", "sl", "tp", "lots",
                "outcome", "pnl", "pips",
                "news_affected", "slippage_pips", "opened_at",
            ])
            for r in results:
                t = r.trade
                writer.writerow([
                    t.id, t.symbol, t.direction.value,
                    f"{t.entry_price:.5f}", f"{t.stop_loss:.5f}", f"{t.take_profit:.5f}",
                    f"{t.lot_size:.2f}",
                    r.outcome, f"{r.pnl_usd:.2f}", f"{r.pips_gain:.1f}",
                    r.news_affected, t.slippage_pips,
                    t.created_at.isoformat(),
                ])
        logger.info("CSV exported → %s  (%d rows)", filepath, len(results))