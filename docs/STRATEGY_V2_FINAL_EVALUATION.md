# strategy_v2 — Final Evaluation

Complete-system evaluation after all 9 modules (Liquidity Sweep,
Premium/Discount, POI Manager, Entry Confirmation, Trade Decision Engine,
Risk Management, MT5 Execution, Trade Management, Monitoring) were built,
individually validated, and wired together (`tools/backtest_strategy_v2.py`)
per the "finish the complete system, then evaluate once" mandate.

## Bot assembly — confirmed working

The backtester wires every module together exactly as designed: H1
Structure/FVG/OB/Liquidity → POI Manager (with CHoCH cleanup) →
Premium/Discount classification → M1 local structure + Entry Confirmation
→ Trade Decision Engine → Risk Management (position sizing) → an
intrabar SL/TP fill simulation driven by Trade Management's exit ladder →
Monitoring. It ran to completion across the full real dataset (4 symbols
× ~300,000 M1 bars = 1.2M bar-iterations) with zero crashes and zero
invariant violations. A forced smoke-test trade (bypassing qualification,
opened directly on real EURUSD M1 data) confirmed the open/manage/close/
PnL-realization glue code is arithmetically correct end-to-end: it closed
at exactly −$100 on a $100 risk allocation — precisely −1R, as a full
stop-loss hit before any breakeven adjustment must produce. This is the
one piece of the assembled system that individual-module testing could
not exercise (no real trade ever opened during the real backtest itself,
per the result below), so it was verified separately here.

## Data constraints (reported, not worked around)

- **M1 data availability is the binding constraint**: ~10 months
  (2025-09-11 to 2026-07-17), vs. H1's 13 months. Entry Confirmation
  requires M1 candles, so the backtest window is capped at ~10 months —
  **this is not a multi-year backtest.**
- Only 4 symbols have real data pulled (XAUUSD, EURUSD, GBPUSD, USDJPY).
  AUDUSD (part of the original 5-symbol universe) and D1 bias data were
  never pulled — Phase 1 from the original plan remains incomplete for
  those.
- **No historical economic-calendar/news data source is available**
  in this sandbox (no internet access). The backtest's news gate always
  returns "not blocked" — an untested gate, not one proven harmless.

## Result: zero trades

Across the complete real ~10-month window, all 4 symbols, the fully
wired system produced:

| Symbol | Signals (Entry Confirmation) | Qualified (Trade Decision) | Opened | Closed |
|---|---|---|---|---|
| XAUUSD | 0 | 0 | 0 | 0 |
| EURUSD | 0 | 0 | 0 | 0 |
| GBPUSD | 0 | 0 | 0 | 0 |
| USDJPY | 1 | 0 | 0 | 0 |

**One raw signal in 1.2M M1 bar-closes across 4 major pairs over ~10
months — and it was rejected.** The signal (USDJPY, 2026-06-16 16:31 UTC,
entry 160.368, SL 160.527, TP 156.308, RR 25.5 — comfortably clearing
MIN_RR) was rejected by Trade Decision Engine's session-window gate: it
occurred at 16:31 UTC, 31 minutes past `NEWYORK_SESSION_UTC`'s exclusive
16:00 end. Every upstream gate (sweep, POI touch, premium/discount,
local M1 structure, displacement entry candle, RR≥5) had already passed.

This is consistent with, not contradicted by, the funnel already measured
during Module 4's own real-data validation (identical dataset, same
result independently reproduced):

| Funnel stage | Bars passing | % of previous stage |
|---|---|---|
| M1 bars with an active H1 pair + direction | 804,962 / 1,200,000 | 67.1% |
| … with a matching-direction active POI (FVG/OB) | 224,211 | 27.9% |
| … where the POI was actually touched | 7,706 | 3.4% |
| … where the touch was in the favorable premium/discount half | 875 | 11.4% |
| … where the M1 entry candle qualified (displacement + swing break) | 14 | 1.6% |
| … where RR ≥ 5 | 1 | 7.1% |
| … where the session window was also satisfied | **0** | 0% |

## Why the 24-point evaluation cannot be run as numbers

With zero trades, there is no trade sequence to analyze. Fabricating
placeholder statistics would misrepresent the result. Status of each
requested analysis:

| # | Analysis | Status |
|---|---|---|
| 1 | Multi-year backtest | Not possible — M1 data spans ~10 months, not multiple years (data constraint, reported above) |
| 2 | Walk-forward validation | Not computable — 0 trades, no windows to compare |
| 3 | Out-of-sample testing | Not computable — 0 trades |
| 4 | Monte Carlo simulation | Not computable — needs a realized trade sequence to reshuffle; none exists |
| 5 | Spread sensitivity | Not computable — no trades to re-cost |
| 6 | Slippage sensitivity | Not computable — no trades to re-cost |
| 7 | News-event behavior | Untested — no historical news data source available in this sandbox |
| 8 | Session analysis | Computed structurally: 25.3% of real M1 bar-timestamps fall in the London/NY window (matches the 6/24h expectation exactly); the one real signal missed it by 31 minutes |
| 9 | Symbol comparison | 0/0/0/1 raw-signal split across XAUUSD/EURUSD/GBPUSD/USDJPY — no meaningful comparison possible at this frequency |
| 10 | Risk analysis | N/A — no capital was ever put at risk |
| 11 | Drawdown analysis | 0% (no trades, equity never moved) |
| 12 | Trade distribution | N/A — 0 trades |
| 13 | Win rate | Undefined (0 trades) |
| 14 | Expectancy | Undefined |
| 15 | Profit Factor | Undefined (`monitoring.profit_factor` correctly returns `None`, not a fabricated number) |
| 16 | Sharpe Ratio | Undefined |
| 17 | Maximum Drawdown | 0.0 (trivial — no trades) |
| 18 | Recovery Factor | Undefined |
| 19 | Equity curve | Flat at $10,000 initial balance throughout |
| 20 | Monthly returns | 0% every month |
| 21 | Trade duration | Undefined (0 trades) |
| 22 | Best market conditions | Cannot be determined from zero realized trades |
| 23 | Worst market conditions | Cannot be determined from zero realized trades |
| 24 | Failure modes | **This is where the real finding is — see below** |

## Failure mode analysis

Every individual module (1–9) was independently proven correct against
real market data with zero invariant violations throughout this build.
**The failure is not a coding defect in any module** — it is the
*compounding* effect of requiring all of the following simultaneously,
every one of which is independently reasonable but whose conjunction is
extremely restrictive:

1. An active H1 trend (structure engine) — satisfied 67% of the time, not restrictive.
2. A matching-direction, currently-untouched POI (FVG or OB) — drops to 28%. Reasonable: not every trend has a fresh POI at every moment.
3. Price actually touching that POI — drops to 3.4% of those. Expected: POIs are narrow price bands.
4. The touch landing in the *favorable* premium/discount half — only 11.4% of touches. Lower than a naive 50/50 split would suggest, likely because a POI's own creation (a displacement candle) is itself evidence of a strong directional push, which tends to leave freshly-created same-direction zones sitting nearer the *expensive* side of the range, not the discount side a pullback entry wants.
5. An M1 displacement candle also breaking the local M1 swing at that exact moment — only 1.6% of the survivors. This is the single most restrictive individual gate.
6. RR ≥ 5 — only 1 of the 14 remaining candidates clears this.
7. Landing inside a 6-hour session window — the one survivor missed this by 31 minutes.

None of these seven conditions is unreasonable in isolation, and each was
implemented and tested correctly. But requiring **all seven simultaneously**
compounds to roughly 1-in-1.2-million M1 bars, across 4 of the most
liquid pairs traded, over ~10 months of real data. This is a
**specification problem, not an implementation bug**: the frozen
Structure/FVG/OB/Liquidity engine (Modules covered by
`STRATEGY_V2_PHASE2_FREEZE_AUDIT.md`) is working correctly and is not
implicated. The bottleneck is entirely in the **threshold and
confluence choices made in Entry Confirmation (Module 4) and Trade
Decision Engine (Module 5)** — decisions this build had to make without
access to the original spec's exact numbered text (compacted out of
context earlier in this session), documented transparently at the time
as flagged, conservative interpretations (see the "Documented
Implementation Decisions" blocks in `entry_confirmation.py` and
`trade_decision.py`).

## Final decision

**B) NOT READY.**

More precisely, per the alternate branch of the requested decision
framework: **the completed strategy, as specified (including this
build's necessarily-interpreted thresholds), does not generate a usable
number of trades in real market conditions — it cannot be evaluated for
edge at all, let alone deployed.** This is a stronger and more specific
finding than "no edge": there is no trade frequency to even test for one.

**Components responsible**: Entry Confirmation's requirement that the
POI touch land in the favorable zone AND the M1 displacement/swing-break
condition (the two tightest individual filters), combined with the RR≥5
threshold and the 6-hour session window in Trade Decision Engine. The
Structure Engine, FVG, Order Block, Liquidity, POI Manager, Premium/
Discount, Risk Management, MT5 Execution, Trade Management, and
Monitoring modules are all working correctly and are not responsible for
this outcome — they were simply never exercised past the qualification
gate.

**Recommendation**: redesign the entry threshold specification rather
than incrementally tweak it. Specifically worth reconsidering as a
deliberate, evidence-based redesign decision (not a silent loosening):

- Whether RR≥5 is appropriate for this confluence stack, versus a lower
  threshold (e.g. RR≥2-3) now that six other independent filters already
  select for high-quality setups.
- Whether requiring the local M1 structure to independently confirm
  (Module 4's E4 gate) is redundant with the sweep+POI+favorable-zone
  confluence already required — it was this build's own documented,
  flagged interpretation of "CHoCH confirmation" for the entry sequence,
  not a locked, tested-against-a-written-spec requirement.
- Whether the session window should gate entry at all, or only inform
  position sizing/risk (the original plan text noted session gating was
  itself a locked decision, but its interaction with an already-rare
  signal was not something this build's earlier work anticipated at this
  severity).
- Pulling more M1 history (if the broker's retention allows) before
  re-running this exact backtester, so any redesigned thresholds can be
  evaluated against more than 10 months of data.

This report does not implicate the frozen Phase 2 components
(`STRATEGY_V2_PHASE2_FREEZE_AUDIT.md`) and recommends no changes there.
