"""Real-data backtest for the ported SmartMoney_3Step_EA.mq5
(src/application/smc_3step.py) across XAUUSD/EURUSD/GBPUSD/USDJPY.

Uses the same real H1 (logs/phase15_h1.json, ~13mo) and M15
(logs/phase_entry_m15.json, ~20mo) pulls as strategy_v2's backtest. M5
is NOT separately pulled -- resampled from the same M1 data
(logs/phase_entry_m1.json, ~10mo, 300k bars/symbol) already on disk, so
the backtest window is capped at the ~10-month M1/M5 overlap, same
binding constraint as strategy_v2's own backtest.

FILL MODEL: SL/TP touches are checked against M5 bar high/low (the
EA's own native execution timeframe) -- not M1 intrabar precision. This
is a deliberate simplification, documented here rather than silently
assumed: a real MT5 EA checks every tick, so an M5-bar-only fill check
can occasionally miss a brief intrabar spike that would have triggered
a real broker fill. Position sizing/lot conventions (TICK_VALUE_USD,
DIGITS) are reused from tools/backtest_strategy_v2.py -- market-data
constants, not strategy logic, identical regardless of which strategy
is being tested.

HOW TO RUN
    python3 tools/backtest_smc_3step.py   (from repo root; no env vars needed)
    --out <path> optionally saves the full trade log/report as JSON.
"""
import sys, json, argparse
from datetime import datetime, timezone, timedelta
sys.path.insert(0, ".")

from src.domain.entities import Candle, Timeframe
from src.application.strategy_v2 import config_v2
from src.application.smc_3step import (
    ema_series, get_trend, find_pullback_zone, is_zone_invalidated,
    ThreeStepEA, ThreeStepConfig, calculate_lot_size,
)
from src.application.strategy_v2 import monitoring

DATA_DIR = "logs"
SYMBOLS = ["XAUUSD"]   # research scope narrowed to XAUUSD-only (Priority 4 decision, 2026-07-19):
# EURUSD/GBPUSD/USDJPY were unprofitable across nearly every segment (zone
# type, direction, session) under this exact strategy -- not a single
# fixable filter, a broad regime mismatch. tools/backtest_smc_3step.py
# still accepts symbols=[...] to re-test FX later if this changes.
ALL_SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]
DIGITS = {"XAUUSD": 2, "EURUSD": 5, "GBPUSD": 5, "USDJPY": 3}
TICK_VALUE_USD = {"EURUSD": 10.0, "GBPUSD": 10.0, "XAUUSD": 10.0, "USDJPY": 9.1}
MIN_LOT, MAX_LOT, LOT_STEP = 0.01, 10.0, 0.01
INITIAL_BALANCE = 10000.0

# ── Realistic cost model (IC Markets Raw Spread account, per src/config.py) ──
# Commission is real (IC_COMMISSION_USD, src/config.py: $3.50/side, all
# instruments, $7.00 round-trip per lot). Spread/slippage are NOT backed by
# a historical spread log for this account (none exists in this repo) --
# FLAGGED ASSUMPTIONS, chosen as reasonable-but-conservative raw-ECN
# estimates, not measured. XAUUSD is the priority symbol per current
# research direction; its spread estimate matters most here.
COMMISSION_PER_LOT_ROUNDTRIP = 7.00   # USD, all symbols (IC Markets Raw, $3.50/side)
SPREAD_POINTS = {"XAUUSD": 20, "EURUSD": 5, "GBPUSD": 7, "USDJPY": 7}   # points, one-way
SLIPPAGE_POINTS = {"XAUUSD": 5, "EURUSD": 2, "GBPUSD": 2, "USDJPY": 2}   # points, PER FILL (entry and exit each)


def to_candles(sym, rows, tf):
    return [Candle(time=datetime.fromtimestamp(r["time"], tz=timezone.utc), open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"], volume=float(r["tick_volume"]), symbol=sym, timeframe=tf)
            for r in rows]


def resample_m1_to_m5(m1_candles, symbol):
    """Groups M1 candles by 5-minute UTC floor. Partial buckets (data
    gaps at weekend boundaries) are kept as-is using whatever bars fall
    in them, rather than dropped -- matches how a live EA would just see
    fewer ticks in a gappy period, not a missing bar."""
    buckets = {}
    order = []
    for c in m1_candles:
        floor = c.time.replace(minute=(c.time.minute // 5) * 5, second=0, microsecond=0)
        if floor not in buckets:
            buckets[floor] = []
            order.append(floor)
        buckets[floor].append(c)
    out = []
    for floor in order:
        group = buckets[floor]
        out.append(Candle(
            time=floor, open=group[0].open, high=max(g.high for g in group),
            low=min(g.low for g in group), close=group[-1].close,
            volume=sum(g.volume for g in group), symbol=symbol, timeframe=Timeframe.M5,
        ))
    return out


def run_backtest(apply_costs=True, symbols=None, cost_multiplier=1.0):
    """cost_multiplier scales SPREAD_POINTS/SLIPPAGE_POINTS only (both are
    flagged assumptions, not measured broker data) -- commission is left
    at its real, measured rate regardless, since scaling a known-real cost
    would misrepresent it as uncertain."""
    RAW_H1 = json.load(open(f"{DATA_DIR}/phase15_h1.json"))
    RAW_M15 = json.load(open(f"{DATA_DIR}/phase_entry_m15.json"))
    RAW_M1 = json.load(open(f"{DATA_DIR}/phase_entry_m1.json"))

    cfg = ThreeStepConfig()
    trade_log = []
    per_symbol_summary = {}
    running_equity = [INITIAL_BALANCE]

    for sym in (symbols or SYMBOLS):
        h1_candles = to_candles(sym, RAW_H1["symbols"][sym]["rates"], Timeframe.H1)
        m15_candles = to_candles(sym, RAW_M15["symbols"][sym]["rates"], Timeframe.M15)
        m1_candles = to_candles(sym, RAW_M1["symbols"][sym]["rates"], Timeframe.M1)
        m5_candles = resample_m1_to_m5(m1_candles, sym)

        h1_ema = ema_series([c.close for c in h1_candles], cfg.ema_period)
        h1_time_to_idx = {c.time: i for i, c in enumerate(h1_candles)}
        m15_time_to_idx = {c.time: i for i, c in enumerate(m15_candles)}

        ea = ThreeStepEA(cfg, sym)
        open_pos = None   # dict: direction, entry, sl, tp, lots, opened_at
        n_signals = n_opened = n_closed = 0
        wins = losses = 0

        for j in range(len(m5_candles)):
            m5_candle = m5_candles[j]

            if open_pos is not None:
                # MAE/MFE: intrabar best/worst excursion vs entry, this bar.
                if open_pos["direction"] == 1:
                    fav_excursion = m5_candle.high - open_pos["entry"]
                    adv_excursion = open_pos["entry"] - m5_candle.low
                else:
                    fav_excursion = open_pos["entry"] - m5_candle.low
                    adv_excursion = m5_candle.high - open_pos["entry"]
                open_pos["mfe"] = max(open_pos["mfe"], fav_excursion)
                open_pos["mae"] = max(open_pos["mae"], adv_excursion)

                hit_sl = (m5_candle.low <= open_pos["sl"]) if open_pos["direction"] == 1 else (m5_candle.high >= open_pos["sl"])
                hit_tp = (m5_candle.high >= open_pos["tp"]) if open_pos["direction"] == 1 else (m5_candle.low <= open_pos["tp"])
                if hit_sl or hit_tp:
                    exit_price = open_pos["sl"] if hit_sl else open_pos["tp"]
                    if apply_costs:
                        # Exit slippage: fills worse than the requested SL/TP
                        # level, in the adverse direction, both directions.
                        slip = SLIPPAGE_POINTS[sym] * cost_multiplier * config_v2.point_size(sym)
                        exit_price = exit_price - slip if open_pos["direction"] == 1 else exit_price + slip
                    move = (exit_price - open_pos["entry"]) if open_pos["direction"] == 1 else (open_pos["entry"] - exit_price)
                    # Mirrors tools/backtest_strategy_v2.py's realize_pnl exactly:
                    # pips = move/pip_size; pip_value = TICK_VALUE_USD*10 (1 pip =
                    # 10 MT5 ticks/points); pnl = pips*pip_value*lots. Must use the
                    # SAME formula the lot-sizing call below uses, or risk-per-trade
                    # silently stops meaning what InpRiskPercent says it means.
                    pips_moved = move / config_v2.pip_size(sym)
                    pip_value = TICK_VALUE_USD[sym] * 10
                    pnl = pips_moved * pip_value * open_pos["lots"]
                    if apply_costs:
                        pnl -= COMMISSION_PER_LOT_ROUNDTRIP * open_pos["lots"]
                    running_equity[0] += pnl
                    initial_risk = abs(open_pos["entry"] - open_pos["sl"])
                    trade_log.append({
                        "symbol": sym, "direction": "bullish" if open_pos["direction"] == 1 else "bearish",
                        "entry": open_pos["entry"], "sl": open_pos["sl"], "tp": open_pos["tp"],
                        "lots": open_pos["lots"], "pnl": round(pnl, 2),
                        "outcome": "WIN" if hit_tp else "LOSS",
                        "opened_at": open_pos["opened_at"].isoformat(), "closed_at": m5_candle.time.isoformat(),
                        "mfe_r": round(open_pos["mfe"] / initial_risk, 3) if initial_risk > 0 else None,
                        "mae_r": round(open_pos["mae"] / initial_risk, 3) if initial_risk > 0 else None,
                        "zone_is_ob": open_pos["zone_is_ob"], "entry_hour_utc": open_pos["opened_at"].hour,
                    })
                    if hit_tp:
                        wins += 1
                    else:
                        losses += 1
                    n_closed += 1
                    open_pos = None
                continue

            # The most recently FULLY CLOSED H1/M15 bar as of this M5 bar's
            # close is always one full period behind the floor of its own
            # timestamp (the bar covering "now" hasn't closed yet) --
            # matches tools/backtest_strategy_v2.py's own alignment logic.
            floored_h1_hour = m5_candle.time.replace(minute=0, second=0, microsecond=0)
            prev_h1_hour = floored_h1_hour - timedelta(hours=1)
            h1_idx = h1_time_to_idx.get(prev_h1_hour)
            floored_m15 = m5_candle.time.replace(minute=(m5_candle.time.minute // 15) * 15, second=0, microsecond=0)
            prev_m15_slot = floored_m15 - timedelta(minutes=15)
            m15_idx = m15_time_to_idx.get(prev_m15_slot)

            if h1_idx is None or m15_idx is None or j < 3:
                continue
            if h1_idx < cfg.ema_period or h1_idx < cfg.swing_lookback_h1:
                continue

            point = config_v2.point_size(sym)
            if apply_costs:
                half_spread = SPREAD_POINTS[sym] * cost_multiplier * point / 2.0
                bid = m5_candle.close - half_spread
                ask = m5_candle.close + half_spread
            else:
                bid = m5_candle.close
                ask = m5_candle.close + 1 * point   # 1-point synthetic floor, no real cost
            signal = ea.on_m5_bar(h1_candles, h1_idx, h1_ema, m15_candles, m15_idx,
                                   m5_candles, j, current_price_bid=bid, current_price_ask=ask)
            if signal is None:
                continue
            n_signals += 1

            if apply_costs:
                # Entry slippage: filled worse than the quoted bid/ask, in
                # the adverse direction.
                slip = SLIPPAGE_POINTS[sym] * cost_multiplier * point
                if signal.direction == 1:
                    signal.entry_price += slip
                else:
                    signal.entry_price -= slip

            sl_distance = abs(signal.entry_price - signal.stop_loss)
            # calculate_lot_size's value_per_unit = tick_value/tick_size. Passing
            # tick_value=TICK_VALUE_USD*10 (the pip_value, not the raw per-point
            # value) and tick_size=pip_size makes loss_per_lot = sl_pips*pip_value
            # -- the same formula the settlement code above uses. Passing
            # TICK_VALUE_USD alone here (without the x10) would size positions
            # 10x too large relative to what the settlement side actually pays
            # out per pip, silently breaking "risk 1% per trade."
            lots = calculate_lot_size(
                balance=running_equity[0], risk_percent=cfg.risk_percent, sl_distance=sl_distance,
                tick_value=TICK_VALUE_USD[sym] * 10, tick_size=config_v2.pip_size(sym),
                min_lot=MIN_LOT, max_lot=MAX_LOT, lot_step=LOT_STEP,
            )
            if lots <= 0:
                continue
            n_opened += 1
            open_pos = {"direction": signal.direction, "entry": signal.entry_price, "sl": signal.stop_loss,
                        "tp": signal.take_profit, "lots": lots, "opened_at": m5_candle.time,
                        "mfe": 0.0, "mae": 0.0, "zone_is_ob": signal.zone.is_ob}

        per_symbol_summary[sym] = {"m5_bars": len(m5_candles), "signals": n_signals, "opened": n_opened,
                                    "closed": n_closed, "wins": wins, "losses": losses}
        print(f"{sym}: M5 bars={len(m5_candles)} signals={n_signals} opened={n_opened} "
              f"closed={n_closed} wins={wins} losses={losses}", flush=True)

    return trade_log, per_symbol_summary, running_equity[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--symbols", default=None, help="comma-separated, e.g. XAUUSD")
    parser.add_argument("--no-costs", action="store_true", help="disable commission/spread/slippage")
    args = parser.parse_args()
    syms = args.symbols.split(",") if args.symbols else None

    trade_log, per_symbol_summary, final_equity = run_backtest(apply_costs=not args.no_costs, symbols=syms)

    print(); print("="*78); print("PER-SYMBOL SUMMARY"); print("="*78)
    for sym, s in per_symbol_summary.items():
        print(f"  {sym}: {s}")

    print(); print("="*78); print("TRADE LOG"); print("="*78)
    for t in trade_log:
        print(f"  {t}")

    print(); print("="*78); print("PERFORMANCE REPORT"); print("="*78)
    r_multiples = None
    if trade_log:
        report = {
            "total_trades": len(trade_log),
            "wins": sum(1 for t in trade_log if t["outcome"] == "WIN"),
            "losses": sum(1 for t in trade_log if t["outcome"] == "LOSS"),
            "total_pnl": round(sum(t["pnl"] for t in trade_log), 2),
        }
        report["win_rate_pct"] = round(100.0 * report["wins"] / report["total_trades"], 1)
        gains = sum(t["pnl"] for t in trade_log if t["pnl"] > 0)
        loss_sum = abs(sum(t["pnl"] for t in trade_log if t["pnl"] < 0))
        report["profit_factor"] = round(gains / loss_sum, 2) if loss_sum > 0 else None
        report["expectancy"] = round(report["total_pnl"] / report["total_trades"], 2)
    else:
        report = {"total_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
                   "total_pnl": 0.0, "profit_factor": None, "expectancy": None}
    for k, v in report.items():
        print(f"  {k}: {v}")
    print(f"\n  final_equity: {final_equity:.2f}  (initial: {INITIAL_BALANCE:.2f})")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"per_symbol_summary": per_symbol_summary, "trade_log": trade_log,
                        "report": report, "final_equity": final_equity}, f, indent=2)
        print(f"\nWrote {args.out}")
