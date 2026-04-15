"""
main.py — Entry Point & Dependency Wiring.

HOW TO RUN:
  Live trading:   python -m src.main
  Backtesting:    python -m src.main --backtest

New services wired in:
  - CleanupService    : daily log/cache/zone retention
  - CheckpointService : state save/resume on restart
  - TradeJournal      : persistent trade record for dashboard
  - ZoneFileStore     : per-symbol timestamped zone snapshots
  - DashboardServer   : Flask API at http://localhost:8080
  - Scheduler         : cleanup daily, backtest weekly
  - WeeklyBacktestRunner + EmailService
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

from src.infrastructure.mt5_bridge.mt5_gateway  import MT5Gateway
from src.infrastructure.market_data_repo         import MT5MarketDataRepository
from src.infrastructure.trade_repo               import MT5TradeRepository
from src.infrastructure.news_client              import ForexNewsClient
from src.infrastructure.cache_store              import JsonCacheStore
from src.infrastructure.zone_repo                import InMemoryZoneRepository
from src.infrastructure.spread_calculator        import SpreadCalculator
from src.infrastructure.cleanup_service          import CleanupService
from src.infrastructure.checkpoint_service       import CheckpointService
from src.infrastructure.trade_journal            import TradeJournal
from src.infrastructure.zone_store               import ZoneFileStore
from src.application.news_manager                import NewsManager
from src.application.execution_service           import ExecutionService
from src.application.scheduler                   import Scheduler
from src.application.email_service               import EmailService
from src.application.weekly_backtest_runner      import WeeklyBacktestRunner


def _setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/trading_bot.log", encoding="utf-8"),
            logging.FileHandler("trading_bot.log",      encoding="utf-8"),
        ],
    )

logger = logging.getLogger("main")


def connect_mt5() -> MT5Gateway:
    gw = MT5Gateway(
        login=    config.TRADING_ID,
        password= config.MT5_PASSWORD,
        server=   config.MT5_SERVER,
        mt5_path= config.MT5_PATH,
    )
    if not gw.connect(retries=5, delay=6.0):
        logger.critical(
            "MT5 connection failed.\n"
            "  1. MT5 open and charts loaded\n"
            "  2. Tools > Options > Expert Advisors > Allow Algorithmic Trading\n"
            "  3. Correct TRADING_ID / MT5_PASSWORD / MT5_SERVER"
        )
        sys.exit(1)
    return gw


def build_services(gw: MT5Gateway):
    market_data  = MT5MarketDataRepository(gw)
    trade_repo   = MT5TradeRepository(gw)
    zone_repo    = InMemoryZoneRepository(persist=True)
    cache        = JsonCacheStore()
    news_client  = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)
    news_manager = NewsManager(news_client)
    commission   = _commission_in_account_ccy(config.COMMISSION_PER_LOT)
    spread_calc  = SpreadCalculator(
        market_data=       market_data,
        commission_per_lot=commission,
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
    return execution, news_manager, market_data, zone_repo


def _commission_in_account_ccy(usd: float) -> float:
    rates = {"USD":1.0,"EUR":0.92,"GBP":0.79,"KES":130.0,"NGN":1550.0,"ZAR":18.5}
    return usd * rates.get(config.ACCOUNT_CURRENCY.upper(), 1.0)


class H1ClockWatcher:
    def __init__(self): self._last = None
    def should_remap(self, now: datetime) -> bool:
        h = int(now.replace(minute=0,second=0,microsecond=0).timestamp())
        if self._last != h:
            self._last = h
            return True
        return False


def run_live() -> None:
    gw  = connect_mt5()
    acc = gw.get_account_info()
    if acc:
        logger.info("MT5  |  %s  |  balance=%.2f %s  |  server=%s",
                    acc.get("name"), acc.get("balance",0),
                    acc.get("currency","?"), acc.get("server"))

    execution, news_manager, market_data, zone_repo = build_services(gw)

    cleanup    = CleanupService()
    checkpoint = CheckpointService(getattr(config,"CHECKPOINT_DIR",".checkpoints"))
    journal    = TradeJournal()
    zone_store = ZoneFileStore()
    email_svc  = EmailService()
    scheduler  = Scheduler()
    weekly_bt  = WeeklyBacktestRunner(market_data, email_svc)

    cp = checkpoint.load()
    if cp:
        logger.info("Resumed from checkpoint (saved_at=%s).", cp.get("saved_at"))

    # Re-apply any config values changed via the dashboard before last shutdown.
    # Must run AFTER checkpoint load and BEFORE building services that read config.
    try:
        from src.api.server import load_config_overrides
        load_config_overrides()
    except Exception as _e:
        logger.warning("Config override load skipped: %s", _e)

    if getattr(config, "DASHBOARD_ENABLED", True):
        try:
            from src.api.server import DashboardServer
            DashboardServer(journal, checkpoint).start()
        except Exception as e:
            logger.warning("Dashboard failed to start: %s", e)

    scheduler.add_daily("cleanup", cleanup.run_all, hour=2)
    if getattr(config, "WEEKLY_BACKTEST_ENABLED", True):
        scheduler.add_weekly(
            "weekly_backtest", weekly_bt.run,
            weekday=getattr(config,"WEEKLY_BACKTEST_WEEKDAY",5),
            hour=   getattr(config,"WEEKLY_BACKTEST_HOUR_UTC",6),
        )
    scheduler.start()

    running = [True]
    def _shutdown(sig, frame):
        logger.info("Shutdown signal.")
        running[0] = False
    signal.signal(signal.SIGINT, _shutdown)
    try: signal.signal(signal.SIGTERM, _shutdown)
    except (OSError, ValueError): pass

    logger.info("Initial zone mapping...")
    for sym in config.SYMBOLS:
        try:
            execution.run_zone_mapping(sym)
            zone_store.save_snapshot(sym, zone_repo.get_active_zones(sym), zone_repo.get_liquidity_pool(sym))
        except Exception as exc:
            logger.exception("Zone mapping [%s]: %s", sym, exc)

    logger.info("Bot live. Watching: %s", " | ".join(config.SYMBOLS))

    h1_watcher      = H1ClockWatcher()
    last_checkpoint = datetime.now(tz=timezone.utc)
    cp_interval     = getattr(config,"CHECKPOINT_INTERVAL_MIN",15) * 60
    loop_n          = 0

    while running[0]:
        loop_start = datetime.now(tz=timezone.utc)
        try:
            news_manager.refresh_if_needed(loop_start)

            if h1_watcher.should_remap(loop_start):
                logger.info("H1 close - re-mapping zones.")
                for sym in config.SYMBOLS:
                    try:
                        execution.run_zone_mapping(sym)
                        zone_store.save_snapshot(sym, zone_repo.get_active_zones(sym), zone_repo.get_liquidity_pool(sym))
                    except Exception as exc:
                        logger.exception("Zone remap [%s]: %s", sym, exc)

            for sym in config.SYMBOLS:
                try:
                    trade = execution.run_monitoring_cycle(sym)
                    if trade:
                        logger.info("TRADE | %s %s | %.2f lots | entry=%.5f SL=%.5f TP=%.5f",
                                    trade.symbol, trade.direction.value.upper(),
                                    trade.lot_size, trade.entry_price,
                                    trade.stop_loss, trade.take_profit)
                        if getattr(trade, "mt5_ticket", None):
                            journal.record_open(
                                ticket=   trade.mt5_ticket,
                                symbol=   trade.symbol,
                                direction=trade.direction.value,
                                entry=    trade.entry_price,
                                sl=       trade.stop_loss,
                                tp=       trade.take_profit,
                                lot_size= trade.lot_size,
                            )
                except Exception as exc:
                    logger.exception("Monitoring [%s]: %s", sym, exc)

            loop_n += 1
            if (loop_start - last_checkpoint).total_seconds() >= cp_interval:
                checkpoint.save({
                    "loop_n":   loop_n,
                    "symbols":  config.SYMBOLS,
                    "risk_pct": config.RISK_PERCENT,
                    "min_rr":   config.MIN_RR,
                })
                last_checkpoint = loop_start

        except Exception as exc:
            logger.exception("Loop error: %s", exc)

        elapsed = (datetime.now(tz=timezone.utc) - loop_start).total_seconds()
        time.sleep(max(0.0, config.LOOP_INTERVAL_SECONDS - elapsed))

    scheduler.stop()
    checkpoint.save({"symbols": config.SYMBOLS, "shutdown": "clean"})
    gw.disconnect()
    logger.info("Bot stopped cleanly.")


def run_backtest() -> None:
    from src.application.backtester  import Backtester
    from src.application.diagnostics import DiagnosticsCollector
    from src.domain.entities         import Timeframe
    from datetime                    import timedelta

    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=30 * config.BACKTEST_MONTHS_BACK)
    start_str = config.BACKTEST_START_DATE or start_dt.strftime("%Y-%m-%d")
    end_str   = config.BACKTEST_END_DATE   or end_dt.strftime("%Y-%m-%d")

    logger.info("Backtest: %s to %s", start_str, end_str)
    gw = connect_mt5()
    md = MT5MarketDataRepository(gw)

    h1_buf = timedelta(hours=150)
    h4_buf = timedelta(hours=config.H4_CANDLE_COUNT * 4 + 40)

    candle_data = {}
    for sym in config.SYMBOLS:
        h1 = md.get_candles_range(sym, Timeframe.H1, start_dt-h1_buf, end_dt)
        m5 = md.get_candles_range(sym, Timeframe.M5, start_dt, end_dt)
        m1 = md.get_candles_range(sym, Timeframe.M1, start_dt, end_dt)
        h4 = md.get_candles_range(sym, Timeframe.H4, start_dt-h4_buf, end_dt)
        if h1 and m5:
            candle_data[sym] = {Timeframe.H1:h1,Timeframe.M5:m5,Timeframe.M1:m1,Timeframe.H4:h4}
        else:
            logger.warning("  %s: insufficient data.", sym)

    if not candle_data:
        logger.critical("No candle data.")
        gw.disconnect(); sys.exit(1)

    cache = JsonCacheStore()
    news  = ForexNewsClient(api_key=config.NEWS_API_KEY, cache=cache)
    comm  = _commission_in_account_ccy(config.COMMISSION_PER_LOT)
    bt    = Backtester(news_client=news, account=config.ACCOUNT_BALANCE,
                       risk_pct=config.RISK_PERCENT, commission=comm,
                       currency=config.ACCOUNT_CURRENCY)
    diag    = DiagnosticsCollector()
    results = bt.run(candle_data=candle_data, start_date=start_str,
                     end_date=end_str, diag=diag)
    bt.analyze_trade_features(results)

    report_dir = Path("src/backtest_results")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"backtest_{start_str}_{end_str}_report.txt"
    diag.write_report(str(report_path))
    logger.info("Report -> %s", report_path)
    gw.disconnect()


if __name__ == "__main__":
    _setup_logging()
    logger.info("-" * 50)
    logger.info("  Zone-Reactive Bot  |  account=%s  |  server=%s",
                config.TRADING_ID, config.MT5_SERVER)
    logger.info("  Symbols : %s", ", ".join(config.SYMBOLS))
    logger.info("  Risk    : %.1f%%  |  MinRR: %.1f", config.RISK_PERCENT, config.MIN_RR)
    logger.info("-" * 50)

    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", action="store_true")
    args = parser.parse_args()

    if args.backtest:
        logger.info("Mode: BACKTEST")
        run_backtest()
    else:
        logger.info("Mode: LIVE")
        run_live()