"""Ablation study for smc_3step's OWN architecture -- a different question
from the regime-gate work (which asked "when should we apply this given
strategy"). This asks: which of the strategy's three internal design
choices actually carry edge (raise win rate / trade quality), vs which
just cut trade frequency by waiting for a stricter setup without making
the ones it does take any better?

Does NOT touch src/application/smc_3step.py or the live runner -- reuses
its unchanged building-block functions (find_order_block,
find_fair_value_gap, check_choch, build_signal, etc.) under configurable
toggles, in a separate EA class here.

Three axes:
  trend_mode: "both" (current: H1 EMA-slope AND swing-structure must
              agree) | "ema_only" | "structure_only"
  zone_mode:  "both" (current: Order Block tried first, FVG fallback)
              | "ob_only" | "fvg_only"
  choch_mode: "wait" (current: M5 CHoCH confirmation required before
              entry) | "immediate" (enter as soon as a valid pullback
              zone is found, no confirmation wait)

Same cost model/constants as tools/backtest_smc_3step_fresh.py (measured
spread, real commission) so results are directly comparable to the
already-validated ER-gated baseline.

HOW TO RUN: python3 tools/smc_3step_ablation.py
"""
import sys, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, ".")

from src.domain.entities import Candle, Timeframe
from src.application.strategy_v2 import config_v2
from src.application.smc_3step import (
    ema_series, find_fractal_swings, find_order_block, find_fair_value_gap,
    find_pullback_zone, is_zone_invalidated, check_choch, calculate_lot_size,
    build_signal, ThreeStepConfig, EAState,
)

SYM = "XAUUSD"
TICK_VALUE_USD = 10.0
COMMISSION_PER_LOT_ROUNDTRIP = 7.00
SPREAD_POINTS, SLIPPAGE_POINTS = 6.0, 5.0
MIN_LOT, MAX_LOT, LOT_STEP = 0.01, 10.0, 0.01
INITIAL_BALANCE = 10000.0
DATA = "logs/smc3step_backtest_data.json"


def to_candles(rows, tf):
    return [Candle(time=datetime.fromtimestamp(r["time"], tz=timezone.utc), open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"], volume=float(r["tick_volume"]), symbol=SYM, timeframe=tf)
            for r in rows]


def get_trend_variant(h1_candles, up_to_index, ema_values, slope_bars, swing_lookback, wing, trend_mode):
    if up_to_index >= len(ema_values) or up_to_index - slope_bars < 0:
        return 0
    ema_now = ema_values[up_to_index]
    ema_past = ema_values[up_to_index - slope_bars]
    if ema_now is None or ema_past is None:
        return 0
    ema_up, ema_down = ema_now > ema_past, ema_now < ema_past

    if trend_mode == "ema_only":
        return 1 if ema_up else (-1 if ema_down else 0)

    highs, lows = find_fractal_swings(h1_candles, wing, swing_lookback, up_to_index)
    if not highs or not lows:
        return 0
    structure_up = highs[1].price > highs[0].price and lows[1].price > lows[0].price
    structure_down = highs[1].price < highs[0].price and lows[1].price < lows[0].price

    if trend_mode == "structure_only":
        return 1 if structure_up else (-1 if structure_down else 0)

    if ema_up and structure_up:
        return 1
    if ema_down and structure_down:
        return -1
    return 0


def find_zone_variant(m15_candles, up_to_index, trend, lookback, impulse_factor, current_price, zone_mode):
    if zone_mode == "ob_only":
        return find_order_block(m15_candles, up_to_index, trend, lookback, impulse_factor, current_price)
    if zone_mode == "fvg_only":
        return find_fair_value_gap(m15_candles, up_to_index, trend, lookback, current_price)
    return find_pullback_zone(m15_candles, up_to_index, trend, lookback, impulse_factor, current_price)


class AblationEA:
    def __init__(self, cfg, symbol, trend_mode="both", zone_mode="both", choch_mode="wait"):
        self.cfg, self.symbol = cfg, symbol
        self.trend_mode, self.zone_mode, self.choch_mode = trend_mode, zone_mode, choch_mode
        self.state = EAState.WAIT_SETUP
        self.zone = None
        self.zone_wait_bars = 0

    def on_m5_bar(self, h1_candles, h1_idx, h1_ema, m15_candles, m15_idx, m5_candles, m5_idx,
                  current_price_bid, current_price_ask):
        cfg = self.cfg
        trend = get_trend_variant(h1_candles, h1_idx, h1_ema, cfg.ema_slope_bars,
                                   cfg.swing_lookback_h1, cfg.fractal_wing, self.trend_mode)
        if trend == 0:
            self.state = EAState.WAIT_SETUP
            return None

        if self.state == EAState.WAIT_SETUP:
            price = current_price_ask if trend == 1 else current_price_bid
            z = find_zone_variant(m15_candles, m15_idx, trend, cfg.zone_lookback_m15,
                                   cfg.impulse_factor, price, self.zone_mode)
            if z is not None:
                age_bars = m15_idx - z.form_index
                if 0 <= age_bars <= cfg.zone_max_age_bars:
                    if self.choch_mode == "immediate":
                        return build_signal(cfg, z, self.symbol, current_price_bid, current_price_ask)
                    self.zone = z
                    self.state = EAState.WAIT_CHOCH
                    self.zone_wait_bars = 0
            return None

        # STATE_WAIT_CHOCH (only reachable when choch_mode == "wait")
        self.zone_wait_bars += 1
        if (trend != self.zone.direction
                or is_zone_invalidated(self.zone, m15_candles, m15_idx)
                or self.zone_wait_bars > cfg.zone_timeout_m5_bars):
            self.state = EAState.WAIT_SETUP
            self.zone = None
            return None

        level = check_choch(m5_candles, m5_idx, self.zone.direction, cfg.choch_lookback_m5, cfg.fractal_wing)
        if level is not None:
            signal = build_signal(cfg, self.zone, self.symbol, current_price_bid, current_price_ask)
            self.state = EAState.WAIT_SETUP
            self.zone = None
            return signal
        return None


def run_ablation(h1_candles, m15_candles, m5_candles, trend_mode, zone_mode, choch_mode, label,
                  regime_gate=None, h1_er=None):
    cfg = ThreeStepConfig()
    h1_ema = ema_series([c.close for c in h1_candles], cfg.ema_period)
    h1_time_to_idx = {c.time: i for i, c in enumerate(h1_candles)}
    m15_time_to_idx = {c.time: i for i, c in enumerate(m15_candles)}
    point, pip = config_v2.point_size(SYM), config_v2.pip_size(SYM)

    ea = AblationEA(cfg, SYM, trend_mode, zone_mode, choch_mode)
    open_pos, equity, trade_log = None, [INITIAL_BALANCE], []
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
                slip = SLIPPAGE_POINTS * point
                exit_price = exit_price - slip if open_pos["direction"] == 1 else exit_price + slip
                move = (exit_price - open_pos["entry"]) if open_pos["direction"] == 1 else (open_pos["entry"] - exit_price)
                pnl = (move / pip) * (TICK_VALUE_USD * 10) * open_pos["lots"] - COMMISSION_PER_LOT_ROUNDTRIP * open_pos["lots"]
                equity[0] += pnl
                initial_risk = abs(open_pos["entry"] - open_pos["sl"])
                trade_log.append({
                    "direction": "bullish" if open_pos["direction"] == 1 else "bearish", "pnl": round(pnl, 2),
                    "outcome": "WIN" if hit_tp else "LOSS", "opened_at": open_pos["opened_at"].isoformat(),
                    "mfe_r": round(open_pos["mfe"] / initial_risk, 3) if initial_risk > 0 else None,
                    "mae_r": round(open_pos["mae"] / initial_risk, 3) if initial_risk > 0 else None,
                })
                open_pos = None
            continue

        floored_h1_hour = m5_candle.time.replace(minute=0, second=0, microsecond=0)
        h1_idx = h1_time_to_idx.get(floored_h1_hour - timedelta(hours=1))
        floored_m15 = m5_candle.time.replace(minute=(m5_candle.time.minute // 15) * 15, second=0, microsecond=0)
        m15_idx = m15_time_to_idx.get(floored_m15 - timedelta(minutes=15))
        if h1_idx is None or m15_idx is None or j < 3:
            continue
        if h1_idx < cfg.ema_period or h1_idx < cfg.swing_lookback_h1:
            continue

        half_spread = SPREAD_POINTS * point / 2.0
        bid, ask = m5_candle.close - half_spread, m5_candle.close + half_spread
        signal = ea.on_m5_bar(h1_candles, h1_idx, h1_ema, m15_candles, m15_idx, m5_candles, j,
                               current_price_bid=bid, current_price_ask=ask)
        if signal is None:
            continue
        n_signals += 1

        if signal.direction == 1:
            valid_geometry = signal.stop_loss < signal.entry_price < signal.take_profit
        else:
            valid_geometry = signal.take_profit < signal.entry_price < signal.stop_loss
        if not valid_geometry:
            continue

        if regime_gate is not None:
            er_val = h1_er[h1_idx] if h1_er is not None else None
            if not regime_gate(er_val):
                continue

        slip = SLIPPAGE_POINTS * point
        signal.entry_price += slip if signal.direction == 1 else -slip
        sl_distance = abs(signal.entry_price - signal.stop_loss)
        lots = calculate_lot_size(balance=equity[0], risk_percent=cfg.risk_percent, sl_distance=sl_distance,
                                   tick_value=TICK_VALUE_USD * 10, tick_size=pip,
                                   min_lot=MIN_LOT, max_lot=MAX_LOT, lot_step=LOT_STEP)
        if lots <= 0:
            continue
        n_opened += 1
        open_pos = {"direction": signal.direction, "entry": signal.entry_price, "sl": signal.stop_loss,
                    "tp": signal.take_profit, "lots": lots, "opened_at": m5_candle.time, "mfe": 0.0, "mae": 0.0}

    n_wins = sum(1 for t in trade_log if t["outcome"] == "WIN")
    total_pnl = round(sum(t["pnl"] for t in trade_log), 2)
    gains = sum(t["pnl"] for t in trade_log if t["pnl"] > 0)
    loss_sum = abs(sum(t["pnl"] for t in trade_log if t["pnl"] < 0))
    pf = round(gains / loss_sum, 2) if loss_sum > 0 else None
    win_rate = round(100.0 * n_wins / len(trade_log), 1) if trade_log else None
    exp = round(total_pnl / len(trade_log), 2) if trade_log else None
    print(f"[{label}] signals={n_signals} opened={n_opened} closed={len(trade_log)} "
          f"win_rate={win_rate}% pnl=${total_pnl} PF={pf} exp=${exp}", flush=True)
    return {"label": label, "signals": n_signals, "opened": n_opened, "closed": len(trade_log),
            "wins": n_wins, "win_rate_pct": win_rate, "total_pnl": total_pnl, "profit_factor": pf,
            "expectancy": exp, "trade_log": trade_log}


def main():
    raw = json.load(open(DATA))["symbols"][SYM]
    h1_candles = to_candles(raw["h1"], Timeframe.H1)
    m15_candles = to_candles(raw["m15"], Timeframe.M15)
    m5_candles = to_candles(raw["m5"], Timeframe.M5)
    half = len(m5_candles) // 2
    m5_half1, m5_half2 = m5_candles[:half], m5_candles[half:]

    results = {}
    for trend_mode in ["both", "ema_only", "structure_only"]:
        for zone_mode in ["both", "ob_only", "fvg_only"]:
            for choch_mode in ["wait", "immediate"]:
                label = f"trend={trend_mode} zone={zone_mode} choch={choch_mode}"
                full = run_ablation(h1_candles, m15_candles, m5_candles, trend_mode, zone_mode, choch_mode, f"{label} FULL")
                h1r = run_ablation(h1_candles, m15_candles, m5_half1, trend_mode, zone_mode, choch_mode, f"{label} H1")
                h2r = run_ablation(h1_candles, m15_candles, m5_half2, trend_mode, zone_mode, choch_mode, f"{label} H2")
                results[label] = {"full": full, "half1": h1r, "half2": h2r}
                print()

    out_path = "/tmp/claude-1000/-home-josewathome-project-claude-session/da8709d8-6731-49ae-8e32-f7a073ed2150/scratchpad/ablation_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
