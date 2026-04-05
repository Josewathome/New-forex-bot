"""
diagnostics.py — Backtest Diagnostic Collector  (v5 — Monitor Metrics)

NEW IN v5:
  • record_monitor_event()  — stores every TradeMonitor signal that fired
  • write_report() adds a MONITOR PERFORMANCE section showing:
      - How many signals of each type fired
      - True positive rate per type (signal fired → trade reversed)
      - False positive rate (signal fired → trade actually won)
      - Average MFE at signal time (were they firing early enough?)
      - Breakeven save count (how many losses were turned into BEs)
"""
from __future__ import annotations
import src.config as config
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.application.trade_monitor import  MonitorEvent
class DiagnosticsCollector:

    def __init__(self) -> None:
        self.started_at = datetime.now(tz=timezone.utc)

        # Data fetched from MT5
        self.mt5_data: Dict[str, dict] = {}

        # News API
        self.news_fetches: List[dict]      = []
        self.no_trade_windows: List[dict]  = []

        # Per H1 bar stats
        self.h1_bars_total     = defaultdict(int)
        self.h1_bars_no_m1     = defaultdict(int)
        self.h1_bars_no_zones  = defaultdict(int)
        self.h1_bars_processed = defaultdict(int)
        self.obs_found         = defaultdict(int)
        self.fvgs_found        = defaultdict(int)

        # Signal funnel
        self.rejections: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.signals_found = defaultdict(int)

        # Trade outcomes
        self.outcomes: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

        # v4: per-trade MFE records
        self.trade_records: List[dict] = []

        # v5: monitor event records
        # Each entry mirrors MonitorEvent fields + trade_outcome + was_correct
        self.monitor_events: List[dict] = []

    # ── MT5 data ─────────────────────────────────────────────────

    def record_mt5_data(self, symbol, h1_bars, m1_bars):
        h1_start = h1_bars[0].time.strftime("%Y-%m-%d")  if h1_bars else "—"
        h1_end   = h1_bars[-1].time.strftime("%Y-%m-%d") if h1_bars else "—"
        m1_start = m1_bars[0].time.strftime("%Y-%m-%d %H:%M")  if m1_bars else "—"
        m1_end   = m1_bars[-1].time.strftime("%Y-%m-%d %H:%M") if m1_bars else "—"
        h1_span  = (h1_bars[-1].time - h1_bars[0].time).days or 1 if h1_bars else 1
        m1_span  = (m1_bars[-1].time - m1_bars[0].time).days or 1 if m1_bars else 0
        coverage = round(min(m1_span / h1_span * 100, 100), 1) if m1_bars else 0.0
        self.mt5_data[symbol] = {
            "h1_count": len(h1_bars), "h1_start": h1_start, "h1_end": h1_end,
            "m1_count": len(m1_bars), "m1_start": m1_start, "m1_end": m1_end,
            "m1_coverage": coverage,
        }

    # ── News ──────────────────────────────────────────────────────

    def record_news_fetch(self, api_name, start_date, end_date,
                          currencies, total_events, high_impact):
        self.news_fetches.append({
            "api": api_name, "start": start_date, "end": end_date,
            "currencies": ", ".join(sorted(currencies)),
            "total_events": total_events, "high_impact": high_impact,
        })

    def record_no_trade_window(self, currency, event_name, start, end):
        self.no_trade_windows.append({
            "currency": currency, "event": event_name,
            "start": start, "end": end,
        })

    # ── Per-bar ───────────────────────────────────────────────────

    def record_h1_bar(self, symbol, m1_count, ob_count, fvg_count, has_pool):
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

    def record_rejection(self, symbol, reason):
        self.rejections[symbol][reason] += 1

    def record_signal(self, symbol):
        self.signals_found[symbol] += 1

    def record_outcome(self, symbol, outcome):
        self.outcomes[symbol][outcome] += 1

    # ── v4: per-trade MFE record ──────────────────────────────────

    def record_trade_result(self, symbol, outcome, mfe_pips,
                            pips_to_tp_at_mfe, duration_minutes,
                            reversal_zone_type, balance_before,
                            balance_after, opened_at=None):
        self.trade_records.append({
            "symbol": symbol, "outcome": outcome,
            "mfe_pips": mfe_pips, "pips_to_tp_at_mfe": pips_to_tp_at_mfe,
            "duration_minutes": duration_minutes,
            "reversal_zone_type": reversal_zone_type or "unknown",
            "balance_before": balance_before, "balance_after": balance_after,
            "opened_at": opened_at or "",
        })

    # ── v5: monitor event record ──────────────────────────────────

    def record_monitor_event(self, event: "MonitorEvent") -> None:
        """
        Called once per MonitorEvent that fires during backtest simulation.
        Stores everything needed to evaluate accuracy in write_report().
        """
        self.monitor_events.append({
            "trade_id":          event.trade_id,
            "symbol":            event.symbol,
            "signal_time":       event.signal_time.isoformat(),
            "signal_type":       event.signal_type,
            "price":             event.price_at_signal,
            "mfe_pips":          event.mfe_pips_at_signal,
            "pips_to_tp":        event.pips_to_tp,
            "detail":            event.detail,
            "action_taken":      event.action_taken,
            "trade_outcome":     event.trade_outcome,
            "was_correct":       event.was_correct,
        })

    # ── Report ────────────────────────────────────────────────────

    def write_report(self, filepath: str) -> None:
        finished_at = datetime.now(tz=timezone.utc)
        duration    = finished_at - self.started_at
        lines       = []

        def hr(char="─", w=70):  lines.append(char * w)
        def section(t):
            lines.append(""); hr("═"); lines.append(f"  {t.upper()}"); hr("═")
        def sub(t):
            lines.append(""); lines.append(f"  ── {t}"); hr()

        # ── Header ───────────────────────────────────────────────
        lines.append("═" * 70)
        lines.append("  ZONE-REACTIVE BOT — BACKTEST DIAGNOSTIC REPORT  (v5)")
        lines.append("═" * 70)
        lines.append(f"  Generated : {finished_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"  Duration  : {str(duration).split('.')[0]}")

        # ── Config ───────────────────────────────────────────────
        section("Config snapshot")
        lines += [
            f"  Account  : {config.ACCOUNT_NICKNAME}  ({config.TRADING_ID})",
            f"  Balance  : {config.ACCOUNT_BALANCE:,.0f} {config.ACCOUNT_CURRENCY}",
            f"  Symbols  : {', '.join(config.SYMBOLS)}",
            f"  Risk     : {config.RISK_PERCENT}%  |  MinRR: {config.MIN_RR}",
            f"  Monitor  : enabled={getattr(config,'MONITOR_ENABLED',True)}  "
            f"act={getattr(config,'MONITOR_ACT_ON_SIGNALS',False)}  "
            f"BE_trigger={getattr(config,'BREAKEVEN_RR_TRIGGER',2.0)}×SL  "
            f"stall_bars={getattr(config,'MFE_STALL_BARS',6)}",
        ]

        # ── MT5 data ──────────────────────────────────────────────
        section("MT5 data fetched")
        lines.append(
            f"  {'Symbol':<10} {'H1':>8} {'H1 from':>12} {'H1 to':>12}"
            f"  {'M1':>8} {'M1 coverage':>12}")
        hr()
        for sym, d in self.mt5_data.items():
            warn = "  ← PARTIAL" if d['m1_coverage'] < 80 else ""
            lines.append(
                f"  {sym:<10} {d['h1_count']:>8,} {d['h1_start']:>12} "
                f"{d['h1_end']:>12}  {d['m1_count']:>8,} "
                f"{d['m1_coverage']:>11.1f}%{warn}")

        # ── News ──────────────────────────────────────────────────
        section("News API")
        for f in self.news_fetches:
            lines += [f"  {f['api']}  {f['start']} → {f['end']}",
                      f"  Events: {f['total_events']}  (high: {f['high_impact']})", ""]
        sub(f"No-trade windows: {len(self.no_trade_windows)}")
        by_ccy = defaultdict(list)
        for w in self.no_trade_windows:
            by_ccy[w['currency']].append(w)
        for ccy, ws in sorted(by_ccy.items()):
            lines.append(f"  {ccy} ({len(ws)}):")
            for w in ws[:3]:
                lines.append(f"    {w['start']} → {w['end']}  [{w['event']}]")
            if len(ws) > 3:
                lines.append(f"    … {len(ws)-3} more")

        # ── Per-symbol breakdown ──────────────────────────────────
        section("Per-symbol signal funnel")
        all_symbols = sorted(set(list(self.mt5_data) + list(self.h1_bars_total)))
        for sym in all_symbols:
            tot = self.h1_bars_total[sym]
            pr  = self.h1_bars_processed[sym]
            sig = self.signals_found[sym]
            rej = self.rejections[sym]
            lines += [
                f"  {sym}",
                f"    H1 bars total   : {tot:,}",
                f"    Bars processed  : {pr:,}  ({_pct(pr,tot)})",
                f"    OBs / FVGs      : {self.obs_found[sym]:,} / {self.fvgs_found[sym]:,}",
                f"    Signals         : {sig:,}",
                f"    Rejected (top)  : no_tap={rej.get('no_zone_tap',0):,}"
                f"  rr={rej.get('rr',0):,}"
                f"  score={rej.get('below_score_threshold',0):,}", "",
            ]

        # ── Trade outcomes ────────────────────────────────────────
        section("Trade outcomes per symbol")
        lines.append(
            f"  {'Symbol':<10} {'WIN':>5} {'LOSS':>5} "
            f"{'BE':>5} {'MC':>5} {'OPEN':>5} {'TOTAL':>6}")
        hr()
        tw=tl=tb=tm=to=tt = 0
        for sym in all_symbols:
            out  = self.outcomes[sym]
            w,l,b,m,o = (out.get(k,0) for k in ("WIN","LOSS","BREAKEVEN","MONITOR_CLOSE","OPEN"))
            tot  = w+l+b+m+o
            tw+=w; tl+=l; tb+=b; tm+=m; to+=o; tt+=tot
            lines.append(f"  {sym:<10} {w:>5} {l:>5} {b:>5} {m:>5} {o:>5} {tot:>6}")
        hr()
        lines.append(f"  {'TOTAL':<10} {tw:>5} {tl:>5} {tb:>5} {tm:>5} {to:>5} {tt:>6}")
        wr = tw/(tw+tl+tb+tm)*100 if (tw+tl+tb+tm) else 0
        lines.append(f"\n  Win rate (resolved): {wr:.1f}%")
        if tb or tm:
            lines.append(
                f"  Breakeven saves   : {tb}  (losses avoided by monitor moving SL to entry)")
            lines.append(
                f"  Monitor closes    : {tm}  (trades closed early by monitor)")

        # ── Loss analysis (v4) ────────────────────────────────────
        section("Loss analysis — MFE & reversal breakdown")
        losers  = [r for r in self.trade_records if r["outcome"] == "LOSS"]
        winners = [r for r in self.trade_records if r["outcome"] == "WIN"]
        if not losers:
            lines.append("  No losing trades.")
        else:
            am  = sum(r["mfe_pips"] for r in losers) / len(losers)
            al  = sum(r["pips_to_tp_at_mfe"] for r in losers) / len(losers)
            adl = sum(r["duration_minutes"] for r in losers) / len(losers)
            adw = sum(r["duration_minutes"] for r in winners)/len(winners) if winners else 0
            lines += [
                f"  Losses: {len(losers)}  Winners: {len(winners)}",
                "",
                f"  MFE on losers:",
                f"    Avg  : {am:.1f} pips",
                f"    Max  : {max(r['mfe_pips'] for r in losers):.1f} pips",
                f"    Min  : {min(r['mfe_pips'] for r in losers):.1f} pips",
                "",
                f"  Pips to TP at MFE:",
                f"    Avg  : {al:.1f}  (on avg {al:.1f} pips short at best point)",
                f"    Min  : {min(r['pips_to_tp_at_mfe'] for r in losers):.1f} pips",
                "",
                f"  Duration  — losses avg : {adl:.0f} min  winners avg : {adw:.0f} min",
                "",
                "  Reversal zone at MFE:",
            ]
            rz_counts: dict = defaultdict(int)
            for r in losers:
                rz_counts[r["reversal_zone_type"]] += 1
            for zt, cnt in sorted(rz_counts.items(), key=lambda x: -x[1]):
                pct = cnt/len(losers)*100
                lines.append(f"    {zt:<25} {cnt:>5}  ({pct:.1f}%)")

        # ── Monitor performance (v5 — the new section) ────────────
        section("Monitor performance — signal accuracy  (v5)")

        if not self.monitor_events:
            lines.append("  No monitor events recorded.")
            lines.append("  → Set MONITOR_ENABLED = True in config.py to activate.")
        else:
            lines.append(
                "  How to read this section:\n"
                "    true_positive  = signal fired AND trade went on to LOSS/BE/MC\n"
                "    false_positive = signal fired BUT trade went on to WIN\n"
                "    A good signal type has high TP% and fires with meaningful MFE.\n"
            )

            sig_types = ["breakeven_trigger","opposing_zone","reverse_choch","mfe_stall"]

            lines.append(
                f"  {'Signal type':<24} {'Fired':>6} {'TP':>6} {'FP':>6} "
                f"{'TP%':>6} {'AvgMFE':>8} {'Actions':>10}")
            hr()

            for st in sig_types:
                evs   = [e for e in self.monitor_events if e["signal_type"] == st]
                if not evs:
                    lines.append(f"  {st:<24} {'0':>6}")
                    continue
                tp    = sum(1 for e in evs if e["was_correct"])
                fp    = len(evs) - tp
                tp_pct= tp / len(evs) * 100
                avg_m = sum(e["mfe_pips"] for e in evs) / len(evs)
                acts  = sum(1 for e in evs if e["action_taken"] != "none")
                lines.append(
                    f"  {st:<24} {len(evs):>6} {tp:>6} {fp:>6} "
                    f"{tp_pct:>5.1f}% {avg_m:>7.1f}p {acts:>10}")

            lines.append("")

            # ── Per-signal-type detail ────────────────────────────
            for st in sig_types:
                evs = [e for e in self.monitor_events if e["signal_type"] == st]
                if not evs:
                    continue
                sub(f"{st} — {len(evs)} events")

                tp_evs = [e for e in evs if e["was_correct"]]
                fp_evs = [e for e in evs if not e["was_correct"]]

                lines += [
                    f"  True positives  : {len(tp_evs)}  ({len(tp_evs)/len(evs)*100:.1f}%)",
                    f"  False positives : {len(fp_evs)}  ({len(fp_evs)/len(evs)*100:.1f}%)",
                    f"  Avg MFE at fire : {sum(e['mfe_pips'] for e in evs)/len(evs):.1f} pips",
                    f"  Avg pips to TP  : {sum(e['pips_to_tp'] for e in evs)/len(evs):.1f} pips",
                ]

                actions = defaultdict(int)
                for e in evs:
                    actions[e["action_taken"]] += 1
                lines.append("  Actions taken:")
                for act, cnt in sorted(actions.items(), key=lambda x: -x[1]):
                    lines.append(f"    {act:<28} : {cnt}")

                # Show 5 example events
                lines.append("  Sample events (first 5):")
                for e in evs[:5]:
                    lines.append(
                        f"    [{e['signal_time'][:16]}] {e['symbol']:<7}"
                        f" MFE={e['mfe_pips']:.1f}p left={e['pips_to_tp']:.1f}p"
                        f" → {e['trade_outcome']}  ({'✓' if e['was_correct'] else '✗'})")
                lines.append("")

            # ── Breakeven saves summary ────────────────────────────
            be_evs = [e for e in self.monitor_events
                      if e["action_taken"] == "moved_to_breakeven"]
            if be_evs:
                sub(f"Breakeven saves — {len(be_evs)} trades")
                saved  = sum(1 for e in be_evs if e["trade_outcome"] in
                             ("BREAKEVEN","MONITOR_CLOSE"))
                failed = len(be_evs) - saved
                lines += [
                    f"  SL moved to entry  : {len(be_evs)}",
                    f"  Saved (scratched)  : {saved}  — would have been LOSS",
                    f"  Unnecessary (won)  : {failed}  — trade went on to win anyway",
                    f"  Save rate          : {saved/len(be_evs)*100:.1f}%",
                    "",
                    "  Interpretation:",
                    f"    {saved} trades were rescued from a full loss.",
                    f"    {failed} trades were closed at breakeven that would have hit TP.",
                    "    Consider raising BREAKEVEN_RR_TRIGGER if false-save rate is high.",
                ]

        # ── Equity curve ──────────────────────────────────────────
        sub("Equity curve (all trades in order)")
        lines.append(
            f"  {'#':>4}  {'Symbol':<10} {'Outcome':<14}  "
            f"{'Before':>10}  {'After':>10}  {'Change':>10}")
        hr()
        for i, r in enumerate(self.trade_records, 1):
            chg  = r["balance_after"] - r["balance_before"]
            sign = "+" if chg >= 0 else ""
            lines.append(
                f"  {i:>4}  {r['symbol']:<10} {r['outcome']:<14}  "
                f"{r['balance_before']:>10,.0f}  {r['balance_after']:>10,.0f}  "
                f"{sign}{chg:>10,.0f}")

        # ── Warnings ─────────────────────────────────────────────
        section("Warnings & flags")
        warned = False
        for sym, d in self.mt5_data.items():
            if d["m1_coverage"] < 50:
                lines.append(f"  ⚠  {sym}: M1 coverage {d['m1_coverage']}%")
                warned = True
            if d["h1_count"] < 200:
                lines.append(f"  ⚠  {sym}: only {d['h1_count']} H1 bars")
                warned = True
        for sym in all_symbols:
            if self.h1_bars_processed[sym] > 0 and self.signals_found[sym] == 0:
                rej = self.rejections[sym]
                top = max(rej, key=rej.get) if rej else "unknown"
                lines.append(f"  ⚠  {sym}: 0 signals. Top rejection: {top}")
                warned = True
        if not warned:
            lines.append("  No warnings.")

        lines += ["", "═"*70, "  END OF REPORT", "═"*70, ""]

        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nReport → {path}")


def _pct(part, total):
    return f"{part/total*100:.1f}%" if total else "0%"