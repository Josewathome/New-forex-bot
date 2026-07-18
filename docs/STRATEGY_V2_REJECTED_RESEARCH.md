# strategy_v2 — Rejected Research

Three independent research cycles investigated whether the structure
engine's swing/pair handling could be improved. All three came back
negative. This document exists so that **none of these ideas are
reintroduced without new evidence** — re-testing an already-falsified
hypothesis wastes review time that should go to Liquidity Sweep,
Premium/Discount, and Entry Confirmation, where the real edge is more
likely to live.

Methodology used throughout: real 13-month H1 data (XAUUSD, EURUSD,
GBPUSD, USDJPY, 2025-06-02–2026-06-30), genuine counterfactual replay
(never relabeling existing history — a parameterized engine copy,
sanity-matched against the real committed engine before being trusted),
bootstrap 95% confidence intervals, Welch t-tests, and results pooled
across all 4 symbols (per-symbol trends that look encouraging in
isolation are not trusted unless they survive pooling).

## 1. Largest-displacement swing selection — REJECTED

**Idea**: instead of D2's "most recent confirmed opposite swing," pick
the swing with the largest displacement magnitude among the candidate
pool.

**Evidence**: catastrophic on real data. BOS/CHoCH frequency collapsed
3–10× (EURUSD: 240→22 BOS, 90→4 CHoCH). Average trend duration inflated
to absurd levels (EURUSD: 1,322 bars ≈ 55 days for one "trend"). Average
Fib/pair width ballooned up to 6× (XAUUSD: 681→5,865 pips). The rule
locks onto rare historical extremes and stops resetting — not a usable
structural model. Disqualified outright, no significance testing needed.

## 2. Liquidity-based swing selection — REJECTED (no-op, not harmful)

**Idea**: prefer the most recent candidate swing that had an Asian-range
sweep nearby, falling back to D2's default otherwise.

**Evidence**: with a 48-bar sweep-detection window, results were nearly
identical to D2 on every metric (BOS/CHoCH counts within ~5%, trend
duration and Fib width within a few pips). A sweep of *some* kind occurs
near almost every swing within a 2-day window, so the filter almost
never overrides the default. Neither harmful nor helpful as
operationalized; a materially tighter sweep-recency requirement was not
tested (would be new scope).

## 3. BOS-validated swing selection (retrospective "did it later matter")

**Evidence**: a retrospective significance score (was this swing later a
BOS-generating or CHoCH-reference member, did it hold as an extremum over
±20 bars) showed D2's actual choice matches the unbounded-pool
best-scoring candidate only 11.3% of the time — but bounding the
comparison to the last 15 plausible candidates (removing months-old,
practically-irrelevant options from the comparison), D2 matches the best
available candidate **46.4%** of the time, average rank 3.1 of 15,
average score gap only 2.4 points. The unbounded number is misleading
about how well D2 actually performs against genuinely relevant
alternatives.

## 4. Equal High/Low tie-break — REJECTED

**Idea**: among the last 3 confirmed same-kind candidates, prefer the
most recent one with an equal-level nearby, else D2's default.

**Evidence**: a targeted 100-sample study found 29 cases where D2's pick
looked inferior by a full significance score — but splitting out
look-ahead components, only 12/29 were genuine real-time-observable
disagreements (13/29 were ties with no observable signal, 4/29 actually
favored D2 once look-ahead was removed). Of those 12, `equal_hl` was the
dominant recurring pattern (9/12). Built the smallest bounded tie-break
and replayed it across the full dataset: BOS/CHoCH counts stayed stable,
but generating-member prominence — the metric the tie-break was meant to
improve — got **worse**, not better (±10 bars: 41.6%→39.9%; ±20:
20.5%→17.6%; ±50: 6.5%→4.2%). Equal-highs/lows likely marks a retested,
congested price zone rather than a decisively-held extreme. The
retrospective pattern did not survive an honest, non-look-ahead replay.

## 5. Weak swing pre-filtering (excluding the bottom 10/20/30% from structure)

**Idea**: compute a live-only composite quality score per swing (sweep,
equal-hl, FVG/OB origin, displacement), exclude the weakest 10/20/30%
from ever entering `swing_log`/becoming pair candidates.

**Evidence** (oracle-informed global percentile threshold — an upper
bound; a real causal/rolling threshold would be strictly less
informed): pooled R-multiple-proxy performance was **flat across all
filter levels** (0%: −0.066, 10%: −0.077, 20%: −0.057, 30%: −0.052, all
95% CIs overlapping and including zero). Welch t (30% vs. baseline) =
**+0.18** — not remotely significant. Per-symbol results were
inconsistent and canceled out on pooling (XAUUSD improved, USDJPY
worsened, EURUSD/GBPUSD flat). The only consistent effect — longer
average trend duration (75→90 bars) — is a mechanical artifact of
discarding candidates, not a quality signal; it didn't correlate with
improved false-break rate or trading performance.

## 6. Institutional swing scoring / composite quality scores — REJECTED

Same composite score as #5, tested both as a structure pre-filter and as
a downstream trade-selection filter (structure held 100% fixed, only
deciding which BOS-derived signals to act on). Downstream test: pooled
mean R went from −0.066 (baseline) to −0.100 (skip weakest 30%) — the
wrong direction, though not significant (t=−0.43). Per-symbol
inconsistent (GBPUSD notably worse, others mixed).

## 7. Metadata-based trade filtering — REJECTED

Per-field analysis (not just the composite) on 729 real trade signals:
`equal_hl_proximity` showed a directionally interesting but
**non-significant** split (+0.110 True vs. −0.113 False, t=+1.71).
`fvg_origin` and `ob_origin` showed large t-statistics (−2.11, −18.18)
**on samples of only 10 and 6 signals respectively** — flagged explicitly
as small-sample artifacts, not real findings; a t=−18 on n=6 is exactly
the kind of result that must not be acted on. Continuous fields
(`displacement_magnitude`, `htf_pair_proximity_atr`,
`dist_prev_structural_swing`) showed near-zero correlation with outcome
(all |r| < 0.09).

## Standing rule

Do not reintroduce any of the above (or a close variant — e.g. a
different lookback window for the equal-hl tie-break, a different
percentile for pre-filtering, a different weighting for the composite
score) without a fresh, full replay against the real dataset producing a
**pooled, statistically significant** improvement across all 4 symbols.
A result that looks good on one symbol and is silent or negative on the
others is not evidence — it's noise, as demonstrated repeatedly above.

The one thing that *is* proven and kept: `swing_diagnostics.py` computes
this same metadata correctly, causally, and with zero structural
side-effects (verified by import-graph inspection and byte-for-byte BOS/
CHoCH identity checks). It remains available as an **optional, descriptive
diagnostics layer only** — never as a filtering or selection rule.
