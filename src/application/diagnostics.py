"""
diagnostics.py — Backtest Diagnostic Collector.
Tracks everything that happens during a backtest run and writes
a full human-readable report so you know exactly what the bot saw.
"""
from __future__ import annotations
import src.config as config
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


class DiagnosticsCollector:
    """
    Pass this object into Backtester.run().
    Every stage of the backtest calls methods on it to record what happened.
    At the end call write_report(filepath) to dump a full .txt log.
    """

    def __init__(self) -> None:
        self.started_at = datetime.now(tz=timezone.utc)

        # ── Data fetched from MT5 ──────────────────────────────────
        # symbol → dict with candle counts and date ranges
        self.mt5_data: Dict[str, dict] = {}

        # ── News API ───────────────────────────────────────────────
        self.news_fetches: List[dict] = []          # one entry per API call
        self.no_trade_windows: List[dict] = []      # each window that was built

        # ── Per H1 bar stats ───────────────────────────────────────
        # symbol → counts
        self.h1_bars_total       = defaultdict(int)
        self.h1_bars_no_m1       = defaultdict(int)   # skipped: m1_window < 15
        self.h1_bars_no_zones    = defaultdict(int)   # zones/pool came back empty
        self.h1_bars_processed   = defaultdict(int)   # actually checked

        # ── Zone detection ─────────────────────────────────────────
        self.obs_found           = defaultdict(int)
        self.fvgs_found          = defaultdict(int)

        # ── Signal funnel — why signals were rejected ──────────────
        # symbol → reason → count
        self.rejections: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.signals_found       = defaultdict(int)

        # ── Trade outcomes ─────────────────────────────────────────
        # symbol → outcome → count
        self.outcomes: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # ── MT5 data recording ────────────────────────────────────────

    def record_mt5_data(
        self,
        symbol:    str,
        h1_bars:   list,
        m1_bars:   list,
    ) -> None:
        h1_start = h1_bars[0].time.strftime("%Y-%m-%d")  if h1_bars else "—"
        h1_end   = h1_bars[-1].time.strftime("%Y-%m-%d") if h1_bars else "—"
        m1_start = m1_bars[0].time.strftime("%Y-%m-%d %H:%M")  if m1_bars else "—"
        m1_end   = m1_bars[-1].time.strftime("%Y-%m-%d %H:%M") if m1_bars else "—"

        # Calculate M1 coverage as a % of H1 period
        if h1_bars and m1_bars:
            h1_span = (h1_bars[-1].time - h1_bars[0].time).days or 1
            m1_span = (m1_bars[-1].time - m1_bars[0].time).days or 1
            coverage_pct = round(min(m1_span / h1_span * 100, 100), 1)
        else:
            coverage_pct = 0.0

        self.mt5_data[symbol] = {
            "h1_count":    len(h1_bars),
            "h1_start":    h1_start,
            "h1_end":      h1_end,
            "m1_count":    len(m1_bars),
            "m1_start":    m1_start,
            "m1_end":      m1_end,
            "m1_coverage": coverage_pct,
        }

    # ── News recording ────────────────────────────────────────────

    def record_news_fetch(
        self,
        api_name:    str,
        start_date:  str,
        end_date:    str,
        currencies:  list,
        total_events: int,
        high_impact:  int,
    ) -> None:
        self.news_fetches.append({
            "api":          api_name,
            "start":        start_date,
            "end":          end_date,
            "currencies":   ", ".join(sorted(currencies)),
            "total_events": total_events,
            "high_impact":  high_impact,
        })

    def record_no_trade_window(self, currency: str, event_name: str, start: str, end: str) -> None:
        self.no_trade_windows.append({
            "currency":   currency,
            "event":      event_name,
            "start":      start,
            "end":        end,
        })

    # ── Per-bar recording ─────────────────────────────────────────

    def record_h1_bar(
        self,
        symbol:     str,
        m1_count:   int,
        ob_count:   int,
        fvg_count:  int,
        has_pool:   bool,
    ) -> None:
        self.h1_bars_total[symbol] += 1
        if m1_count < 15:
            self.h1_bars_no_m1[symbol] += 1
            return
        if ob_count + fvg_count == 0 or not has_pool:
            self.h1_bars_no_zones[symbol] += 1
            return
        self.h1_bars_processed[symbol] += 1
        self.obs_found[symbol]  += ob_count
        self.fvgs_found[symbol] += fvg_count

    # ── Signal funnel ─────────────────────────────────────────────

    def record_rejection(self, symbol: str, reason: str) -> None:
        """
        reason must be one of:
          'no_zone_tap'  — price never entered the zone
          'wick'         — rejection wick too small
          'volume'       — volume not elevated enough
          'engulfing'    — next candle didn't engulf
          'rr'           — risk:reward below minimum
        """
        self.rejections[symbol][reason] += 1

    def record_signal(self, symbol: str) -> None:
        self.signals_found[symbol] += 1

    def record_outcome(self, symbol: str, outcome: str) -> None:
        self.outcomes[symbol][outcome] += 1

    # ── Report writer ─────────────────────────────────────────────

    def write_report(self, filepath: str) -> None:
        finished_at = datetime.now(tz=timezone.utc)
        duration    = finished_at - self.started_at
        lines       = []

        def hr(char="─", width=70):
            lines.append(char * width)

        def section(title: str) -> None:
            lines.append("")
            hr("═")
            lines.append(f"  {title.upper()}")
            hr("═")

        def sub(title: str) -> None:
            lines.append("")
            lines.append(f"  ── {title}")
            hr()

        # ── Header ────────────────────────────────────────────────
        lines.append("═" * 70)
        lines.append("  ZONE-REACTIVE BOT — BACKTEST DIAGNOSTIC REPORT")
        lines.append("═" * 70)
        lines.append(f"  Generated : {finished_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"  Duration  : {str(duration).split('.')[0]}")

        # ── Config snapshot ───────────────────────────────────────
        section("Config snapshot (config.py values used)")
        lines.append(f"  Account        : {config.ACCOUNT_NICKNAME}  ({config.TRADING_ID})")
        lines.append(f"  Server         : {config.MT5_SERVER}")
        lines.append(f"  Currency       : {config.ACCOUNT_CURRENCY}")
        lines.append(f"  Balance        : {config.ACCOUNT_BALANCE:,.0f} {config.ACCOUNT_CURRENCY}")
        lines.append(f"  Symbols        : {', '.join(config.SYMBOLS)}")
        lines.append("")
        lines.append(f"  Risk/trade     : {config.RISK_PERCENT}%")
        lines.append(f"  Commission/lot : {config.COMMISSION_PER_LOT} USD")
        lines.append(f"  Min RR         : {config.MIN_RR}")
        lines.append(f"  SL buffer      : {config.EXTRA_PIPS_SL} pips")
        lines.append(f"  Max open/sym   : {config.MAX_OPEN_TRADES}")
        lines.append("")
        lines.append(f"  OB impulse candles    : {config.OB_IMPULSE_CANDLES}")
        lines.append(f"  OB impulse multiplier : {config.OB_IMPULSE_MULTIPLIER}")
        lines.append(f"  Liquidity lookback    : {config.LIQUIDITY_LOOKBACK} H1 bars")
        lines.append(f"  Rejection wick ratio  : {config.REJECTION_WICK_RATIO}")
        lines.append(f"  Volume ratio min      : {config.VOLUME_RATIO_MIN}")
        lines.append("")
        lines.append(f"  News buffer    : ±{config.NEWS_BUFFER_MINUTES} min")
        lines.append(f"  ATR cost limit : {config.ATR_COST_THRESHOLD * 100:.0f}% of ATR")
        lines.append(f"  Backtest period: {config.BACKTEST_MONTHS_BACK} months back")

        # ── MT5 data fetched ──────────────────────────────────────
        section("MT5 data fetched per symbol")
        lines.append(f"  {'Symbol':<10} {'H1 bars':>8} {'H1 from':>12} {'H1 to':>12}  {'M1 bars':>8} {'M1 from':>18} {'M1 to':>18}  {'M1 covers':>10}")
        hr()
        for sym, d in self.mt5_data.items():
            cov = f"{d['m1_coverage']}%"
            warn = "  ← PARTIAL" if d['m1_coverage'] < 80 else ""
            lines.append(
                f"  {sym:<10} {d['h1_count']:>8,} {d['h1_start']:>12} {d['h1_end']:>12}  "
                f"{d['m1_count']:>8,} {d['m1_start']:>18} {d['m1_end']:>18}  {cov:>10}{warn}"
            )
        lines.append("")
        lines.append("  NOTE: M1 coverage < 80% means most H1 bars had no M1 data.")
        lines.append("        Reduce BACKTEST_MONTHS_BACK in config.py to fix this.")

        # ── News API ──────────────────────────────────────────────
        section("News API calls")
        if not self.news_fetches:
            lines.append("  No news fetches recorded.")
        for f in self.news_fetches:
            lines.append(f"  API          : {f['api']}")
            lines.append(f"  Date range   : {f['start']} → {f['end']}")
            lines.append(f"  Currencies   : {f['currencies']}")
            lines.append(f"  Total events : {f['total_events']}")
            lines.append(f"  High impact  : {f['high_impact']}")
            lines.append("")

        sub(f"No-trade windows built: {len(self.no_trade_windows)}")
        if self.no_trade_windows:
            # Group by currency
            by_ccy: dict = defaultdict(list)
            for w in self.no_trade_windows:
                by_ccy[w['currency']].append(w)
            for ccy, windows in sorted(by_ccy.items()):
                lines.append(f"  {ccy} ({len(windows)} events):")
                for w in windows[:5]:  # show first 5 per currency
                    lines.append(f"    {w['start']} → {w['end']}  [{w['event']}]")
                if len(windows) > 5:
                    lines.append(f"    … and {len(windows) - 5} more")

        # ── Per-symbol processing stats ───────────────────────────
        section("Per-symbol processing breakdown")
        all_symbols = sorted(set(
            list(self.mt5_data.keys()) +
            list(self.h1_bars_total.keys())
        ))
        for sym in all_symbols:
            total     = self.h1_bars_total[sym]
            no_m1     = self.h1_bars_no_m1[sym]
            no_zones  = self.h1_bars_no_zones[sym]
            processed = self.h1_bars_processed[sym]
            obs       = self.obs_found[sym]
            fvgs      = self.fvgs_found[sym]
            signals   = self.signals_found[sym]
            rej       = self.rejections[sym]

            lines.append(f"  {sym}")
            lines.append(f"    H1 bars total          : {total:,}")
            lines.append(f"    Skipped (no M1 data)   : {no_m1:,}  ({_pct(no_m1, total)})")
            lines.append(f"    Skipped (no zones)     : {no_zones:,}  ({_pct(no_zones, total)})")
            lines.append(f"    Bars actually checked  : {processed:,}  ({_pct(processed, total)})")
            lines.append(f"    OBs found (total)      : {obs:,}")
            lines.append(f"    FVGs found (total)     : {fvgs:,}")
            lines.append(f"    Zones total            : {obs + fvgs:,}")
            lines.append("")
            lines.append(f"    Signal funnel:")
            lines.append(f"      Zones checked        : {sum(rej.values()) + signals:,}")
            lines.append(f"      Rejected — no tap    : {rej.get('no_zone_tap', 0):,}")
            lines.append(f"      Rejected — wick      : {rej.get('wick', 0):,}")
            lines.append(f"      Rejected — volume    : {rej.get('volume', 0):,}")
            lines.append(f"      Rejected — engulfing : {rej.get('engulfing', 0):,}")
            lines.append(f"      Rejected — RR < {config.MIN_RR}  : {rej.get('rr', 0):,}")
            lines.append(f"      Signals generated    : {signals:,}")
            lines.append("")

        # ── Trade outcomes ────────────────────────────────────────
        section("Trade outcomes per symbol")
        lines.append(f"  {'Symbol':<10} {'WIN':>6} {'LOSS':>6} {'SLIP':>6} {'OPEN':>6} {'TOTAL':>6}")
        hr()
        total_wins = total_losses = total_slips = total_open = total_trades = 0
        for sym in all_symbols:
            out   = self.outcomes[sym]
            wins  = out.get("WIN", 0)
            loss  = out.get("LOSS", 0)
            slip  = out.get("NEWS_SLIP", 0)
            open_ = out.get("OPEN", 0)
            tot   = wins + loss + slip + open_
            total_wins    += wins
            total_losses  += loss
            total_slips   += slip
            total_open    += open_
            total_trades  += tot
            lines.append(f"  {sym:<10} {wins:>6} {loss:>6} {slip:>6} {open_:>6} {tot:>6}")
        hr()
        lines.append(f"  {'TOTAL':<10} {total_wins:>6} {total_losses:>6} {total_slips:>6} {total_open:>6} {total_trades:>6}")
        win_rate = total_wins / (total_wins + total_losses + total_slips) * 100 if (total_wins + total_losses + total_slips) > 0 else 0
        lines.append(f"\n  Win rate (resolved only): {win_rate:.1f}%")

        # ── Warnings / flags ──────────────────────────────────────
        section("Warnings & flags")
        warnings_found = False
        for sym, d in self.mt5_data.items():
            if d["m1_coverage"] < 50:
                lines.append(f"  ⚠  {sym}: M1 coverage only {d['m1_coverage']}% — most bars had no M1 data.")
                lines.append(f"     Fix: reduce BACKTEST_MONTHS_BACK in config.py")
                warnings_found = True
            if d["h1_count"] < 200:
                lines.append(f"  ⚠  {sym}: Only {d['h1_count']} H1 bars — very short history.")
                warnings_found = True
        for sym in all_symbols:
            processed = self.h1_bars_processed[sym]
            signals   = self.signals_found[sym]
            if processed > 0 and signals == 0:
                rej = self.rejections[sym]
                top_reason = max(rej, key=rej.get) if rej else "unknown"
                lines.append(f"  ⚠  {sym}: {processed} bars checked but 0 signals. "
                              f"Top rejection: '{top_reason}' ({rej.get(top_reason, 0)} times).")
                warnings_found = True
        if not warnings_found:
            lines.append("  No warnings.")

        # ── Footer ────────────────────────────────────────────────
        lines.append("")
        hr("═")
        lines.append("  END OF REPORT")
        hr("═")
        lines.append("")

        # Write file
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        print(f"\nDiagnostic report written → {path}")


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{part / total * 100:.1f}%"