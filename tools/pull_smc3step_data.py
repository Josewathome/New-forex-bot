import MetaTrader5 as mt5, json, sys, datetime

SYM = "XAUUSD"
if not mt5.initialize():
    print("init failed", mt5.last_error()); sys.exit(1)

def pull(tf_name, tf_const, count):
    rates = mt5.copy_rates_from_pos(SYM, tf_const, 0, count)
    if rates is None or len(rates) == 0:
        print(tf_name, "FAILED", mt5.last_error()); return None
    rows = [
        {"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"]), "tick_volume": int(r["tick_volume"]),
         "spread": int(r["spread"])}
        for r in rates
    ]
    t0 = datetime.datetime.utcfromtimestamp(rows[0]["time"])
    t1 = datetime.datetime.utcfromtimestamp(rows[-1]["time"])
    print(tf_name, "count=", len(rows), "range:", t0, "->", t1)
    return rows

h1 = pull("H1", mt5.TIMEFRAME_H1, 30000)     # >3yr of H1, plenty of warmup margin
m15 = pull("M15", mt5.TIMEFRAME_M15, 105000) # ~3yr of M15
m5 = pull("M5", mt5.TIMEFRAME_M5, 200000)    # broker's full M5 depth (~2.8yr)

if m5:
    spreads = [r["spread"] for r in m5 if r["spread"] > 0]
    if spreads:
        spreads_sorted = sorted(spreads)
        n = len(spreads_sorted)
        print("M5 spread (points) over %d bars: min=%d p25=%d median=%d mean=%.1f p75=%d p95=%d max=%d" % (
            n, spreads_sorted[0], spreads_sorted[n//4], spreads_sorted[n//2],
            sum(spreads_sorted)/n, spreads_sorted[3*n//4], spreads_sorted[int(n*0.95)], spreads_sorted[-1]))

# also grab a live tick sample to measure REAL current spread (replacing the
# backtest's flagged-assumption 20-point estimate with a measured one)
ticks = []
info = mt5.symbol_info(SYM)
point = info.point if info else None
tick = mt5.symbol_info_tick(SYM)
if tick:
    spread_points = round((tick.ask - tick.bid) / point) if point else None
    print("LIVE TICK spread: bid=%.2f ask=%.2f spread_points=%s point=%s" % (tick.bid, tick.ask, spread_points, point))

out = {"symbols": {SYM: {"h1": h1, "m15": m15, "m5": m5}}}
with open("/bot/logs/smc3step_backtest_data.json", "w") as f:
    json.dump(out, f)
print("wrote /bot/logs/smc3step_backtest_data.json")
mt5.shutdown()
