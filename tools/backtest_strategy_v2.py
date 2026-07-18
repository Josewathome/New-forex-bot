"""strategy_v2 complete-system backtester.

Wires every strategy_v2 module together into one forward-only, decision-
time-only simulation: H1 structure/FVG/OB/liquidity -> POIManager (with
CHoCH cleanup) -> premium/discount classification -> M1 local structure +
entry confirmation -> Trade Decision Engine (qualification) -> Risk
Management (position sizing) -> a bar-by-bar fill/exit simulation using
Trade Management's exit ladder -> Monitoring (final report).

DATA CONSTRAINT (reported honestly, not worked around): M1 data is only
available for ~10 months (2025-09 to 2026-07) on this account, vs. H1's
13 months. The backtest window is therefore capped at the M1/H1
OVERLAP -- entry confirmation requires M1 candles, so no signal can be
evaluated outside that window regardless of how much H1 history exists.
This is NOT a multi-year backtest; it is bounded by the real data actually
available, and that shortfall is reported as a fact, not stretched.

NEWS VALIDATION: no historical economic-calendar data source is available
in this sandbox (no internet access). The news_check passed to
trade_decision.qualify_trade() always returns False (never blocks) --
documented as an untested gate, not silently assumed harmless.

FILL MODEL: entry_confirmation.EntrySignal.entry_price is the confirming
M1 candle's OWN close (a momentum/displacement entry, per how Module 4
was actually built and validated) -- not a resting limit order waiting
for a retrace. The backtester therefore fills at that close price
directly (no separate pending-order wait-and-expire step), consistent
with the entry model actually implemented and tested in Module 4. This
is a documented consistency choice, not a redesign of Module 4.

STOP-LOSS / TAKE-PROFIT FILL SIMULATION: trade_management.py only
RECOMMENDS SL/action changes, it does not simulate whether price actually
crossed a stop -- that is inherently the backtester/broker's job. Every
open bar is checked here for an intrabar SL or TP touch (using the
bar's high/low, not just its close) before applying trade_management's
non-close recommendations (breakeven/trail).

HOW TO RUN
    python3 tools/backtest_strategy_v2.py   (from repo root; no env vars needed)
    Writes tools/../logs is NOT touched; results are printed and optionally
    saved via --out <path> for downstream analysis (walk-forward/Monte
    Carlo consume the same trade-log JSON shape).
"""
import sys, json, argparse
from datetime import datetime, timezone, timedelta
sys.path.insert(0, ".")

from src.domain.entities import Candle, Direction, Timeframe
from src.domain.strategy_v2_entities import TrendState, SwingType
from src.application.strategy_v2 import config_v2
from src.application.strategy_v2.structure import StructureEngine
from src.application.strategy_v2.atr import wilder_atr_series
from src.application.strategy_v2 import fvg as fvg_mod
from src.application.strategy_v2 import order_blocks as ob_mod
from src.application.strategy_v2 import liquidity as liq_mod
from src.application.strategy_v2.poi_manager import POIManager
from src.application.strategy_v2.entry_confirmation import evaluate_entry
from src.application.strategy_v2.trade_decision import qualify_trade, AccountState
from src.application.strategy_v2.risk_management import compute_position_size, select_risk_pct
from src.application.strategy_v2.trade_management import TradeState, evaluate_trade
from src.application.strategy_v2 import monitoring

SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]
DIGITS = {"XAUUSD": 2, "EURUSD": 5, "GBPUSD": 5, "USDJPY": 3}
# Transcribed from src/config.py (not imported -- that module validates
# live MT5 credentials at import time, which this sandbox does not have
# and should not need for a backtest).
TICK_VALUE_USD = {"EURUSD": 10.0, "GBPUSD": 10.0, "XAUUSD": 10.0, "USDJPY": 9.1}
SYMBOL_CURRENCIES = {
    "EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"], "XAUUSD": ["USD"], "USDJPY": ["USD", "JPY"],
}
MAX_LOT_SIZE = 10.0
COMMISSION_PER_LOT = 3.0
INITIAL_BALANCE = 10000.0


def to_candles(sym, rows, tf):
    return [Candle(time=datetime.fromtimestamp(r["time"], tz=timezone.utc), open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"], volume=float(r["tick_volume"]), symbol=sym, timeframe=tf)
            for r in rows]


def build_h1_snapshots(sym, h1_candles):
    """Per-H1-bar snapshot of (active_pair, active FVG/OB bounds by
    direction, swept confirmed_reversal liquidity levels) -- same
    construction already proven in the Module 4/5 real-data replays."""
    atr_h1 = wilder_atr_series(h1_candles, config_v2.ATR_PERIOD)
    tol = liq_mod.equal_level_tolerance(sym)
    lookback_bars = config_v2.EQUAL_LEVEL_LOOKBACK_DAYS * 24

    asia_highs, asia_lows = liq_mod.detect_asia_levels(h1_candles, sym)
    prev_highs, prev_lows = liq_mod.detect_prev_day_levels(h1_candles, sym)
    h1_time_to_idx = {c.time: i for i, c in enumerate(h1_candles)}
    levels_by_idx = {}
    for lv in asia_highs + asia_lows + prev_highs + prev_lows:
        idx = h1_time_to_idx.get(lv.created_at)
        if idx is not None:
            levels_by_idx.setdefault(idx, []).append(lv)

    eng = StructureEngine(sym)
    mgr = POIManager(sym)
    snapshots = []
    for i in range(len(h1_candles)):
        if i >= 2:
            c1, c2, c3 = h1_candles[i - 2], h1_candles[i - 1], h1_candles[i]
            atr_at_c2 = atr_h1[i - 2]
            new_fvg = fvg_mod.detect_fvg(sym, Timeframe.H1, c1, c2, c3, atr_at_c2)
            if new_fvg is not None:
                mgr.add_fvg(new_fvg)
            new_ob = ob_mod.detect_order_block(sym, Timeframe.H1, h1_candles, i - 2, c2, atr_at_c2)
            if new_ob is not None:
                mgr.add_ob(new_ob[0])
        for lv in levels_by_idx.get(i, []):
            mgr.add_liquidity(lv)

        ev = eng.process_new_candle_close(h1_candles, i)
        for s in ev.new_swings:
            eq_lvl = liq_mod.detect_equal_level(sym, eng.swing_log, s, tol, lookback_bars)
            if eq_lvl is not None:
                mgr.add_liquidity(eq_lvl)

        mgr.update(h1_candles[i])
        if ev.choch_fired:
            mgr.on_choch(ev.invalidated_trend)

        snapshots.append({
            "time": h1_candles[i].time,
            "pair": eng.active_pair,
            "fvg": [(z.gap_low, z.gap_high, z.direction) for z in mgr.active_fvgs()],
            "ob": [(z.low, z.high, z.direction) for z in mgr.active_obs()],
            "swept_reversals": [lv for lv in mgr.liquidity_levels
                                 if lv.reaction == "confirmed_reversal" and lv.swept_at is not None],
        })
    return snapshots


def run_backtest():
    trade_log = []       # TradeJournal-shaped closed-trade records
    open_positions = {}  # symbol -> dict(state=TradeState, lots=float, ticket=int, direction, risk_amount, sl_original)
    ticket_counter = [0]
    running_equity = [INITIAL_BALANCE]
    daily_realized_pnl = {}  # "YYYY-MM-DD" -> pnl
    peak_equity = [INITIAL_BALANCE]

    def account_state_for(symbol, now):
        open_for_symbol = 1 if symbol in open_positions else 0
        open_count = len(open_positions)
        exposure_pct = sum(p["risk_pct"] for p in open_positions.values())
        today_key = now.strftime("%Y-%m-%d")
        today_pnl_pct = (daily_realized_pnl.get(today_key, 0.0) / running_equity[0]) * 100 if running_equity[0] else 0.0
        dd_pct = ((peak_equity[0] - running_equity[0]) / peak_equity[0] * 100) if peak_equity[0] > 0 else 0.0
        return AccountState(open_trades_count=open_count, open_trades_for_symbol=open_for_symbol,
                             portfolio_exposure_pct=exposure_pct, today_realized_pnl_pct=today_pnl_pct,
                             current_drawdown_pct=dd_pct)

    def realize_pnl(symbol, price_move_favorable, lots, close_frac):
        pip_calc_pips = price_move_favorable  # already price units; convert below
        digits = DIGITS[symbol]
        pip_size = config_v2.pip_size(symbol)
        pips = price_move_favorable / pip_size
        pip_value = TICK_VALUE_USD[symbol] * 10
        pnl = pips * pip_value * lots * close_frac
        running_equity[0] += pnl
        peak_equity[0] = max(peak_equity[0], running_equity[0])
        return pnl

    RAW_H1 = json.load(open("/tmp/claude-1000/-home-josewathome-project-claude-session/416d9b86-909d-4b9b-b991-7ee4b51bf0a8/scratchpad/phase15_h1.json"))
    RAW_M1 = json.load(open("/tmp/claude-1000/-home-josewathome-project-claude-session/416d9b86-909d-4b9b-b991-7ee4b51bf0a8/scratchpad/phase_entry_m1.json"))

    per_symbol_summary = {}

    for sym in SYMBOLS:
        h1_candles = to_candles(sym, RAW_H1["symbols"][sym]["rates"], Timeframe.H1)
        m1_candles = to_candles(sym, RAW_M1["symbols"][sym]["rates"], Timeframe.M1)
        atr_m1 = wilder_atr_series(m1_candles, config_v2.ATR_PERIOD)

        h1_snapshots = build_h1_snapshots(sym, h1_candles)
        h1_hour_to_idx = {snap["time"]: i for i, snap in enumerate(h1_snapshots)}

        m1_eng = StructureEngine(sym)
        n_signals = n_qualified = n_opened = n_closed = 0

        for j in range(len(m1_candles)):
            m1_candle = m1_candles[j]

            # --- Manage any open position for this symbol first ---
            if sym in open_positions:
                pos = open_positions[sym]
                ts = pos["state"]
                direction = ts.direction

                # Intrabar SL/TP fill check (execution-layer, not
                # trade_management's job -- see module docstring).
                hit_sl = (m1_candle.low <= ts.current_sl) if direction == Direction.BULLISH else (m1_candle.high >= ts.current_sl)
                hit_tp = (m1_candle.high >= pos["take_profit"]) if direction == Direction.BULLISH else (m1_candle.low <= pos["take_profit"])

                if hit_sl:
                    exit_price = ts.current_sl
                    move = (exit_price - ts.entry_price) if direction == Direction.BULLISH else (ts.entry_price - exit_price)
                    pnl = realize_pnl(sym, move, pos["lots"], ts.remaining_pct)
                    outcome = "LOSS" if pnl < 0 else "WIN_PARTIAL"  # breakeven-or-better stop-out
                    trade_log.append({
                        "ticket": pos["ticket"], "symbol": sym, "direction": direction.value, "entry": ts.entry_price,
                        "sl": ts.current_sl, "tp": pos["take_profit"], "lot_size": pos["lots"], "grade": "?",
                        "pnl": round(pnl, 2), "outcome": outcome, "opened_at": ts.opened_at.isoformat(),
                        "closed_at": m1_candle.time.isoformat(), "risk_amount": pos["risk_amount"],
                    })
                    daily_realized_pnl[m1_candle.time.strftime("%Y-%m-%d")] = daily_realized_pnl.get(m1_candle.time.strftime("%Y-%m-%d"), 0.0) + pnl
                    del open_positions[sym]
                    n_closed += 1
                elif hit_tp:
                    exit_price = pos["take_profit"]
                    move = (exit_price - ts.entry_price) if direction == Direction.BULLISH else (ts.entry_price - exit_price)
                    pnl = realize_pnl(sym, move, pos["lots"], ts.remaining_pct)
                    trade_log.append({
                        "ticket": pos["ticket"], "symbol": sym, "direction": direction.value, "entry": ts.entry_price,
                        "sl": ts.current_sl, "tp": pos["take_profit"], "lot_size": pos["lots"], "grade": "?",
                        "pnl": round(pnl, 2), "outcome": "WIN_FULL", "opened_at": ts.opened_at.isoformat(),
                        "closed_at": m1_candle.time.isoformat(), "risk_amount": pos["risk_amount"],
                    })
                    daily_realized_pnl[m1_candle.time.strftime("%Y-%m-%d")] = daily_realized_pnl.get(m1_candle.time.strftime("%Y-%m-%d"), 0.0) + pnl
                    del open_positions[sym]
                    n_closed += 1
                else:
                    actions = evaluate_trade(ts, m1_candle.close, sym, m1_candle.time, atr_m1[j - 1] if j >= 1 else None)
                    for act in actions:
                        if act.action == "partial_close":
                            move = (m1_candle.close - ts.entry_price) if direction == Direction.BULLISH else (ts.entry_price - m1_candle.close)
                            pnl = realize_pnl(sym, move, pos["lots"], act.close_pct)
                            daily_realized_pnl[m1_candle.time.strftime("%Y-%m-%d")] = daily_realized_pnl.get(m1_candle.time.strftime("%Y-%m-%d"), 0.0) + pnl
                        elif act.action == "full_close":
                            move = (m1_candle.close - ts.entry_price) if direction == Direction.BULLISH else (ts.entry_price - m1_candle.close)
                            pnl = realize_pnl(sym, move, pos["lots"], ts.remaining_pct)
                            trade_log.append({
                                "ticket": pos["ticket"], "symbol": sym, "direction": direction.value, "entry": ts.entry_price,
                                "sl": ts.current_sl, "tp": pos["take_profit"], "lot_size": pos["lots"], "grade": "?",
                                "pnl": round(pnl, 2), "outcome": "MONITOR_CLOSE", "opened_at": ts.opened_at.isoformat(),
                                "closed_at": m1_candle.time.isoformat(), "risk_amount": pos["risk_amount"],
                            })
                            daily_realized_pnl[m1_candle.time.strftime("%Y-%m-%d")] = daily_realized_pnl.get(m1_candle.time.strftime("%Y-%m-%d"), 0.0) + pnl
                            del open_positions[sym]
                            n_closed += 1
                # continue to next M1 bar for this symbol -- one position at a time (MAX_TRADES_PER_SYMBOL=1)
                m1_eng.process_new_candle_close(m1_candles, j)
                continue

            # --- No open position: evaluate for a new entry ---
            m1_ev = m1_eng.process_new_candle_close(m1_candles, j)
            floored_hour = m1_candle.time.replace(minute=0, second=0, microsecond=0)
            prev_h1_hour = floored_hour - timedelta(hours=1)
            h1_idx = h1_hour_to_idx.get(prev_h1_hour)
            if h1_idx is None or m1_ev.trend == TrendState.NEUTRAL:
                continue
            snap = h1_snapshots[h1_idx]
            pair = snap["pair"]
            if pair is None:
                continue

            direction = Direction.BULLISH if m1_ev.trend == TrendState.BULLISH else Direction.BEARISH
            want_kind = SwingType.HIGH if direction == Direction.BULLISH else SwingType.LOW
            local_swing = next((s for s in reversed(m1_eng.swing_log) if s.kind == want_kind), None)
            if local_swing is None:
                continue
            want_sell_side = direction == Direction.BULLISH
            candidate_sweeps = [lv for lv in snap["swept_reversals"]
                                 if liq_mod.is_sell_side(lv.kind) == want_sell_side and lv.swept_at <= m1_candle.time]
            if not candidate_sweeps:
                continue
            sweep_level = max(candidate_sweeps, key=lambda lv: lv.swept_at)
            candidate_zones = [(lo, hi, "fvg") for lo, hi, d in snap["fvg"] if d == direction] + \
                               [(lo, hi, "ob") for lo, hi, d in snap["ob"] if d == direction]
            if not candidate_zones:
                continue

            atr_prev = atr_m1[j - 1] if j >= 1 else None
            for zone_low, zone_high, kind in candidate_zones:
                signal = evaluate_entry(
                    symbol=sym, direction=direction, pair=pair, sweep_level=sweep_level, poi_kind=kind,
                    zone_low=zone_low, zone_high=zone_high, entry_candle=m1_candle, local_trend=m1_ev.trend,
                    local_swing_price=local_swing.price, m1_atr_at_prev=atr_prev, opposite_asia_level=None,
                )
                if signal is None:
                    continue
                n_signals += 1

                account = account_state_for(sym, m1_candle.time)
                risk_pct = select_risk_pct(use_scaled=False)
                decision = qualify_trade(signal, sym, m1_candle.time, account, risk_pct, news_check=lambda s, n: False)
                if not decision.qualified:
                    continue
                n_qualified += 1

                lots = compute_position_size(
                    account_balance=running_equity[0], risk_pct=risk_pct, entry_price=signal.entry_price,
                    stop_loss=signal.stop_loss, symbol=sym, tick_value=TICK_VALUE_USD[sym],
                    commission_per_lot=COMMISSION_PER_LOT, digits=DIGITS[sym], max_lot_size=MAX_LOT_SIZE,
                )
                if lots <= 0:
                    continue
                n_opened += 1

                sl_distance = abs(signal.entry_price - signal.stop_loss)
                pip_value = TICK_VALUE_USD[sym] * 10
                risk_amount = (sl_distance / config_v2.pip_size(sym)) * pip_value * lots

                ticket_counter[0] += 1
                ts = TradeState(direction=direction, entry_price=signal.entry_price, initial_risk=sl_distance,
                                 current_sl=signal.stop_loss, opened_at=m1_candle.time)
                open_positions[sym] = {"state": ts, "lots": lots, "ticket": ticket_counter[0],
                                        "take_profit": signal.take_profit, "risk_amount": risk_amount,
                                        "risk_pct": risk_pct}
                break  # one zone's signal is enough; don't open multiple positions on the same bar

        per_symbol_summary[sym] = {"signals": n_signals, "qualified": n_qualified, "opened": n_opened, "closed": n_closed}
        print(f"{sym}: M1 bars={len(m1_candles)} signals={n_signals} qualified={n_qualified} opened={n_opened} closed={n_closed}")

    return trade_log, per_symbol_summary, running_equity[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    trade_log, summary, final_equity = run_backtest()

    print("\n" + "=" * 78)
    print("PER-SYMBOL FUNNEL SUMMARY")
    print("=" * 78)
    for sym, s in summary.items():
        print(f"  {sym}: {s}")

    print("\n" + "=" * 78)
    print("TRADE LOG")
    print("=" * 78)
    for t in trade_log:
        print(f"  {t['symbol']} {t['direction']} entry={t['entry']:.5f} sl={t['sl']:.5f} tp={t['tp']:.5f} "
              f"lots={t['lot_size']:.4f} pnl={t['pnl']:+.2f} outcome={t['outcome']} "
              f"opened={t['opened_at']} closed={t['closed_at']}")

    print("\n" + "=" * 78)
    print("PERFORMANCE REPORT")
    print("=" * 78)
    report = monitoring.performance_report(trade_log, INITIAL_BALANCE)
    for k, v in report.items():
        print(f"  {k}: {v}")
    print(f"\n  final_equity: {final_equity:.2f}  (initial: {INITIAL_BALANCE:.2f})")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"trade_log": trade_log, "summary": summary, "report": report,
                       "final_equity": final_equity, "initial_balance": INITIAL_BALANCE}, f, indent=1)
        print(f"\nWrote {args.out}")
