# src/smc_3step_live_runner.py
#
# Standalone live/demo runner for the backtested-and-accepted XAUUSD-only
# V1 strategy (src/application/smc_3step.py, the ported SmartMoney_3Step_EA).
#
# DELIBERATELY SEPARATE from src/main_stream.py: that pipeline only streams
# M1/M5/M30/H1/H4 candles via ZMQ (M15 is not tracked at all -- see the
# Explore-agent architecture survey this was built from), while this
# strategy needs M15. Rather than extend the ZMQ/CandleBuilder pipeline
# (a bigger, riskier change touching the existing live bot), this runner
# polls MT5Gateway directly for H1/M15/M5 candles on a simple bar-clock,
# fully isolated from main_stream.py's own symbols/positions. It reuses
# ONLY MT5Gateway (I/O) and config's credentials -- no other execution_
# service.py/analysis.py/trade_monitor.py code paths are touched.
#
# SCOPE: XAUUSD only (the accepted V1 baseline). Market-order entry with
# SL/TP set at open, no dynamic management (breakeven/trailing/partial
# close) -- matches exactly what smc_3step.py's ported EA does and what
# was backtested; adding management beyond that would be untested,
# unvalidated behavior running with real (albeit demo) order calls.
#
# ACCOUNT: this account is confirmed a DEMO account (MT5_SERVER=
# ICMarketsKE-Demo, verified in .env and directly via mt5.account_info()
# before this was written) -- real order calls are intentional here, not
# a bug. A distinct magic number keeps this strategy's positions
# identifiable and separate from anything main_stream.py's own (currently
# not running) SMC pipeline might place on the same account/symbol.
#
# HOW TO RUN (inside the mt5 container, Wine environment):
#   docker exec -u abc -e WINEPREFIX=/config/.wine mt5 wine python -m src.smc_3step_live_runner

from __future__ import annotations
import logging
import sys
import time as time_mod
from datetime import datetime, timedelta, timezone

import src.config as config
from src.infrastructure.mt5_bridge.mt5_gateway import MT5Gateway
from src.domain.entities import Candle, Timeframe, Direction
from src.application.strategy_v2 import config_v2
from src.application.smc_3step import ema_series, ThreeStepEA, ThreeStepConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("logs/smc_3step_live.log")],
)
logger = logging.getLogger("smc_3step_live")

SYMBOL = "XAUUSD"
MAGIC_NUMBER = 30500719   # distinct from any other strategy's magic number on this account
POLL_SECONDS = 15
H1_BARS_NEEDED = 220      # cfg.ema_period(50) + cfg.swing_lookback_h1(80) + margin
M15_BARS_NEEDED = 160     # cfg.zone_lookback_m15(80) + margin
M5_BARS_NEEDED = 100      # cfg.choch_lookback_m5(40) + margin

# ── Connection-stability workarounds (found empirically, 2026-07-23/27) ──
# The MT5-under-Wine Python API IPC channel hung completely (mt5.initialize()
# timing out for ALL clients, not just this process) twice within ~24h of
# real running time, with the terminal process itself staying alive and
# healthy throughout (confirmed via its own internal log) -- a genuine
# automation-interface deadlock, not a crash, not caused by concurrent
# diagnostic connections (the second hang happened with zero other
# connections made). Two mitigations, since the true Wine-side cause isn't
# fixable from the Python side:
#   1. RECONNECT_INTERVAL_SECONDS: proactively cycle mt5.shutdown() + a
#      fresh connect() periodically, before a long-held session has a
#      chance to degrade, instead of holding one IPC session open for the
#      entire runtime.
#   2. MAX_CONSECUTIVE_FAILURES: if reconnection itself is failing (the
#      hang has already happened), exit the process after this many
#      consecutive failed cycles rather than retrying forever inside a
#      Python process that may itself be in a bad state -- see
#      override_start.sh's supervising restart loop, which relaunches this
#      script (a fresh interpreter, fresh MT5Gateway) on exit.
RECONNECT_INTERVAL_SECONDS = 1800   # 30 min
MAX_CONSECUTIVE_FAILURES = 20        # ~5 min of failed cycles at POLL_SECONDS=15

cfg = ThreeStepConfig()


def connect_mt5() -> MT5Gateway:
    gw = MT5Gateway(login=config.TRADING_ID, password=config.MT5_PASSWORD,
                     server=config.MT5_SERVER, mt5_path=config.MT5_PATH)
    if not gw.connect(retries=10, delay=15.0):
        logger.critical("MT5 connection failed -- see mt5_gateway logs for detail.")
        sys.exit(1)
    return gw


def fetch_closed_candles(gw: MT5Gateway, symbol: str, tf_str: str, tf_enum: Timeframe,
                          count: int, utc_offset_hours: int):
    """Fetches `count`+1 bars via copy_rates_from_pos(start_pos=0, ...) and
    drops the LAST one -- MT5's start_pos=0 always includes the currently-
    forming (incomplete) bar as the most recent element. Every function in
    smc_3step.py assumes it is only ever given fully CLOSED candles (matching
    both the backtest and the original MQL5 EA's shift=1 CopyRates calls) --
    silently including the forming bar here would feed the strategy a bar
    whose high/low/close are still changing tick-to-tick.

    Broker timestamps are converted to UTC by subtracting utc_offset_hours,
    matching every backtest/historical-data timestamp this strategy was
    validated against (all built from `datetime.fromtimestamp(t, tz=utc)`
    on the assumption the source times were already UTC -- broker time is
    NOT UTC, so this conversion is required, not cosmetic)."""
    rows = gw.get_candles(symbol, tf_str, count + 1)
    if rows is None or len(rows) < 2:
        return None
    rows = rows[:-1]   # drop the forming bar
    offset = timedelta(hours=utc_offset_hours)
    return [
        Candle(
            time=datetime.fromtimestamp(r["time"], tz=timezone.utc) - offset,
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            volume=r["volume"], symbol=symbol, timeframe=tf_enum,
        )
        for r in rows
    ]


def has_open_position(gw: MT5Gateway) -> bool:
    positions = gw.get_open_positions()
    return any(p["symbol"] == SYMBOL and p["magic"] == MAGIC_NUMBER for p in positions)


def compute_lots(gw: MT5Gateway, sl_distance: float, risk_percent: float) -> float:
    """Uses REAL account balance and REAL broker tick_value/tick_size
    (gw.get_symbol_info) rather than the backtest's TICK_VALUE_USD
    approximation -- more accurate for live sizing, though it means the
    live $-risk-per-trade will differ slightly (should be small) from the
    backtest's own convention. Logged explicitly so this is never a silent
    discrepancy."""
    from src.application.smc_3step import calculate_lot_size
    acc = gw.get_account_info()
    sym_info = gw.get_symbol_info(SYMBOL)
    if acc is None or sym_info is None:
        logger.error("Could not fetch account/symbol info -- skipping trade.")
        return 0.0
    lots = calculate_lot_size(
        balance=acc["balance"], risk_percent=risk_percent, sl_distance=sl_distance,
        tick_value=sym_info["trade_tick_value"], tick_size=sym_info["trade_tick_size"],
        min_lot=sym_info["volume_min"], max_lot=sym_info["volume_max"], lot_step=sym_info["volume_step"],
    )
    logger.info("compute_lots: balance=%.2f risk_pct=%.2f sl_distance=%.4f tick_value=%.4f tick_size=%.6f -> lots=%.2f",
                acc["balance"], risk_percent, sl_distance, sym_info["trade_tick_value"], sym_info["trade_tick_size"], lots)
    return lots


def main():
    logger.info("Starting smc_3step live runner. symbol=%s magic=%d", SYMBOL, MAGIC_NUMBER)
    gw = connect_mt5()
    utc_offset = gw.detect_utc_offset()
    logger.info("Broker UTC offset detected: UTC%+d", utc_offset)

    ea = ThreeStepEA(cfg, SYMBOL)
    last_m5_bar_time = None
    last_reconnect = time_mod.time()
    consecutive_failures = 0

    while True:
        try:
            if time_mod.time() - last_reconnect >= RECONNECT_INTERVAL_SECONDS:
                logger.info("Proactive reconnect (interval reached) -- cycling the IPC session.")
                try:
                    import MetaTrader5 as mt5
                    mt5.shutdown()
                except Exception:
                    pass
                gw._ready = False
                if gw.connect(retries=3, delay=5.0):
                    last_reconnect = time_mod.time()
                    consecutive_failures = 0
                    logger.info("Proactive reconnect succeeded.")
                else:
                    logger.warning("Proactive reconnect failed -- will retry next cycle.")

            # Re-detect every cycle, not just at startup: detect_utc_offset()
            # falls back to 0 when the most recent tick is stale (market
            # closed, e.g. weekend) since it has nothing fresh to measure
            # against. Caching a startup-time 0 would silently misalign
            # every candle timestamp by the broker's real offset for the
            # rest of the run once the market reopens with a genuine tick.
            detected = gw.detect_utc_offset()
            if detected != 0 or utc_offset == 0:
                utc_offset = detected

            m5_probe = fetch_closed_candles(gw, SYMBOL, "M5", Timeframe.M5, 2, utc_offset)
            if m5_probe is None:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.critical(
                        "%d consecutive failed cycles -- exiting so the supervising "
                        "restart loop can relaunch with a fresh process.", consecutive_failures,
                    )
                    sys.exit(1)
                logger.warning("Could not fetch M5 candles -- retrying.")
                time_mod.sleep(POLL_SECONDS)
                continue
            consecutive_failures = 0
            latest_closed_m5_time = m5_probe[-1].time

            if latest_closed_m5_time == last_m5_bar_time:
                time_mod.sleep(POLL_SECONDS)
                continue
            last_m5_bar_time = latest_closed_m5_time
            logger.info("New M5 bar closed: %s UTC", latest_closed_m5_time.isoformat())

            if has_open_position(gw):
                logger.info("Position already open for %s (magic=%d) -- skipping evaluation.", SYMBOL, MAGIC_NUMBER)
                continue

            h1_candles = fetch_closed_candles(gw, SYMBOL, "H1", Timeframe.H1, H1_BARS_NEEDED, utc_offset)
            m15_candles = fetch_closed_candles(gw, SYMBOL, "M15", Timeframe.M15, M15_BARS_NEEDED, utc_offset)
            m5_candles = fetch_closed_candles(gw, SYMBOL, "M5", Timeframe.M5, M5_BARS_NEEDED, utc_offset)
            if h1_candles is None or m15_candles is None or m5_candles is None:
                logger.warning("Missing candle data this cycle -- skipping.")
                continue
            if len(h1_candles) < cfg.ema_period + cfg.swing_lookback_h1:
                logger.info("Not enough H1 history yet (%d bars) -- skipping.", len(h1_candles))
                continue

            h1_ema = ema_series([c.close for c in h1_candles], cfg.ema_period)
            tick = gw.get_tick(SYMBOL)
            if tick is None:
                logger.warning("No tick data -- skipping.")
                continue

            signal = ea.on_m5_bar(
                h1_candles, len(h1_candles) - 1, h1_ema,
                m15_candles, len(m15_candles) - 1,
                m5_candles, len(m5_candles) - 1,
                current_price_bid=tick["bid"], current_price_ask=tick["ask"],
            )
            if signal is None:
                continue

            logger.info("SIGNAL: direction=%s entry=%.2f sl=%.2f tp=%.2f zone_is_ob=%s",
                        "BULLISH" if signal.direction == 1 else "BEARISH",
                        signal.entry_price, signal.stop_loss, signal.take_profit, signal.zone.is_ob)

            sl_distance = abs(signal.entry_price - signal.stop_loss)
            lots = compute_lots(gw, sl_distance, cfg.risk_percent)
            if lots <= 0:
                logger.warning("Computed lot size is 0 -- not placing an order.")
                continue

            order = {
                "symbol": SYMBOL,
                "type": "BUY" if signal.direction == 1 else "SELL",
                "volume": lots,
                "price": tick["ask"] if signal.direction == 1 else tick["bid"],
                "sl": signal.stop_loss,
                "tp": signal.take_profit,
                "magic": MAGIC_NUMBER,
                "comment": "smc3step_v1",
            }
            result = gw.place_order(order)
            logger.info("ORDER RESULT: %s", result)

        except Exception:
            logger.exception("Unhandled error in main loop -- continuing after a pause.")
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.critical(
                    "%d consecutive failed cycles -- exiting so the supervising "
                    "restart loop can relaunch with a fresh process.", consecutive_failures,
                )
                sys.exit(1)
            time_mod.sleep(POLL_SECONDS)
            continue

        time_mod.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
