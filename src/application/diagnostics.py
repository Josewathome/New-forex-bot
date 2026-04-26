"""
diagnostics.py — Backtest Diagnostic Collector  (v6 — win-counter fix)

CHANGES vs v5:
  • write_report(): trade outcomes table now correctly counts WIN_FULL and
    WIN_PARTIAL instead of looking for a "WIN" key that never exists.
    Previously the reported win rate was always 0.0% even when the equity
    curve showed profitable trades.
  • Loss analysis winners list also fixed to use WIN_FULL / WIN_PARTIAL.
"""
from __future__ import annotations
import calendar
import src.config as config
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.application.trade_monitor import MonitorEvent


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
        lines.append("  ZONE-REACTIVE BOT — BACKTEST DIAGNOSTIC REPORT  (v6)")
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
            f"conviction={getattr(config,'MONITOR_CLOSE_CONVICTION',3)}  "
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
        # FIX: the backtester records outcomes as "WIN_FULL" and "WIN_PARTIAL".
        # The old code looked for "WIN" which never exists → always showed 0.
        section("Trade outcomes per symbol")
        lines.append(
            f"  {'Symbol':<10} {'WIN_F':>6} {'WIN_P':>6} {'LOSS':>5} "
            f"{'BE':>5} {'MC':>5} {'OPEN':>5} {'TOTAL':>6}")
        hr()
        twf = twp = tl = tb = tm = to = tt = 0
        for sym in all_symbols:
            out  = self.outcomes[sym]
            # BUG FIX: use WIN_FULL and WIN_PARTIAL, not the nonexistent "WIN"
            wf   = out.get("WIN_FULL", 0)
            wp   = out.get("WIN_PARTIAL", 0)
            l, b, m, o = (out.get(k, 0) for k in ("LOSS", "BREAKEVEN", "MONITOR_CLOSE", "OPEN"))
            tot  = wf + wp + l + b + m + o
            twf += wf; twp += wp; tl += l; tb += b; tm += m; to += o; tt += tot
            lines.append(
                f"  {sym:<10} {wf:>6} {wp:>6} {l:>5} {b:>5} {m:>5} {o:>5} {tot:>6}")
        hr()
        lines.append(
            f"  {'TOTAL':<10} {twf:>6} {twp:>6} {tl:>5} {tb:>5} {tm:>5} {to:>5} {tt:>6}")
        tw = twf + twp
        wr = tw / (tw + tl + tb + tm) * 100 if (tw + tl + tb + tm) else 0
        lines.append(f"\n  Win rate (resolved): {wr:.1f}%")
        lines.append(f"  WIN_FULL            : {twf}   (both TP1 and TP2 hit + runner SL)")
        lines.append(f"  WIN_PARTIAL         : {twp}   (TP1 hit, runner closed by SL/monitor)")
        if tb or tm:
            lines.append(
                f"  Breakeven saves   : {tb}  (losses avoided by monitor moving SL to entry)")
            lines.append(
                f"  Monitor closes    : {tm}  (trades closed early by monitor)")

        # ── Symbol Performance Ranking ────────────────────────────
        section("Symbol Performance Ranking  (best → worst)")
        lines.append(
            f"  {'#':<3}  {'Symbol':<7} {'Trades':>7} {'Wins':>5} "
            f"{'Loss':>5} {'MC':>5} {'Win%':>6}  "
            f"{'Net P&L':>14}  {'Avg Win':>10}  {'Avg Loss':>10}  "
            f"{'% of Gain':>10}  Verdict")
        hr()

        _sym_pnl: dict = defaultdict(list)
        _sym_out: dict = defaultdict(list)
        for _r in self.trade_records:
            _p = _r["balance_after"] - _r["balance_before"]
            _sym_pnl[_r["symbol"]].append(_p)
            _sym_out[_r["symbol"]].append(_r["outcome"])

        total_gain = sum(
            _r["balance_after"] - _r["balance_before"] for _r in self.trade_records
        )

        ranked_syms = sorted(
            _sym_pnl.keys(),
            key=lambda s: sum(_sym_pnl[s]),
            reverse=True,
        )

        for _rank, _sym in enumerate(ranked_syms, 1):
            _pnls  = _sym_pnl[_sym]
            _outs  = _sym_out[_sym]
            _wins  = [p for p, o in zip(_pnls, _outs) if o in ("WIN_FULL", "WIN_PARTIAL")]
            _lossp = [p for p, o in zip(_pnls, _outs) if o == "LOSS"]
            _mcs   = [p for p, o in zip(_pnls, _outs) if o == "MONITOR_CLOSE"]
            _res   = len(_wins) + len(_lossp) + len(_mcs)
            _wr    = len(_wins) / _res * 100 if _res else 0
            _tot   = sum(_pnls)
            _aw    = sum(_wins)  / len(_wins)  if _wins  else 0
            _al    = sum(_lossp) / len(_lossp) if _lossp else 0
            _share = _tot / total_gain * 100 if total_gain else 0
            if _tot > 0 and _wr >= 10:
                _v = "✓ PROFITABLE"
            elif _tot > 0:
                _v = "~ MC-DRIVEN"
            elif len(_lossp) > len(_wins) * 3:
                _v = "✗ HEAVY LOSS"
            else:
                _v = "✗ LOSING"
            lines.append(
                f"  {_rank:<3}  {_sym:<7} {len(_pnls):>7} {len(_wins):>5} "
                f"{len(_lossp):>5} {len(_mcs):>5} {_wr:>5.1f}%  "
                f"{_tot:>+14,.0f}  {_aw:>+10,.0f}  {_al:>+10,.0f}  "
                f"{_share:>+9.1f}%  {_v}")
        hr()
        lines.append(
            f"  {'':52}  Total gain : {total_gain:>+14,.0f}")

        # ── Per-symbol deep dive ──────────────────────────────────
        section("Per-Symbol Deep Dive")
        lines.append(
            "  Scan this section to identify which symbols to keep, tune, or cut.\n")

        for _sym in ranked_syms:
            _pnls  = _sym_pnl[_sym]
            _outs  = _sym_out[_sym]
            _wins  = [p for p, o in zip(_pnls, _outs) if o in ("WIN_FULL", "WIN_PARTIAL")]
            _lossp = [p for p, o in zip(_pnls, _outs) if o == "LOSS"]
            _mcs   = [p for p, o in zip(_pnls, _outs) if o == "MONITOR_CLOSE"]
            _res   = len(_wins) + len(_lossp) + len(_mcs)
            _wr    = len(_wins) / _res * 100 if _res else 0
            _tot   = sum(_pnls)
            _share = _tot / total_gain * 100 if total_gain else 0
            _aw    = sum(_wins)  / len(_wins)  if _wins  else 0
            _al    = sum(_lossp) / len(_lossp) if _lossp else 0
            _amc   = sum(_mcs)   / len(_mcs)   if _mcs   else 0
            _best  = max(_pnls) if _pnls else 0
            _worst = min(_pnls) if _pnls else 0

            _max_streak = _cur = 0
            for _o in _outs:
                if _o == "LOSS":
                    _cur += 1
                    _max_streak = max(_max_streak, _cur)
                else:
                    _cur = 0

            if _tot > 0 and _wr >= 15:
                _verd = "✓ PROFITABLE — solid win rate; strategy working well on this pair"
            elif _tot > 0 and _wr >= 5:
                _verd = "~ MC-DRIVEN PROFIT — wins rare but monitor exits carry returns; fragile"
            elif _tot > 0:
                _verd = "~ MARGINAL — positive but very low win rate; review entry criteria"
            elif _tot < 0 and _max_streak >= 5:
                _verd = "✗ NOT BREAKING EVEN — long losing streaks; consider disabling this pair"
            elif _tot < 0 and len(_lossp) > len(_wins) * 3:
                _verd = "✗ NOT BREAKING EVEN — losses far outweigh wins; needs entry filter work"
            elif _tot < 0:
                _verd = "✗ LOSING — net drag on performance; review setup scoring thresholds"
            else:
                _verd = "~ FLAT — negligible contribution either way"

            sub(_sym)
            lines += [
                f"  Trades        : {len(_pnls)}  "
                f"(WIN_FULL+PARTIAL: {len(_wins)}  |  LOSS: {len(_lossp)}  |  MC: {len(_mcs)})",
                f"  Win rate      : {_wr:.1f}%  (resolved trades only, excl. OPEN)",
                f"  Net P&L       : {_tot:>+,.0f}    ({_share:+.1f}% of total backtest gain)",
                f"  Avg win       : {_aw:>+,.0f}    |  Avg loss : {_al:>+,.0f}    |  Avg MC : {_amc:>+,.0f}",
                f"  Best trade    : {_best:>+,.0f}    |  Worst    : {_worst:>+,.0f}",
                f"  Max consec. losses : {_max_streak}",
                f"",
                f"  Verdict → {_verd}",
            ]

        # ── Loss analysis (v4) ────────────────────────────────────
        section("Loss analysis — MFE & reversal breakdown")
        losers  = [r for r in self.trade_records if r["outcome"] == "LOSS"]
        # FIX: include both WIN_FULL and WIN_PARTIAL in the winners list
        winners = [r for r in self.trade_records if r["outcome"] in ("WIN_FULL", "WIN_PARTIAL")]
        if not losers:
            lines.append("  No losing trades.")
        else:
            am  = sum(r["mfe_pips"] for r in losers) / len(losers)
            al  = sum(r["pips_to_tp_at_mfe"] for r in losers) / len(losers)
            adl = sum(r["duration_minutes"] for r in losers) / len(losers)
            adw = sum(r["duration_minutes"] for r in winners) / len(winners) if winners else 0
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
                pct = cnt / len(losers) * 100
                lines.append(f"    {zt:<25} {cnt:>5}  ({pct:.1f}%)")

        # ── Monitor performance (v5) ──────────────────────────────
        section("Monitor performance — signal accuracy  (v6)")

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

            sig_types = ["breakeven_trigger", "opposing_zone", "reverse_choch", "mfe_stall"]

            lines.append(
                f"  {'Signal type':<24} {'Fired':>6} {'TP':>6} {'FP':>6} "
                f"{'TP%':>6} {'AvgMFE':>8} {'Actions':>10}")
            hr()

            for st in sig_types:
                evs = [e for e in self.monitor_events if e["signal_type"] == st]
                if not evs:
                    lines.append(f"  {st:<24} {'0':>6}")
                    continue
                tp     = sum(1 for e in evs if e["was_correct"])
                fp     = len(evs) - tp
                tp_pct = tp / len(evs) * 100
                avg_m  = sum(e["mfe_pips"] for e in evs) / len(evs)
                acts   = sum(1 for e in evs if e["action_taken"] != "none")
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

                lines.append("  Sample events (first 5):")
                for e in evs[:5]:
                    lines.append(
                        f"    [{e['signal_time'][:16]}] {e['symbol']:<7}"
                        f" MFE={e['mfe_pips']:.1f}p left={e['pips_to_tp']:.1f}p"
                        f" → {e['trade_outcome']}  ({'✓' if e['was_correct'] else '✗'})")
                lines.append("")

            # ── Breakeven saves summary ───────────────────────────
            be_evs = [e for e in self.monitor_events
                      if e["action_taken"] == "moved_to_breakeven"]
            if be_evs:
                sub(f"Breakeven saves — {len(be_evs)} trades")
                saved  = sum(1 for e in be_evs if e["trade_outcome"] in
                             ("BREAKEVEN", "MONITOR_CLOSE"))
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

        # ── Monthly performance ────────────────────────────────────
        # Group trade_records into calendar months using opened_at timestamp
        _monthly: dict = {}
        for _r in self.trade_records:
            try:
                _dt = datetime.fromisoformat(_r["opened_at"]) if _r["opened_at"] else None
            except Exception:
                _dt = None
            _key = (_dt.year, _dt.month) if _dt else (9999, 99)
            if _key not in _monthly:
                _monthly[_key] = []
            _monthly[_key].append(_r)

        sub("Monthly performance summary")
        lines.append(
            f"  {'Month':<22} {'Trades':>7} {'W':>4} {'L':>5} {'MC':>5}  "
            f"{'Month P&L':>14}  {'Eq Start':>13}  {'Eq End':>13}  {'Chg%':>8}")
        hr()
        _mnum = 0
        for (_yr, _mo), _recs in _monthly.items():
            _mnum += 1
            if _yr == 9999:
                _mlabel = f"Month {_mnum}: Unknown"
            else:
                _mlabel = f"Month {_mnum}: {calendar.month_abbr[_mo]} {_yr}"
            _mw   = sum(1 for _r in _recs if _r["outcome"] in ("WIN_FULL", "WIN_PARTIAL"))
            _ml   = sum(1 for _r in _recs if _r["outcome"] == "LOSS")
            _mmc  = sum(1 for _r in _recs if _r["outcome"] == "MONITOR_CLOSE")
            _mpnl = sum(_r["balance_after"] - _r["balance_before"] for _r in _recs)
            _eqs  = _recs[0]["balance_before"]
            _eqe  = _recs[-1]["balance_after"]
            _cpct = (_eqe - _eqs) / _eqs * 100 if _eqs else 0
            lines.append(
                f"  {_mlabel:<22} {len(_recs):>7} {_mw:>4} {_ml:>5} {_mmc:>5}  "
                f"{_mpnl:>+14,.0f}  {_eqs:>13,.0f}  {_eqe:>13,.0f}  "
                f"{_cpct:>+7.1f}%")
        hr()

        # ── Equity curve (grouped by month) ───────────────────────
        sub("Equity curve (grouped by month)")
        lines.append(
            f"  {'#':>4}  {'Symbol':<10} {'Outcome':<14}  "
            f"{'Before':>10}  {'After':>10}  {'Change':>10}")

        _tnum = 0
        _mnum = 0
        for (_yr, _mo), _recs in _monthly.items():
            _mnum += 1
            if _yr == 9999:
                _mlabel = f"Month {_mnum}: Unknown"
            else:
                _mlabel = f"Month {_mnum}: {calendar.month_abbr[_mo]} {_yr}"
            _mw   = sum(1 for _r in _recs if _r["outcome"] in ("WIN_FULL", "WIN_PARTIAL"))
            _ml   = sum(1 for _r in _recs if _r["outcome"] == "LOSS")
            _mmc  = sum(1 for _r in _recs if _r["outcome"] == "MONITOR_CLOSE")
            _mpnl = sum(_r["balance_after"] - _r["balance_before"] for _r in _recs)
            _eqs  = _recs[0]["balance_before"]
            _eqe  = _recs[-1]["balance_after"]
            _cpct = (_eqe - _eqs) / _eqs * 100 if _eqs else 0
            _msign = "+" if _mpnl >= 0 else ""
            lines.append("")
            lines.append(
                f"  ── {_mlabel}  "
                f"[trades={len(_recs)} | W={_mw} L={_ml} MC={_mmc} | "
                f"P&L={_msign}{_mpnl:,.0f} | "
                f"Equity {_eqs:,.0f} → {_eqe:,.0f} ({_cpct:+.1f}%)]")
            hr()
            for _r in _recs:
                _tnum += 1
                _chg  = _r["balance_after"] - _r["balance_before"]
                _sign = "+" if _chg >= 0 else ""
                lines.append(
                    f"  {_tnum:>4}  {_r['symbol']:<10} {_r['outcome']:<14}  "
                    f"{_r['balance_before']:>10,.0f}  {_r['balance_after']:>10,.0f}  "
                    f"{_sign}{_chg:>10,.0f}")

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

        lines += ["", "═" * 70, "  END OF REPORT", "═" * 70, ""]

        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nReport → {path}")


def _pct(part, total):
    return f"{part/total*100:.1f}%" if total else "0%"