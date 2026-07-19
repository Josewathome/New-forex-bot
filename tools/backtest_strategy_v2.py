"""strategy_v2 complete-system backtester.

CORRECTED per docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md. Wires every
strategy_v2 module together into one forward-only, decision-time-only
simulation: H1 structure/bias -> M15 HTF POI (FVG/OB) + M15+H1 liquidity
(merged) -> POIManager (with CHoCH cleanup) -> premium/discount
classification -> M1-specific Entry Zone (FVG/OB) + stateless M1
confirmation (Absorption + CHoCH-trigger + displacement) -> Trade
Decision Engine (qualification) -> Risk Management (position sizing) ->
a bar-by-bar fill/exit simulation using Trade Management's exit ladder ->
Monitoring (final report).

This replaces an earlier version that ran H1-only POIs and a persistent-
M1-trend-state confirmation mechanism -- both contradicted the locked V4
spec (sections 1/11/16) and were corrected after a real-data parity audit.

DATA CONSTRAINT (reported honestly, not worked around): M1 data is only
available for ~10 months (2025-09 to 2026-07) on this account, vs. H1's
13 months and M15's ~20 months. The backtest window is capped at the
narrowest overlap (M1) -- entry confirmation requires M1 candles, so no
signal can be evaluated outside that window regardless of how much H1/M15
history exists. This is NOT a multi-year backtest.

NEWS VALIDATION: no historical economic-calendar data source is available
in this sandbox (no internet access). The news_check passed to
trade_decision.qualify_trade() always returns False (never blocks) --
documented as an untested gate, not silently assumed harmless.

FILL MODEL: entry_confirmation.EntrySignal.entry_price is the Entry
Zone's own computed price (spec section 17: 50% of the M1 OB+FVG
intersection, or the deeper discount/premium structure), NOT the
confirming candle's close. The backtester fills at that computed price
directly on the confirming bar (no separate pending-order wait-and-expire
step -- the recovered spec text does not specify one; see entry_
confirmation.py's E6b note).

STOP-LOSS / TAKE-PROFIT FILL SIMULATION: trade_management.py only
RECOMMENDS SL/action changes, it does not simulate whether price actually
crossed a stop -- that is inherently the backtester/broker's job. Every
open bar is checked here for an intrabar SL or TP touch (using the bar's
high/low, not just its close) before applying trade_management's
non-close recommendations (breakeven/trail).

HOW TO RUN
    python3 tools/backtest_strategy_v2.py   (from repo root; no env vars needed)
    --out <path> optionally saves the full trade log/report as JSON.
"""
import sys, json, argparse, bisect
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
from src.application.strategy_v2 import swings as swings_mod
from src.application.strategy_v2.poi_manager import POIManager
from src.application.strategy_v2.entry_confirmation import (
    evaluate_entry, poi_touched, poi_in_favorable_zone, m1_confirmation, compute_entry_zone,
)
from src.application.strategy_v2.trade_decision import qualify_trade, AccountState
from src.application.strategy_v2.risk_management import compute_position_size, select_risk_pct
from src.application.strategy_v2.trade_management import TradeState, evaluate_trade
from src.application.strategy_v2 import monitoring

DATA_DIR = "logs"  # durable data pull location (relative to repo root); NOT the ephemeral scratchpad
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]
DIGITS = {"XAUUSD": 2, "EURUSD": 5, "GBPUSD": 5, "USDJPY": 3}
# Transcribed from src/config.py (not imported -- that module validates
# live MT5 credentials at import time, which this sandbox does not have
# and should not need for a backtest).
TICK_VALUE_USD = {"EURUSD": 10.0, "GBPUSD": 10.0, "XAUUSD": 10.0, "USDJPY": 9.1}
MAX_LOT_SIZE = 10.0
COMMISSION_PER_LOT = 3.0
INITIAL_BALANCE = 10000.0
MAX_TOUCH_AGE = timedelta(hours=6)   # flagged assumption -- see entry_confirmation.zone_touch_confirmed
MAX_SWEEP_AGE = timedelta(hours=6)


def to_candles(sym, rows, tf):
    return [Candle(time=datetime.fromtimestamp(r["time"], tz=timezone.utc), open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"], volume=float(r["tick_volume"]), symbol=sym, timeframe=tf)
            for r in rows]


def build_h1_state(sym, h1_candles):
    """H1 structure/bias + H1 Asia/prev-day/equal liquidity levels."""
    atr_h1 = wilder_atr_series(h1_candles, config_v2.ATR_PERIOD)
    tol = liq_mod.equal_level_tolerance(sym)
    lookback_bars_h1 = config_v2.EQUAL_LEVEL_LOOKBACK_DAYS * 24

    eng = StructureEngine(sym)
    h1_snapshots = []
    asia_hi, asia_lo = liq_mod.detect_asia_levels(h1_candles, sym, timeframe=Timeframe.H1)
    prev_hi, prev_lo = liq_mod.detect_prev_day_levels(h1_candles, sym, timeframe=Timeframe.H1)
    h1_time_to_idx = {c.time: i for i, c in enumerate(h1_candles)}
    levels_by_idx = {}
    for lv in asia_hi + asia_lo + prev_hi + prev_lo:
        idx = h1_time_to_idx.get(lv.created_at)
        if idx is not None:
            levels_by_idx.setdefault(idx, []).append(lv)

    h1_equal_levels = []
    for i in range(len(h1_candles)):
        ev = eng.process_new_candle_close(h1_candles, i)
        for s in ev.new_swings:
            eq = liq_mod.detect_equal_level(sym, eng.swing_log, s, tol, lookback_bars_h1)
            if eq is not None:
                h1_equal_levels.append(eq)
        h1_snapshots.append({"time": h1_candles[i].time, "pair": eng.active_pair})
    h1_hour_to_idx = {s["time"]: i for i, s in enumerate(h1_snapshots)}
    return h1_snapshots, h1_hour_to_idx, levels_by_idx, asia_hi, asia_lo, prev_hi, prev_lo, h1_equal_levels


def build_m15_state(sym, m15_candles):
    """M15 HTF POI (FVG/OB, the frozen zone-detection functions run on M15
    candles instead of H1 -- spec section 1: M15 = HTF POI engine) + M15
    Asia/prev-day/equal liquidity."""
    atr_m15 = wilder_atr_series(m15_candles, config_v2.ATR_PERIOD)
    tol = liq_mod.equal_level_tolerance(sym)
    lookback_bars_m15 = config_v2.EQUAL_LEVEL_LOOKBACK_DAYS * 24 * 4

    m15_eng_for_swings = StructureEngine(sym)  # only for its swing_log (equal-level detection); trend/BOS unused
    m15_equal_levels = []
    active_fvg_zones, active_ob_zones = [], []
    m15_snapshots = []
    for i in range(len(m15_candles)):
        if i >= 2:
            c1, c2, c3 = m15_candles[i-2], m15_candles[i-1], m15_candles[i]
            atr_at_c2 = atr_m15[i-2]
            new_fvg = fvg_mod.detect_fvg(sym, Timeframe.M15, c1, c2, c3, atr_at_c2)
            if new_fvg is not None:
                active_fvg_zones.append(new_fvg)
            new_ob = ob_mod.detect_order_block(sym, Timeframe.M15, m15_candles, i-2, c2, atr_at_c2)
            if new_ob is not None:
                active_ob_zones.append(new_ob[0])
        ev15 = m15_eng_for_swings.process_new_candle_close(m15_candles, i)
        for s in ev15.new_swings:
            eq = liq_mod.detect_equal_level(sym, m15_eng_for_swings.swing_log, s, tol, lookback_bars_m15)
            if eq is not None:
                m15_equal_levels.append(eq)
        for z in active_fvg_zones:
            fvg_mod.update_fvg_state(z, m15_candles[i])
        for z in active_ob_zones:
            ob_mod.update_ob_state(z, m15_candles[i])
        active_fvg_zones = [z for z in active_fvg_zones if fvg_mod.is_tradable(z)]
        active_ob_zones = [z for z in active_ob_zones if ob_mod.is_tradable(z)]
        m15_snapshots.append({
            "time": m15_candles[i].time,
            "fvg": [(z.gap_low, z.gap_high, z.direction) for z in active_fvg_zones],
            "ob": [(z.low, z.high, z.direction) for z in active_ob_zones],
        })
    m15_hour_to_idx = {s["time"]: i for i, s in enumerate(m15_snapshots)}
    asia_hi, asia_lo = liq_mod.detect_asia_levels(m15_candles, sym, timeframe=Timeframe.M15)
    prev_hi, prev_lo = liq_mod.detect_prev_day_levels(m15_candles, sym, timeframe=Timeframe.M15)
    return m15_snapshots, m15_hour_to_idx, asia_hi, asia_lo, prev_hi, prev_lo, m15_equal_levels


def run_backtest():
    trade_log = []
    open_positions = {}
    ticket_counter = [0]
    running_equity = [INITIAL_BALANCE]
    daily_realized_pnl = {}
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
        pip_size = config_v2.pip_size(symbol)
        pips = price_move_favorable / pip_size
        pip_value = TICK_VALUE_USD[symbol] * 10
        pnl = pips * pip_value * lots * close_frac
        running_equity[0] += pnl
        peak_equity[0] = max(peak_equity[0], running_equity[0])
        return pnl

    RAW_H1 = json.load(open(f"{DATA_DIR}/phase15_h1.json"))
    RAW_M15 = json.load(open(f"{DATA_DIR}/phase_entry_m15.json"))
    RAW_M1 = json.load(open(f"{DATA_DIR}/phase_entry_m1.json"))

    per_symbol_summary = {}

    for sym in SYMBOLS:
        h1_candles = to_candles(sym, RAW_H1["symbols"][sym]["rates"], Timeframe.H1)
        m15_candles = to_candles(sym, RAW_M15["symbols"][sym]["rates"], Timeframe.M15)
        m1_candles = to_candles(sym, RAW_M1["symbols"][sym]["rates"], Timeframe.M1)
        atr_m1 = wilder_atr_series(m1_candles, config_v2.ATR_PERIOD)
        tol = liq_mod.equal_level_tolerance(sym)

        (h1_snapshots, h1_hour_to_idx, h1_levels_by_idx, asia_h1_hi, asia_h1_lo,
         prev_h1_hi, prev_h1_lo, h1_equal_levels) = build_h1_state(sym, h1_candles)
        (m15_snapshots, m15_hour_to_idx, asia_m15_hi, asia_m15_lo,
         prev_m15_hi, prev_m15_lo, m15_equal_levels) = build_m15_state(sym, m15_candles)

        merged_equal_highs = liq_mod.merge_equal_levels(
            [l for l in h1_equal_levels if l.kind.value == "equal_high"],
            [l for l in m15_equal_levels if l.kind.value == "equal_high"], tol)
        merged_equal_lows = liq_mod.merge_equal_levels(
            [l for l in h1_equal_levels if l.kind.value == "equal_low"],
            [l for l in m15_equal_levels if l.kind.value == "equal_low"], tol)
        all_liquidity = (asia_h1_hi + asia_h1_lo + prev_h1_hi + prev_h1_lo +
                          asia_m15_hi + asia_m15_lo + prev_m15_hi + prev_m15_lo +
                          merged_equal_highs + merged_equal_lows)
        min_pen = liq_mod.min_sweep_penetration(sym)

        m1_time_to_idx_full = {c.time: i for i, c in enumerate(m1_candles)}
        m1_times_sorted = [c.time for c in m1_candles]

        def _nearest_m1_idx(t):
            idx = m1_time_to_idx_full.get(t)
            if idx is not None:
                return idx
            pos = bisect.bisect_left(m1_times_sorted, t)
            return pos if pos < len(m1_times_sorted) else None

        levels_by_created_idx = {}
        for lv in all_liquidity:
            idx = _nearest_m1_idx(lv.created_at)
            if idx is not None:
                levels_by_created_idx.setdefault(idx, []).append(lv)

        m1_mgr = POIManager(sym)
        m1_swings = []
        active_liquidity = []
        most_recent_buy_side_reversal = None
        most_recent_sell_side_reversal = None
        zone_touch_registry = {}
        m1_eng = StructureEngine(sym)  # only for local M1 swing/structure bookkeeping shared with trade_management's opposite-CHoCH exit

        n_signals = n_qualified = n_opened = n_closed = 0
        funnel = {
            "bars_active_pair": 0, "bars_matching_htf_zone": 0, "bars_sweep_available": 0,
            "bars_touched_favorable": 0, "zone_bar_candidates": 0, "m1_confirmed": 0,
            "entry_zone_found": 0, "signals": 0,
        }

        for j in range(len(m1_candles)):
            m1_candle = m1_candles[j]

            if j >= 2:
                c1, c2, c3 = m1_candles[j-2], m1_candles[j-1], m1_candles[j]
                atr_at_c2 = atr_m1[j-2]
                new_fvg_m1 = fvg_mod.detect_fvg(sym, Timeframe.M1, c1, c2, c3, atr_at_c2)
                if new_fvg_m1 is not None:
                    m1_mgr.add_fvg(new_fvg_m1)
                new_ob_m1 = ob_mod.detect_order_block(sym, Timeframe.M1, m1_candles, j-2, c2, atr_at_c2)
                if new_ob_m1 is not None:
                    m1_mgr.add_ob(new_ob_m1[0])
            m1_mgr.update(m1_candle)

            new_swings = swings_mod.confirmed_swings_at_close(sym, Timeframe.M1, m1_candles, j)
            m1_swings.extend(new_swings)

            active_liquidity.extend(levels_by_created_idx.get(j, []))
            for lv in active_liquidity:
                if lv.kind in liq_mod.BUY_SIDE_TYPES:
                    if m1_candle.high < lv.price:
                        continue
                else:
                    if m1_candle.low > lv.price:
                        continue
                was_swept = lv.swept
                liq_mod.update_sweep_state(lv, m1_candle, min_pen)
                if not was_swept and lv.swept and lv.reaction == "confirmed_reversal":
                    if liq_mod.is_sell_side(lv.kind):
                        most_recent_sell_side_reversal = lv
                    else:
                        most_recent_buy_side_reversal = lv
            if active_liquidity:
                active_liquidity = [lv for lv in active_liquidity if not lv.swept]

            m1_ev = m1_eng.process_new_candle_close(m1_candles, j)

            # --- Manage any open position for this symbol first ---
            if sym in open_positions:
                pos = open_positions[sym]
                ts = pos["state"]
                direction = ts.direction

                hit_sl = (m1_candle.low <= ts.current_sl) if direction == Direction.BULLISH else (m1_candle.high >= ts.current_sl)
                hit_tp = (m1_candle.high >= pos["take_profit"]) if direction == Direction.BULLISH else (m1_candle.low <= pos["take_profit"])

                if hit_sl:
                    exit_price = ts.current_sl
                    move = (exit_price - ts.entry_price) if direction == Direction.BULLISH else (ts.entry_price - exit_price)
                    pnl = realize_pnl(sym, move, pos["lots"], ts.remaining_pct)
                    outcome = "LOSS" if pnl < 0 else "WIN_PARTIAL"
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
                    actions = evaluate_trade(ts, m1_candle.close, sym, m1_candle.time, atr_m1[j-1] if j >= 1 else None)
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
                continue

            # --- No open position: evaluate for a new entry ---
            floored_h1_hour = m1_candle.time.replace(minute=0, second=0, microsecond=0)
            prev_h1_hour = floored_h1_hour - timedelta(hours=1)
            h1_idx = h1_hour_to_idx.get(prev_h1_hour)
            floored_m15 = m1_candle.time.replace(minute=(m1_candle.time.minute // 15) * 15, second=0, microsecond=0)
            prev_m15_slot = floored_m15 - timedelta(minutes=15)
            m15_idx = m15_hour_to_idx.get(prev_m15_slot)
            if h1_idx is None or m15_idx is None:
                continue
            pair = h1_snapshots[h1_idx]["pair"]
            if pair is None:
                continue
            direction = Direction.BULLISH if pair.trend == TrendState.BULLISH else Direction.BEARISH
            funnel["bars_active_pair"] += 1

            m15_snap = m15_snapshots[m15_idx]
            candidate_htf_zones = [(lo, hi, "fvg") for lo, hi, d in m15_snap["fvg"] if d == direction] + \
                                   [(lo, hi, "ob") for lo, hi, d in m15_snap["ob"] if d == direction]
            if not candidate_htf_zones:
                continue
            funnel["bars_matching_htf_zone"] += 1

            want_sell_side = direction == Direction.BULLISH
            sweep_level = most_recent_sell_side_reversal if want_sell_side else most_recent_buy_side_reversal
            if sweep_level is None:
                continue
            funnel["bars_sweep_available"] += 1

            for zone_low, zone_high, poi_kind in candidate_htf_zones:
                if poi_touched(m1_candle, zone_low, zone_high):
                    zone_touch_registry[(zone_low, zone_high)] = m1_candle.time

            matching_zones = []
            for zone_low, zone_high, poi_kind in candidate_htf_zones:
                touched_at = zone_touch_registry.get((zone_low, zone_high))
                if touched_at is None or not (timedelta(0) <= (m1_candle.time - touched_at) <= MAX_TOUCH_AGE):
                    continue
                if not poi_in_favorable_zone(pair, zone_low, zone_high):
                    continue
                matching_zones.append((zone_low, zone_high, poi_kind, touched_at))
            if not matching_zones or j < 3:
                continue
            funnel["bars_touched_favorable"] += 1

            # Full candidate lists -- compute_entry_zone (inside evaluate_
            # entry) selects the most recent one created AT OR AFTER the
            # confirmation swing, not pre-selected here (a stale, still-
            # "active" zone from long before the current reversal must not
            # be picked just because nothing mitigated it yet).
            active_m1_fvgs = [(z.gap_low, z.gap_high, z.created_at) for z in m1_mgr.active_fvgs() if z.direction == direction]
            active_m1_obs = [(z.low, z.high, z.created_at) for z in m1_mgr.active_obs() if z.direction == direction]
            last_3 = m1_candles[j-3:j]

            range_lo, range_hi = min(pair.low, pair.high), max(pair.low, pair.high)
            htf_swing_target = range_hi if direction == Direction.BULLISH else range_lo
            want_target_kind = "equal_high" if direction == Direction.BULLISH else "equal_low"
            eq_candidates = [lv for lv in all_liquidity if lv.kind.value == want_target_kind and not lv.swept]
            if direction == Direction.BULLISH:
                eq_candidates = [lv for lv in eq_candidates if lv.price > m1_candle.close]
                equal_level_target = min((lv.price for lv in eq_candidates), default=None)
            else:
                eq_candidates = [lv for lv in eq_candidates if lv.price < m1_candle.close]
                equal_level_target = max((lv.price for lv in eq_candidates), default=None)
            want_asia_kind = "asia_high" if direction == Direction.BULLISH else "asia_low"
            asia_candidates = [lv for lv in all_liquidity if lv.kind.value == want_asia_kind]
            opposite_asia_target = max(asia_candidates, key=lambda lv: lv.created_at).price if asia_candidates else None

            atr_prev = atr_m1[j-1] if j >= 1 else None
            for zone_low, zone_high, poi_kind, touched_at in matching_zones:
                funnel["zone_bar_candidates"] += 1
                # Instrumentation only -- these are the same pure, side-
                # effect-free calls evaluate_entry makes internally;
                # duplicating them here (cheap) lets the funnel report
                # show exactly where candidates are lost without altering
                # evaluate_entry's own gate sequence or behavior.
                conf_swing_probe = m1_confirmation(direction, m1_swings, m1_candle, atr_prev, last_3, zone_low, zone_high)
                if conf_swing_probe is not None:
                    funnel["m1_confirmed"] += 1
                    if compute_entry_zone(direction, pair, conf_swing_probe, active_m1_obs, active_m1_fvgs, atr_prev) is not None:
                        funnel["entry_zone_found"] += 1

                signal = evaluate_entry(
                    symbol=sym, direction=direction, pair=pair, sweep_level=sweep_level, poi_kind=poi_kind,
                    zone_low=zone_low, zone_high=zone_high, zone_touched_at=touched_at,
                    entry_candle=m1_candle, m1_swings=m1_swings, last_3_m1_candles=last_3,
                    m1_atr_at_prev=atr_prev, m1_obs=active_m1_obs, m1_fvgs=active_m1_fvgs,
                    htf_swing_target=htf_swing_target, equal_level_target=equal_level_target,
                    opposite_asia_target=opposite_asia_target,
                    max_sweep_age=MAX_SWEEP_AGE, max_touch_age=MAX_TOUCH_AGE,
                )
                if signal is None:
                    continue
                n_signals += 1
                funnel["signals"] += 1

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
                pos_state = TradeState(direction=direction, entry_price=signal.entry_price, initial_risk=sl_distance,
                                        current_sl=signal.stop_loss, opened_at=m1_candle.time)
                open_positions[sym] = {"state": pos_state, "lots": lots, "ticket": ticket_counter[0],
                                        "take_profit": signal.take_profit, "risk_amount": risk_amount,
                                        "risk_pct": risk_pct}
                break

        per_symbol_summary[sym] = {"signals": n_signals, "qualified": n_qualified, "opened": n_opened,
                                    "closed": n_closed, "funnel": funnel}
        print(f"{sym}: M1 bars={len(m1_candles)} signals={n_signals} qualified={n_qualified} opened={n_opened} closed={n_closed}", flush=True)
        print(f"    funnel: {funnel}", flush=True)

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
