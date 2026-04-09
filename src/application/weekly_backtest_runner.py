"""
weekly_backtest_runner.py — Automated weekend backtest + report.

Runs every Saturday at 06:00 UTC (scheduled by Scheduler).
Saves a .txt report (NO .csv) and emails it.

Report location:
    src/backtest_results/weekly_report_W{week}_YYYY-MM-DD.txt
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

import src.config as config

logger = logging.getLogger(__name__)

REPORTS_DIR = Path("src") / "backtest_results"


class WeeklyBacktestRunner:

    def __init__(self, market_data, email_service) -> None:
        self.md    = market_data
        self.email = email_service

    # ── Entry point (called by Scheduler) ────────────────────────────

    def run(self) -> None:
        logger.info("WeeklyBacktest: starting.")
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        try:
            self._execute()
        except Exception as exc:
            logger.exception("WeeklyBacktest: failed — %s", exc)

    # ── Core ─────────────────────────────────────────────────────────

    def _execute(self) -> None:
        from src.application.backtester     import Backtester
        from src.application.diagnostics    import DiagnosticsCollector
        from src.domain.entities            import Timeframe
        from src.infrastructure.news_client import ForexNewsClient
        from src.infrastructure.cache_store import JsonCacheStore

        end_dt   = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=30 * config.BACKTEST_MONTHS_BACK)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str   = end_dt.strftime("%Y-%m-%d")

        cache = JsonCacheStore()
        news  = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)
        comm  = self._commission_in_account_ccy(config.COMMISSION_PER_LOT)

        bt = Backtester(
            news_client=news,
            account=    config.ACCOUNT_BALANCE,
            risk_pct=   config.RISK_PERCENT,
            commission= comm,
            currency=   config.ACCOUNT_CURRENCY,
        )

        candle_data = self._fetch_candles(start_dt, end_dt)
        if not candle_data:
            logger.error("WeeklyBacktest: no candle data — aborting.")
            return

        diag    = DiagnosticsCollector()
        results = bt.run(
            candle_data=candle_data,
            start_date= start_str,
            end_date=   end_str,
            diag=       diag,
        )
        bt.analyze_trade_features(results)

        week_num    = end_dt.isocalendar()[1]
        report_path = REPORTS_DIR / f"weekly_report_W{week_num:02d}_{end_str}.txt"
        diag_path   = REPORTS_DIR / f"weekly_diagnostics_W{week_num:02d}_{end_str}.txt"

        self._write_report(results, start_str, end_str, str(report_path))
        diag.write_report(str(diag_path))

        logger.info("WeeklyBacktest: reports written → %s", report_path)

        subject = (
            f"Zone Bot Weekly Backtest — W{week_num:02d} "
            f"({start_str} to {end_str})"
        )
        body = (
            f"Weekly backtest complete.\n\n"
            f"Period  : {start_str} to {end_str}\n"
            f"Symbols : {', '.join(config.SYMBOLS)}\n\n"
            f"Report attached."
        )
        self.email.send_report(subject, body, str(report_path))

    # ── Data fetch ───────────────────────────────────────────────────

    def _fetch_candles(self, start_dt, end_dt) -> Dict:
        from src.domain.entities import Timeframe

        h1_buffer = timedelta(hours=150)
        h4_buffer = timedelta(hours=config.H4_CANDLE_COUNT * 4 + 40)
        start_h1  = start_dt - h1_buffer
        start_h4  = start_dt - h4_buffer

        candle_data: Dict = {}
        for sym in config.SYMBOLS:
            try:
                h1 = self.md.get_candles_range(sym, Timeframe.H1, start_h1, end_dt)
                m5 = self.md.get_candles_range(sym, Timeframe.M5, start_dt, end_dt)
                m1 = self.md.get_candles_range(sym, Timeframe.M1, start_dt, end_dt)
                h4 = self.md.get_candles_range(sym, Timeframe.H4, start_h4, end_dt)
                if h1 and m5:
                    candle_data[sym] = {
                        Timeframe.H1: h1,
                        Timeframe.M5: m5,
                        Timeframe.M1: m1,
                        Timeframe.H4: h4,
                    }
                    logger.info(
                        "  %s: H4=%d H1=%d M5=%d M1=%d",
                        sym, len(h4 or []), len(h1), len(m5), len(m1 or []),
                    )
                else:
                    logger.warning("  %s: insufficient data — skipped.", sym)
            except Exception as exc:
                logger.error("  %s: fetch error — %s", sym, exc)

        return candle_data

    # ── Report writer ────────────────────────────────────────────────

    def _write_report(
        self, results: List[Any], start_str: str, end_str: str, path: str,
    ) -> None:
        closed  = [r for r in results if r.outcome != "OPEN"]
        wins    = [r for r in closed if r.outcome in ("WIN_FULL", "WIN_PARTIAL")]
        losses  = [r for r in closed if r.outcome == "LOSS"]
        mc      = [r for r in closed if r.outcome == "MONITOR_CLOSE"]
        total   = len(closed)
        wr      = (len(wins) / total * 100) if total else 0.0
        total_pnl = sum(getattr(r, "pnl", 0) or 0 for r in closed)

        # Max drawdown (equity curve)
        equity  = config.ACCOUNT_BALANCE
        peak    = equity
        max_dd  = 0.0
        for r in closed:
            equity += getattr(r, "pnl", 0) or 0
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        sep = "=" * 64
        lines = [
            sep,
            "  ZONE BOT — WEEKLY BACKTEST REPORT",
            f"  Period    : {start_str}  to  {end_str}",
            f"  Generated : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"  Symbols   : {', '.join(config.SYMBOLS)}",
            sep,
            "",
            "  PERFORMANCE SUMMARY",
            f"  Total Trades   : {total}",
            f"  Wins           : {len(wins)}  (Full={sum(1 for r in wins if r.outcome=='WIN_FULL')}  Partial={sum(1 for r in wins if r.outcome=='WIN_PARTIAL')})",
            f"  Losses         : {len(losses)}",
            f"  Monitor Close  : {len(mc)}",
            f"  Win Rate       : {wr:.1f}%",
            f"  Total PnL      : {total_pnl:+.2f} {config.ACCOUNT_CURRENCY}",
            f"  Max Drawdown   : {max_dd:.1f}%",
            "",
            "  PER-SYMBOL BREAKDOWN",
        ]

        symbols = sorted({getattr(r, "symbol", "?") for r in closed})
        for sym in symbols:
            sym_t   = [r for r in closed if getattr(r, "symbol", "") == sym]
            sym_w   = [r for r in sym_t  if r.outcome in ("WIN_FULL", "WIN_PARTIAL")]
            sym_pnl = sum(getattr(r, "pnl", 0) or 0 for r in sym_t)
            sym_wr  = (len(sym_w) / len(sym_t) * 100) if sym_t else 0
            lines.append(
                f"  {sym:8s}  trades={len(sym_t):3d}  wins={len(sym_w):3d}"
                f"  wr={sym_wr:5.1f}%  pnl={sym_pnl:+.2f}"
            )

        lines += [
            "",
            "  TRADE HISTORY (last 30)",
        ]
        for r in closed[-30:]:
            pnl_str = f"{getattr(r, 'pnl', 0):+.2f}" if getattr(r, "pnl", None) is not None else "?"
            lines.append(
                f"  {getattr(r,'symbol','?'):8s}  {getattr(r,'direction','?').value if hasattr(getattr(r,'direction','?'),'value') else '?':5s}"
                f"  {r.outcome:14s}  pnl={pnl_str}"
            )

        lines += ["", sep]
        Path(path).write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _commission_in_account_ccy(usd_commission: float) -> float:
        FX_RATES = {
            "USD": 1.0, "EUR": 0.92, "GBP": 0.79,
            "KES": 130.0, "NGN": 1550.0,
        }
        rate = FX_RATES.get(config.ACCOUNT_CURRENCY.upper(), 1.0)
        return usd_commission * rate