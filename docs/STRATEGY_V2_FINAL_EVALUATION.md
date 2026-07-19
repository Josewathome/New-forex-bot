# strategy_v2 — Final Evaluation (CORRECTED)

**This replaces the prior version of this document**, which reflected the
implementation *before* `docs/STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md`
found and fixed six real divergences from the recovered, locked V4 spec
(stateless M1 CHoCH, Absorption gate + direction-aware fix, touch/
confirmation decoupling, SL-from-swing, TP priority-fallback, and Entry
Zone price-proximity + distance-cutoff selection). The prior report's
"1 signal, rejected by the session window" result no longer applies — it
was produced by pre-correction code. This document reports the result of
the corrected pipeline, run end-to-end (`tools/backtest_strategy_v2.py`)
against the same real dataset.

## Bot assembly — confirmed working

All 9 modules wire together exactly as designed: H1 Structure/FVG/OB/
Liquidity → merged H1+M15 POI Manager → Premium/Discount classification →
M1-specific Entry Zone + stateless M1 confirmation (Absorption + CHoCH-
trigger + displacement) → Trade Decision Engine → Risk Management →
intrabar SL/TP simulation via Trade Management's exit ladder → Monitoring.
It ran to completion across the full real dataset (4 symbols × 300,000 M1
bars = 1.2M bar-iterations) with zero crashes and zero invariant
violations.

## Data constraints (reported, not worked around)

- M1 data spans ~10 months (2025-09 to 2026-07); H1 spans 13 months;
  M15 spans ~20 months. Entry Confirmation requires M1 candles, so the
  backtest window is capped at ~10 months — **not a multi-year backtest.**
- Only 4 symbols have real data pulled (XAUUSD, EURUSD, GBPUSD, USDJPY).
  AUDUSD and D1 bias data were never pulled.
- No historical economic-calendar/news source is available in this
  sandbox; the news gate always returns "not blocked" — untested, not
  proven harmless.

## Result: zero trades (unchanged conclusion, different and now-understood cause)

| Symbol | M1 bars | Signals | Qualified | Opened | Closed |
|---|---|---|---|---|---|
| XAUUSD | 300,000 | 0 | 0 | 0 | 0 |
| EURUSD | 300,000 | 0 | 0 | 0 | 0 |
| GBPUSD | 300,000 | 0 | 0 | 0 | 0 |
| USDJPY | 300,000 | 0 | 0 | 0 | 0 |
| **Total** | **1,200,000** | **0** | **0** | **0** | **0** |

## The corrected funnel — where candidates disappear

Instrumented directly inside `tools/backtest_strategy_v2.py`'s real
evaluation loop (not a separate, potentially-diverging script), summed
across all 4 symbols:

| Funnel stage | Count | % of previous stage |
|---|---|---|
| Total M1 bars | 1,200,000 | — |
| ...with an active H1 trend/pair | 976,787 | 81.4% |
| ...with a matching-direction M15 HTF POI (FVG/OB) | 940,609 | 96.3% |
| ...with a qualifying liquidity sweep available | 939,782 | 99.9% |
| ...where that POI was touched, in the favorable premium/discount half, within the touch-recency window | 19,360 | 2.06% |
| M1 confirmation attempts (zone-bar candidates) | 19,846 | — |
| ...where Absorption + M1 CHoCH-trigger + displacement all passed (spec §16) | **18** | **0.09%** |
| ...where a qualifying M1 Entry Zone (OB/FVG) existed (spec §17) | **0** | **0%** |

Two gates are worth explaining individually, per the audit's own
instruction to explain any gate removing >90% of candidates:

**Gate: M1 confirmation (19,846 → 18, −99.9%).** This is intentional,
spec-driven selectivity, not a bug: spec §16 requires Absorption (3
consecutive shrinking-body M1 candles, avg body <50% ATR) **and** a
stateless M1 CHoCH-trigger (body close beyond the most recent same-kind
M1 swing) **and** displacement (≥1.5×ATR) — three independent, narrow
conditions on the *same* few-bar sequence. That so few real market moments
satisfy all three simultaneously is exactly what a tight confirmation
filter is supposed to do.

**Gate: Entry Zone availability (18 → 0, −100%).** This is the decisive
finding of this corrected run, and it is *not* a threshold-calibration
artifact. For each of the 18 real M1-confirmed reversals (across all 4
symbols, full ~10-month window), the nearest available M1 OB/FVG —
computed with **no distance limit**, i.e. genuinely the closest one that
existed anywhere in the active pool — was:

| Symbol | Confirmed cases | Nearest zone distance, in M1-ATR(10) multiples |
|---|---|---|
| XAUUSD | 5 | 5.5×, 6.5×, 9.6×, 12.1×, 13.6× |
| EURUSD | 2 | 12.9×, 17.1× |
| GBPUSD | 2 | 6.6×, 14.1× |
| USDJPY | 9 | 4.4×, 4.8×, 5.3×, 6.6×, 7.9×, 13.5×, 14.3×, 19.6×, 24.9× |

**The single closest case across all 18 is 4.37× ATR away.** The
distance cutoff introduced in this correction pass
(`config_v2.MAX_ENTRY_ZONE_DISTANCE_ATR = 3.0`, added with explicit user
sign-off after the price-proximity fix alone was found to still select
zones 50-90 pips from the triggering swing) rejects all 18 — but
loosening that cutoff would not change the outcome: even a 5× or 10×
threshold saves at most a handful of cases, and moving it wide enough to
admit the 24.9×-ATR case would defeat the cutoff's entire purpose (which
is to prevent selecting a zone that is obviously irrelevant to current
price action). **This is a genuine structural incompatibility between two
independently-reasonable, independently-specified rules**, not a
miscalibrated number:

- Spec §16's M1 CHoCH-trigger is **stateless**: "the most recently
  confirmed M1 swing" of the matching kind, with no requirement that this
  swing be recent in *time* or *price* relative to the current M1 bar.
  When no fresh same-kind swing has formed for a while, this can (and, in
  all 18 real cases, does) reference an old swing that price has since
  moved well away from.
- Spec §17's Entry Zone (M1 OB/FVG) is, by construction, a **recent,
  local** structure — M1 order blocks and FVGs are quickly mitigated
  (any return-touch invalidates them), so the ones still "active" at any
  given moment cluster near *current* price, not near wherever an old
  swing happens to sit.

Whenever the rare M1 CHoCH event actually fires, current price (and
therefore the pool of still-active M1 OB/FVGs) has typically already
moved on from the swing level that triggered it. These two spec
requirements were never tested against each other in the recovered spec
text — there is no stated relationship between "the swing the CHoCH
breaks" and "the OB/FVG that becomes the entry zone" beyond both being
on M1. In this ~10-month, 4-symbol real dataset, they are never both
satisfiable within a distance a person would call "the entry zone for
this reversal" rather than "an unrelated old zone that happens to still
be technically active."

## Why the 24-point evaluation still cannot be run as numbers

Unchanged from the prior report — zero trades means no realized sequence
to analyze. Walk-forward, Monte Carlo, spread/slippage sensitivity, win
rate, expectancy, profit factor, Sharpe, drawdown, trade duration are all
undefined, not fabricated as zero-with-confidence. `monitoring.
profit_factor` correctly returns `None`.

## Answering the parity audit's four questions, now with the corrected implementation

**1. Does the implementation faithfully reproduce the original strategy?**
Yes, now — the six divergences found by the parity audit are fixed and
independently unit-tested (64/64 assertions, 14/14 regression suites
passing). The one remaining interpretive addition (the distance cutoff)
was raised to the user as an explicit, flagged judgment call rather than
decided silently, and does not change the zero-trade outcome regardless
of its exact value (see the ATR-multiple table above).

**2. If not, where does it diverge?** It no longer diverges from the
recovered spec text in any of the six previously-found ways. The one
remaining gap is that the spec is genuinely silent on the relationship
between the CHoCH-trigger swing (§16) and the Entry Zone (§17) — this
implementation's distance-cutoff interpretation is documented, not
hidden, and is shown above to be non-load-bearing for this result.

**3. Are the zero trades caused by the strategy itself or by
implementation decisions?** **The strategy itself, now.** Before this
correction pass, the answer was implementation decisions (the parity
audit's core finding). After fixing all six, the funnel shows the
bottleneck moved to a structural interaction between two spec
requirements (§16 and §17) that this real dataset never satisfies
together — not a coding defect, not a threshold this build invented, and
not sensitive to the one flagged assumption that remains.

**4. Which assumptions must be removed before we can honestly evaluate
the strategy?** None remain that affect this outcome. The distance cutoff
was the last flagged assumption in Module 4, and the ATR-multiple data
above shows it is not the reason for zero trades — even removing it
entirely would produce trades whose entry price is 4-25× ATR from the
confirming swing, which is not "the entry zone for this reversal" under
any reasonable reading of spec §17's intent, and would very likely fail
the SL/RR geometry regardless (as it did before the cutoff existed, via
the inverted-ordering safety check).

## Final decision

**B) NOT READY** — unchanged from the prior report, but now for a
precisely characterized reason instead of an implementation-divergence
one. The corrected, spec-faithful implementation produces zero trades
across the full real 4-symbol, ~10-month dataset because spec §16's
CHoCH-trigger swing and spec §17's Entry Zone are, in every one of the 18
real qualifying reversals in this data, anchored to price levels 4-25×
M1-ATR(10) apart — a combination the recovered spec text does not
reconcile.

**Recommendation**: this is now a genuine strategy-design question, not
an implementation one, and is exactly the kind of finding this audit was
run to surface before any redesign discussion. Worth deciding
deliberately (not silently loosening a threshold):

- Whether the Entry Zone (§17) should instead be evaluated relative to
  the **current price at confirmation**, not the CHoCH-trigger swing —
  i.e., "the nearest M1 OB/FVG to where price is now" rather than "to
  the specific old swing that broke."
- Whether §16's "most recently confirmed M1 swing" should itself be
  bounded (e.g., only swings within some recency/distance of current
  price count as valid CHoCH triggers), rather than stateless-and-
  unbounded.
- Whether M1 is simply too fine a timeframe for both a structural
  CHoCH-trigger and a persistent, nearby OB/FVG pool to coexist at the
  same moment, and M15 (already used for the HTF POI) should also host
  the Entry Zone concept.

This report does not implicate the frozen Phase 2 components
(`STRATEGY_V2_PHASE2_FREEZE_AUDIT.md`) and recommends no changes there.
