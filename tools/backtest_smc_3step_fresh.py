"""Real-data backtest for smc_3step (src/application/smc_3step.py) using
FRESH, NATIVE M5 candles pulled directly from this account's MT5 broker
(logs/smc3step_backtest_data.json — H1/M15/M5 XAUUSD, 2023-10 to present,
~2.8yr, the broker's full M5 history depth). Unlike tools/backtest_
smc_3step.py, this does NOT resample M5 from M1 (removing that
simplification) and does NOT invent a spread assumption -- SPREAD_POINTS
is derived from the real historical `spread` field MT5 stores on every M5
bar for this exact symbol/account (median/p95 reported by the puller,
see logs/smc3step_backtest_data.json's sibling pull script), not a guess.

Reuses ThreeStepEA/calculate_lot_size (signal-generation and lot-sizing
logic, unchanged) and the exact settlement/commission formula from tools/
backtest_smc_3step.py (kept byte-for-byte identical so results are
comparable) -- only the data source and the spread constant differ.

Adds --split N: chops the chronological candle range into N equal-length
segments and runs each independently, to check whether XAUUSD's edge (if
any) is stable across the sample or concentrated in one regime -- the
walk-forward/stability check the July report explicitly flagged as
un-done.

HOW TO RUN (from repo root, plain python3, no MT5/Wine needed):
    python3 tools/backtest_smc_3step_fresh.py --spread 6
    python3 tools/backtest_smc_3step_fresh.py --spread 6 --split 4
"""
import sys, json, argparse
from datetime import datetime, timezone, timedelta
sys.path.insert(0, ".")

from src.domain.entities import Candle, Timeframe
from src.application.strategy_v2 import config_v2
from src.application.smc_3step import ema_series, ThreeStepEA, ThreeStepConfig, calculate_lot_size
from tools.regime_metrics import adx_series, efficiency_ratio_series

SYM = "XAUUSD"
TICK_VALUE_USD = 10.0
COMMISSION_PER_LOT_ROUNDTRIP = 7.00
SLIPPAGE_POINTS_DEFAULT = 5
MIN_LOT, MAX_LOT, LOT_STEP = 0.01, 10.0, 0.01
INITIAL_BALANCE = 10000.0


def to_candles(rows, tf):
    return [Candle(time=datetime.fromtimestamp(r["time"], tz=timezone.utc), open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"], volume=float(r["tick_volume"]), symbol=SYM, timeframe=tf)
            for r in rows]


def run_segment(h1_candles, m15_candles, m5_candles, spread_points, slippage_points, apply_costs, label,
                 regime_gate=None, h1_adx=None, h1_er=None):
    """regime_gate: optional callable(adx_value, er_value) -> bool. Blocks
    only the FINAL entry action when it returns False (mirrors the
    session-hour filter in tools/backtest_smc_3step.py) -- the EA's own
    zone-acquisition/CHoCH-wait state machine still runs every bar
    regardless, so a rejected signal doesn't freeze/reset the state
    machine, it just doesn't get acted on. h1_adx/h1_er: precomputed
    series (same length as h1_candles) -- pass in when calling this
    across many segments/gates so ADX/ER aren't recomputed every time
    (they're O(n), cheap, but no reason to repeat it)."""
    cfg = ThreeStepConfig()
    h1_ema = ema_series([c.close for c in h1_candles], cfg.ema_period)
    h1_time_to_idx = {c.time: i for i, c in enumerate(h1_candles)}
    m15_time_to_idx = {c.time: i for i, c in enumerate(m15_candles)}
    point = config_v2.point_size(SYM)
    pip = config_v2.pip_size(SYM)
    if h1_adx is None:
        h1_adx = adx_series(h1_candles, 14)
    if h1_er is None:
        h1_er = efficiency_ratio_series(h1_candles, 20)

    ea = ThreeStepEA(cfg, SYM)
    open_pos = None
    equity = [INITIAL_BALANCE]
    trade_log = []
    n_signals = n_opened = 0

    for j, m5_candle in enumerate(m5_candles):
        if open_pos is not None:
            if open_pos["direction"] == 1:
                fav = m5_candle.high - open_pos["entry"]; adv = open_pos["entry"] - m5_candle.low
            else:
                fav = open_pos["entry"] - m5_candle.low; adv = m5_candle.high - open_pos["entry"]
            open_pos["mfe"] = max(open_pos["mfe"], fav)
            open_pos["mae"] = max(open_pos["mae"], adv)

            hit_sl = (m5_candle.low <= open_pos["sl"]) if open_pos["direction"] == 1 else (m5_candle.high >= open_pos["sl"])
            hit_tp = (m5_candle.high >= open_pos["tp"]) if open_pos["direction"] == 1 else (m5_candle.low <= open_pos["tp"])
            if hit_sl or hit_tp:
                exit_price = open_pos["sl"] if hit_sl else open_pos["tp"]
                if apply_costs:
                    slip = slippage_points * point
                    exit_price = exit_price - slip if open_pos["direction"] == 1 else exit_price + slip
                move = (exit_price - open_pos["entry"]) if open_pos["direction"] == 1 else (open_pos["entry"] - exit_price)
                pips_moved = move / pip
                pip_value = TICK_VALUE_USD * 10
                pnl = pips_moved * pip_value * open_pos["lots"]
                if apply_costs:
                    pnl -= COMMISSION_PER_LOT_ROUNDTRIP * open_pos["lots"]
                equity[0] += pnl
                initial_risk = abs(open_pos["entry"] - open_pos["sl"])
                trade_log.append({
                    "direction": "bullish" if open_pos["direction"] == 1 else "bearish",
                    "entry": open_pos["entry"], "sl": open_pos["sl"], "tp": open_pos["tp"],
                    "lots": open_pos["lots"], "pnl": round(pnl, 2),
                    "outcome": "WIN" if hit_tp else "LOSS",
                    "opened_at": open_pos["opened_at"].isoformat(), "closed_at": m5_candle.time.isoformat(),
                    "mfe_r": round(open_pos["mfe"] / initial_risk, 3) if initial_risk > 0 else None,
                    "mae_r": round(open_pos["mae"] / initial_risk, 3) if initial_risk > 0 else None,
                    "zone_is_ob": open_pos["zone_is_ob"],
                    "adx_at_signal": open_pos["adx_at_signal"], "er_at_signal": open_pos["er_at_signal"],
                })
                open_pos = None
            continue

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

        if apply_costs:
            half_spread = spread_points * point / 2.0
            bid = m5_candle.close - half_spread
            ask = m5_candle.close + half_spread
        else:
            bid = m5_candle.close
            ask = m5_candle.close + 1 * point

        signal = ea.on_m5_bar(h1_candles, h1_idx, h1_ema, m15_candles, m15_idx,
                               m5_candles, j, current_price_bid=bid, current_price_ask=ask)
        if signal is None:
            continue
        n_signals += 1
        adx_val = h1_adx[h1_idx]
        er_val = h1_er[h1_idx]

        # Regime gate: blocks only this signal's entry action, mirroring the
        # session-hour filter's convention -- the EA's own state machine
        # already advanced this bar regardless (on_m5_bar was called above),
        # so a rejected signal doesn't distort zone-tracking/CHoCH-wait state.
        if regime_gate is not None and not regime_gate(adx_val, er_val):
            continue

        if apply_costs:
            slip = slippage_points * point
            if signal.direction == 1:
                signal.entry_price += slip
            else:
                signal.entry_price -= slip

        sl_distance = abs(signal.entry_price - signal.stop_loss)
        lots = calculate_lot_size(
            balance=equity[0], risk_percent=cfg.risk_percent, sl_distance=sl_distance,
            tick_value=TICK_VALUE_USD * 10, tick_size=pip,
            min_lot=MIN_LOT, max_lot=MAX_LOT, lot_step=LOT_STEP,
        )
        if lots <= 0:
            continue
        n_opened += 1
        open_pos = {"direction": signal.direction, "entry": signal.entry_price, "sl": signal.stop_loss,
                    "tp": signal.take_profit, "lots": lots, "opened_at": m5_candle.time,
                    "mfe": 0.0, "mae": 0.0, "zone_is_ob": signal.zone.is_ob,
                    "adx_at_signal": round(adx_val, 2) if adx_val is not None else None,
                    "er_at_signal": round(er_val, 4) if er_val is not None else None}

    n_wins = sum(1 for t in trade_log if t["outcome"] == "WIN")
    n_losses = len(trade_log) - n_wins
    total_pnl = round(sum(t["pnl"] for t in trade_log), 2)
    gains = sum(t["pnl"] for t in trade_log if t["pnl"] > 0)
    loss_sum = abs(sum(t["pnl"] for t in trade_log if t["pnl"] < 0))
    pf = round(gains / loss_sum, 2) if loss_sum > 0 else None
    win_rate = round(100.0 * n_wins / len(trade_log), 1) if trade_log else None
    expectancy = round(total_pnl / len(trade_log), 2) if trade_log else None
    print(f"[{label}] range={m5_candles[0].time.date()}..{m5_candles[-1].time.date()} "
          f"m5_bars={len(m5_candles)} signals={n_signals} opened={n_opened} closed={len(trade_log)} "
          f"wins={n_wins} losses={n_losses} win_rate={win_rate}% pnl=${total_pnl} PF={pf} exp=${expectancy}",
          flush=True)
    return {"label": label, "start": str(m5_candles[0].time.date()), "end": str(m5_candles[-1].time.date()),
            "m5_bars": len(m5_candles), "signals": n_signals, "opened": n_opened, "closed": len(trade_log),
            "wins": n_wins, "losses": n_losses, "win_rate_pct": win_rate, "total_pnl": total_pnl,
            "profit_factor": pf, "expectancy": expectancy, "final_equity": round(equity[0], 2),
            "trade_log": trade_log}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="logs/smc3step_backtest_data.json")
    parser.add_argument("--spread", type=float, default=6.0, help="one-way spread in points (measured median≈5-6)")
    parser.add_argument("--slippage", type=float, default=SLIPPAGE_POINTS_DEFAULT, help="per-fill slippage in points")
    parser.add_argument("--no-costs", action="store_true")
    parser.add_argument("--split", type=int, default=1, help="chop the range into N sequential segments")
    parser.add_argument("--er-gate", type=float, default=None,
                         help="only take a signal when H1 Efficiency Ratio(20) >= this value at signal time")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    raw = json.load(open(args.data))[
        "symbols"][SYM]
    h1_candles = to_candles(raw["h1"], Timeframe.H1)
    m15_candles = to_candles(raw["m15"], Timeframe.M15)
    m5_candles = to_candles(raw["m5"], Timeframe.M5)

    regime_gate = None
    if args.er_gate is not None:
        thresh = args.er_gate
        regime_gate = lambda adx_val, er_val: er_val is not None and er_val >= thresh
    h1_adx = adx_series(h1_candles, 14)
    h1_er = efficiency_ratio_series(h1_candles, 20)

    apply_costs = not args.no_costs
    results = []
    if args.split <= 1:
        results.append(run_segment(h1_candles, m15_candles, m5_candles, args.spread, args.slippage,
                                    apply_costs, "FULL", regime_gate=regime_gate, h1_adx=h1_adx, h1_er=h1_er))
    else:
        n = len(m5_candles)
        seg_len = n // args.split
        for i in range(args.split):
            start = i * seg_len
            end = n if i == args.split - 1 else (i + 1) * seg_len
            seg_m5 = m5_candles[start:end]
            if not seg_m5:
                continue
            # H1/M15 candles are shared/full-range (the lookup dicts key by
            # timestamp, so passing the full arrays is correct and required --
            # the EA needs H1 history from BEFORE this segment's first M5 bar
            # for EMA/swing warmup; slicing them would starve early bars).
            results.append(run_segment(h1_candles, m15_candles, seg_m5, args.spread, args.slippage,
                                        apply_costs, f"SEG{i+1}/{args.split}",
                                        regime_gate=regime_gate, h1_adx=h1_adx, h1_er=h1_er))

    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
