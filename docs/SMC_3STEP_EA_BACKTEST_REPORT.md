# SmartMoney_3Step_EA — Python Port & Real-Data Backtest Report

Port of the user-supplied `SmartMoney_3Step_EA.mq5` to
`src/application/smc_3step.py` (33/33 unit assertions pass against
hand-built fixtures, `tools/validate_smc_3step.py`), backtested against
real market data for XAUUSD/EURUSD/GBPUSD/USDJPY via
`tools/backtest_smc_3step.py`.

## What this strategy is (for contrast with the V4/strategy_v2 effort)

A different, simpler SMC variant from the V4 spec this session spent most
of its time on: H1 EMA(50)-slope + swing-structure trend → pullback into
an M15 Order Block or FVG (bounded to `InpZoneMaxAgeBars`=60 M15 bars,
~15 hours old) → M5 Change-of-Character entry, fixed RR=2. No liquidity
sweep requirement, no Absorption gate, no RR≥5, no priority-fallback TP
— structurally far less restrictive than V4, and (notably) its
zone-selection **requires current price to be inside the candidate
zone** and bounds zone age, which sidesteps the exact "stale, far-away
zone" defect this session's V4 spec audit found
(`docs/STRATEGY_V4_SPEC_CONTRADICTION_AUDIT.md`).

## Data & method

- H1 (`logs/phase15_h1.json`, ~13mo), M15 (`logs/phase_entry_m15.json`,
  ~20mo) — same real pulls used throughout this session.
- M5 was **not** separately pulled; resampled from the same M1 data
  (`logs/phase_entry_m1.json`, ~10mo, 300k bars/symbol) already on disk.
  The backtest window is therefore capped at the ~10-month M1/M5
  overlap — the same binding constraint as every other backtest this
  session ran.
- Fill model: SL/TP touches checked against **M5 bar high/low** (the
  EA's native timeframe), not M1 intrabar precision — a documented
  simplification (see the script's own docstring), not a silent one.
- Position sizing/PnL settlement reuses this repo's existing `TICK_
  VALUE_USD`/pip-value convention (`tick_value × 10 = $ per pip per
  lot`), the same formula `tools/backtest_strategy_v2.py`'s
  `realize_pnl` already uses — kept identical so "risk 1% per trade"
  means the same thing in both backtests.
- **No commission and no real spread modeled.** The bid/ask split used
  during signal evaluation is a synthetic 1-point floor (needed only to
  distinguish "is price inside this zone" for buy vs sell direction),
  not a transaction cost. Real spread, slippage, and commission would
  make every result below strictly worse — this is a best-case, not a
  realistic, cost basis.

## One real bug caught during porting (not by inspection)

`find_fair_value_gap`'s 3-candle triplet index arithmetic was initially
copied directly from the MQL5 source's reverse-series indexing (index 0
= most recent) without flipping direction for this module's forward-
chronological convention — silently computing the "oldest" candle of
the triplet as 2 bars *after* the newest instead of 2 bars *before* it.
Caught by the unit test for a hand-built bullish FVG returning `None`
when it should have matched; fixed and covered by a dedicated regression
case (`tools/validate_smc_3step.py` section 5).

## Result: 932 real trades, aggregate roughly breakeven, but concentrated entirely in one symbol

| Symbol | Trades | Win rate | Total PnL | Profit Factor | Expectancy/trade |
|---|---|---|---|---|---|
| XAUUSD | 310 | 42.6% | **+$19,296.30** | **1.48** | **+$62.25** |
| EURUSD | 185 | 33.0% | −$1,102.10 | 0.97 | −$5.96 |
| GBPUSD | 183 | 29.5% | −$5,614.20 | 0.83 | −$30.68 |
| USDJPY | 254 | 26.0% | −$9,346.82 | 0.71 | −$36.80 |
| **Blended** | **932** | **33.6%** | **+$3,233.18** | **1.02** | **+$3.47** |

**The blended, all-symbols number (PF 1.02, marginally profitable) is
misleading on its own.** It is not "a small, uniform edge across 4
pairs" — it is one strongly profitable symbol (XAUUSD, PF 1.48) offsetting
three money-losing ones (EURUSD/GBPUSD/USDJPY, PF 0.71–0.97), which
happen to roughly net out over this specific ~10-month window. A
portfolio that trades all 4 pairs using this exact configuration would
have been given back most of its XAUUSD gains by its FX losses; a
portfolio trading XAUUSD alone would show a real, credible edge in this
sample, and one trading only the 3 FX pairs would show a real, credible
loss.

This is exactly the failure mode the strategy_v2 work spent this session
guarding against (aggregating across symbols/regimes can hide that an
"edge" is really concentrated in one favorable regime) — reported
plainly rather than papered over by the flattering blended total.

## What has NOT been checked (explicitly out of scope for this pass)

- **No walk-forward, no Monte Carlo reshuffle, no bootstrap confidence
  interval, no spread/slippage sensitivity sweep** — the same battery
  applied to the SMC audits earlier in this session. 932 trades (and
  183–310 per symbol) is enough for a preliminary read, not enough to
  call this result statistically settled, especially per-symbol.
- **No commission/spread cost applied.** Given EURUSD/GBPUSD/USDJPY are
  already net-negative with zero costs, real trading costs would only
  make them worse. XAUUSD's positive result would also shrink once
  real costs are applied, by an amount that hasn't been estimated here.
- **AUDUSD and any 5th symbol**: not tested (only the 4 named in this
  turn's request).
- This is a single, un-optimized parameter set taken directly from the
  EA's own input defaults — no tuning was performed or should be
  inferred from these numbers being "the strategy's best case."

## Bottom line

This EA is dramatically more willing to trade than the V4 spec's build
(932 real signals vs. V4's 0, over a comparable window) — its bounded
zone-age and price-must-be-inside-the-zone selection avoid V4's core
defect. But "produces trades" and "has a proven edge across the
requested 4 pairs" are different claims: on this data, the aggregate
result is driven almost entirely by XAUUSD, and the strategy as
configured is a net loser on EURUSD, GBPUSD, and USDJPY individually. If
XAUUSD's edge is the interesting result, it deserves its own
walk-forward/cost-sensitivity pass before being trusted; the FX-pair
losses should not be masked by blending them with gold's gains.
