"""
main.py — Entry Point & Dependency Wiring.

HOW TO RUN:
  Live trading:   python main.py
  Backtesting:    python main.py --backtest

Edit config.py to change any setting.

UPGRADED (v2): Backtest data-fetch now pulls H4 and M5 candles in
addition to H1, so the new H4 bias filter and M5 ChoCh signal can
run during the walk-forward simulation.
"""
from __future__ import annotations
import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import src.config as config

from src.infrastructure.mt5_bridge.mt5_gateway import MT5Gateway
from src.infrastructure.market_data_repo        import MT5MarketDataRepository
from src.infrastructure.trade_repo              import MT5TradeRepository
from src.infrastructure.news_client             import ForexNewsClient
from src.infrastructure.cache_store             import JsonCacheStore
from src.infrastructure.zone_repo               import InMemoryZoneRepository
from src.infrastructure.spread_calculator       import SpreadCalculator
from src.application.news_manager               import NewsManager
from src.application.execution_service          import ExecutionService


# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────

def _setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("trading_bot.log", encoding="utf-8"),
        ],
    )

logger = logging.getLogger("main")


# ─────────────────────────────────────────────
#  MT5 Connection
# ─────────────────────────────────────────────

def connect_mt5() -> MT5Gateway:
    gw = MT5Gateway(
        login=    config.TRADING_ID,
        password= config.MT5_PASSWORD,
        server=   config.MT5_SERVER,
        mt5_path= config.MT5_PATH,
    )
    if not gw.connect(retries=5, delay=6.0):
        logger.critical(
            "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  MT5 connection failed. Checklist:\n"
            "  1. Open MetaTrader 5 — wait for charts to load\n"
            "  2. Tools → Options → Expert Advisors\n"
            "     → Allow Algorithmic Trading  ✓\n"
            "  3. Check TRADING_ID / MT5_PASSWORD / MT5_SERVER in config.py\n"
            "  4. Run as the SAME user/admin level as MT5\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        sys.exit(1)
    return gw


# ─────────────────────────────────────────────
#  Dependency Injection
# ─────────────────────────────────────────────

def build_services(gw: MT5Gateway):
    market_data  = MT5MarketDataRepository(gw)
    trade_repo   = MT5TradeRepository(gw)
    zone_repo    = InMemoryZoneRepository(persist=True)
    cache        = JsonCacheStore()

    news_client  = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)
    news_manager = NewsManager(news_client)

    commission = _commission_in_account_ccy(config.COMMISSION_PER_LOT)

    spread_calc = SpreadCalculator(
        market_data=        market_data,
        commission_per_lot= commission,
    )

    execution = ExecutionService(
        market_data=       market_data,
        trade_repo=        trade_repo,
        zone_repo=         zone_repo,
        news_manager=      news_manager,
        spread_calculator= spread_calc,
        symbols=           config.SYMBOLS,
        risk_pct=          config.RISK_PERCENT,
        commission=        commission,
        min_rr=            config.MIN_RR,
        account_currency=  config.ACCOUNT_CURRENCY,
    )

    return execution, news_manager


def _commission_in_account_ccy(usd_commission: float) -> float:
    FX_RATES = {
        "USD": 1.0,   "EUR": 0.92,  "GBP": 0.79,
        "KES": 130.0, "NGN": 1550.0,"ZAR": 18.5,
        "GHS": 15.5,  "UGX": 3750.0,"TZS": 2550.0,
    }
    rate      = FX_RATES.get(config.ACCOUNT_CURRENCY.upper(), 1.0)
    converted = usd_commission * rate
    if config.ACCOUNT_CURRENCY.upper() != "USD":
        logger.info(
            "Commission: $%.2f USD → %.2f %s",
            usd_commission, converted, config.ACCOUNT_CURRENCY,
        )
    return converted


# ─────────────────────────────────────────────
#  H1 Bar-Close Detector
# ─────────────────────────────────────────────

class H1ClockWatcher:
    def __init__(self) -> None:
        self._last_h1_hour: Optional[int] = None

    def should_remap(self, now: datetime) -> bool:
        hour_int = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
        if self._last_h1_hour != hour_int:
            self._last_h1_hour = hour_int
            return True
        return False


# ─────────────────────────────────────────────
#  Live Mode
# ─────────────────────────────────────────────

def run_live() -> None:
    gw = connect_mt5()

    acc = gw.get_account_info()
    if acc:
        logger.info(
            "MT5 ✓  %s  |  balance=%.2f %s  |  server=%s",
            acc.get("name"), acc.get("balance", 0),
            acc.get("currency", "?"), acc.get("server"),
        )

    execution, news_manager = build_services(gw)
    h1_watcher = H1ClockWatcher()

    running = [True]
    def _shutdown(sig, frame):
        logger.info("Shutdown — stopping.")
        running[0] = False

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (OSError, ValueError):
        pass

    logger.info("Initial zone mapping …")
    for sym in config.SYMBOLS:
        try:
            execution.run_zone_mapping(sym)
        except Exception as exc:
            logger.exception("Zone mapping error [%s]: %s", sym, exc)

    logger.info("Bot live. Watching: %s", " | ".join(config.SYMBOLS))

    while running[0]:
        loop_start = datetime.now(tz=timezone.utc)
        try:
            news_manager.refresh_if_needed(loop_start)

            if h1_watcher.should_remap(loop_start):
                logger.info("H1 close — re-mapping zones.")
                for sym in config.SYMBOLS:
                    execution.run_zone_mapping(sym)

            for sym in config.SYMBOLS:
                trade = execution.run_monitoring_cycle(sym)
                if trade:
                    logger.info(
                        "TRADE ✓ | %s %s | %.2f lots | entry=%.5f SL=%.5f TP=%.5f",
                        trade.symbol, trade.direction.value.upper(),
                        trade.lot_size, trade.entry_price,
                        trade.stop_loss, trade.take_profit,
                    )

        except Exception as exc:
            logger.exception("Loop error: %s", exc)

        elapsed = (datetime.now(tz=timezone.utc) - loop_start).total_seconds()
        time.sleep(max(0.0, config.LOOP_INTERVAL_SECONDS - elapsed))

    gw.disconnect()
    logger.info("Bot stopped.")


# ─────────────────────────────────────────────
#  Backtest Mode
# ─────────────────────────────────────────────

def run_backtest() -> None:
    from src.application.backtester     import Backtester
    from src.application.diagnostics    import DiagnosticsCollector
    from src.domain.entities            import Timeframe
    from datetime                       import timedelta

    # ── Date range ───────────────────────────
    start_date = config.BACKTEST_START_DATE
    end_date   = config.BACKTEST_END_DATE
    if not start_date or not end_date:
        end_dt     = datetime.now(tz=timezone.utc)
        start_dt   = end_dt - timedelta(days=30 * config.BACKTEST_MONTHS_BACK)
        start_date = start_dt.strftime("%Y-%m-%d")
        end_date   = end_dt.strftime("%Y-%m-%d")

    logger.info("Backtest: %s → %s", start_date, end_date)

    # ── Connect MT5 ───────────────────────────
    gw   = connect_mt5()
    md   = MT5MarketDataRepository(gw)
    cache= JsonCacheStore()
    news = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)

    commission = _commission_in_account_ccy(config.COMMISSION_PER_LOT)

    bt = Backtester(
        news_client= news,
        account=     config.ACCOUNT_BALANCE,
        risk_pct=    config.RISK_PERCENT,
        commission=  commission,
        currency=    config.ACCOUNT_CURRENCY,
    )

    # ── Fetch candle data ─────────────────────
    from datetime import timedelta as td
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=30 * config.BACKTEST_MONTHS_BACK)

    # Add buffer before start so zone mapping has history to work with
    h1_buffer   = timedelta(hours=150)
    h4_buffer   = timedelta(hours=config.H4_CANDLE_COUNT * 4 + 40)  # ~H4_WINDOW bars extra
    fetch_start_h1 = start_dt - h1_buffer
    fetch_start_h4 = start_dt - h4_buffer

    logger.info(
        "Fetching candles: H1/M5/M1 from %s, H4 from %s → %s",
        fetch_start_h1.strftime("%Y-%m-%d"),
        fetch_start_h4.strftime("%Y-%m-%d"),
        end_dt.strftime("%Y-%m-%d"),
    )

    candle_data = {}
    for sym in config.SYMBOLS:
        h1 = md.get_candles_range(sym, Timeframe.H1, fetch_start_h1, end_dt)
        m5 = md.get_candles_range(sym, Timeframe.M5, start_dt,       end_dt)
        m1 = md.get_candles_range(sym, Timeframe.M1, start_dt,       end_dt)
        h4 = md.get_candles_range(sym, Timeframe.H4, fetch_start_h4, end_dt)

        if h1 and m5:
            candle_data[sym] = {
                Timeframe.H1: h1,
                Timeframe.M5: m5,
                Timeframe.M1: m1,   # kept for diagnostics coverage check
                Timeframe.H4: h4,
            }
            logger.info(
                "  %s: %d H4  |  %d H1  |  %d M5  |  %d M1  (%.1f MB est.)",
                sym, len(h4), len(h1), len(m5), len(m1),
                (len(h4) + len(h1) + len(m5) + len(m1)) * 6 * 8 / 1_000_000,
            )
        else:
            logger.warning(
                "  %s: insufficient data — check Market Watch and MT5 history.", sym
            )

    if not candle_data:
        logger.critical("No candle data — check MT5 is open and symbols are in Market Watch.")
        gw.disconnect()
        sys.exit(1)

    # ── Run backtest with diagnostics ─────────
    diag    = DiagnosticsCollector()
    results = bt.run(
        candle_data=candle_data,
        start_date= start_date,
        end_date=   end_date,
        diag=       diag,
    )

    # ── Export results ────────────────────────
    csv_path    = f"src/backtest_results/backtest_{start_date}_{end_date}.csv"
    report_path = f"src/backtest_results/backtest_{start_date}_{end_date}_report.txt"

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)

    bt.export_csv(results, csv_path)
    diag.write_report(report_path)

    logger.info("Files written:")
    logger.info("  Trades  → %s", csv_path)
    logger.info("  Report  → %s", report_path)

    gw.disconnect()


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    _setup_logging()

    logger.info("━" * 50)
    logger.info("  Zone-Reactive Bot  |  account=%s  |  server=%s",
                config.TRADING_ID, config.MT5_SERVER)
    logger.info("  Symbols : %s", ", ".join(config.SYMBOLS))
    logger.info("  Risk    : %.1f%%  |  MinRR: %.1f  |  Broker: HFM",
                config.RISK_PERCENT, config.MIN_RR)
    logger.info("━" * 50)

    parser = argparse.ArgumentParser(description="Zone-Reactive Trading Bot")
    parser.add_argument("--backtest", action="store_true",
                        help="python main.py           → live\n"
                             "python main.py --backtest → backtest")
    args = parser.parse_args()

    if args.backtest:
        logger.info("Mode: BACKTEST")
        run_backtest()
    else:
        logger.info("Mode: LIVE")
        run_live()