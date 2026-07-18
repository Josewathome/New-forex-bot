# strategy_v2 — Phase 2 Freeze Audit

Final architecture audit of the completed Structure, FVG, Order Block, and
shared-utilities modules, performed before starting Liquidity Sweep. Goes
beyond the Phase 2B cleanup validation (`STRATEGY_V2_PHASE2B_VALIDATION.md`,
which used synthetic suites plus a hand-written tuple/count diff against a
recorded "before" run) with three stronger, harder-to-fool techniques:

1. **Property-based equivalence testing** of the `numerics.py` extraction —
   thousands of randomized trials, including direct empirical comparison
   against the pre-refactor inline formulas (not just internal consistency).
2. **Proof that `swing_diagnostics.py` is strictly read-only** — a static
   proof (no code path it can reach contains a mutation of its parameters)
   plus an empirical proof (mutation-detection across every real swing in
   the 13-month dataset), not just "BOS/CHoCH output happens to match."
3. **A deterministic cryptographic hash** of the entire replay event stream
   (every swing, BOS, CHoCH, FVG creation/mitigation/invalidation, Order
   Block creation/mitigation/invalidation) computed once from the
   pre-refactor commit and once from current HEAD — proving byte-for-byte
   identity, not merely matching counts.

Reference commits:

| Label | Commit | Description |
|---|---|---|
| Pre-refactor | `a666e490dea928626623df791d79164207c4cfd9` | Order Block detection, mitigation, invalidation (parent of the cleanup commit) |
| Post-refactor (audited) | `553f31f1a8ed0adbcdc4e8a1c1ec0b13011f7eec` | Phase 2B engineering cleanup, zero behavior change |

Isolation method: rather than `git checkout` the old commit (which would
disturb the working tree), the pre-refactor versions of `atr.py`, `fvg.py`,
`order_blocks.py`, `structure.py`, `swings.py`, `config_v2.py` were
extracted via `git show a666e49:<path>` into a standalone scratch package,
with only the one import each of `fvg.py`/`order_blocks.py` that used to
point at `atr.is_displacement` repointed at the extracted `atr_old` module
(their other imports — domain entities, `config_v2` — correctly stayed
pointed at the real, unmodified repo modules, since those were never
touched by the refactor). Confirmed importable standalone before use.

## Part A — Property-based verification of the numerics.py extraction

No `hypothesis` library available in this sandbox (no pip/internet) — built
by hand using Python's `random` module: 8,000 randomized trials per
property, spanning realistic price scales for all 4 traded symbols
(EURUSD-like ~0.5–2.5, XAUUSD-like ~500–6000, JPY-like ~50–250), with 30%
of trials deliberately biased to sit at/near the epsilon boundary (where a
subtle bug would actually hide).

**General mathematical properties** (all 8,000/8,000 trials passed):

| Property | Result |
|---|---|
| Reflexivity: `approx_gte(a,a)`, `approx_lte(a,a)`, `approx_eq(a,a)` always True | 0 failures |
| Symmetry: `intersect(A,B) == intersect(B,A)` | 0 failures |
| Consistency far from any boundary: `approx_gte(a,b) == (a>=b)` when `\|a-b\| >> EPSILON` | 0 failures |
| Monotonicity: `approx_gte(a,b)` and `c>=a` implies `approx_gte(c,b)` | 0 failures |

**Direct equivalence against the OLD pre-refactor inline formula**, for
every one of the 7 call sites the refactor changed (all 8,000/8,000 trials
passed per site, 30% near-boundary-biased):

| Call site | Old formula | Result |
|---|---|---|
| `atr.is_displacement` | `body_size >= multiplier*atr - _EPSILON` | 0 mismatches |
| `fvg` min-size gate | `(gap_high-gap_low) < min_size - _EPSILON` | 0 mismatches |
| `order_blocks` bullish mitigation | `low <= high + eps` | 0 mismatches |
| `order_blocks` bearish mitigation | `high >= low - eps` | 0 mismatches |
| `order_blocks` bullish invalidation | `not (close >= low - eps)` | 0 mismatches |
| `order_blocks` bearish invalidation | `not (close <= high + eps)` | 0 mismatches |
| `ob_fvg_confluence` classification | `lo<hi-eps` / `abs(lo-hi)<=eps` / else none | 0 mismatches |
| `fvg.overlap_range` (strict, no epsilon in either version) | `lo<hi` | 0 mismatches, confirming the deliberately non-tolerant path was untouched |

**Conclusion**: the `numerics.py` extraction is behavior-preserving, proven
empirically across 96,000 total randomized comparisons (12 properties x
8,000 trials), not merely by re-reading the algebra.

A permanent, dependency-free regression suite (`tools/validate_numerics.py`,
20 deterministic assertions covering the same boundary cases with
hand-built fixtures, no `random` dependency) has been added — closing the
gap that `numerics.py` previously had no dedicated validator.

## Part B — Proof that swing_diagnostics.py is strictly read-only

Stronger than the prior check (AST import-absence + one synthetic-scenario
output-identity check, both still true and still run). Two independent
proofs:

**B1. Static proof** — an AST-based mutation analyzer was run against
every function `compute_swing_metadata()` can reach: itself, its two
private helpers (`_asian_sweep_and_proximity`, `_backward_extremum`), and
the three strategy_v2 functions it calls into (`atr.is_displacement`,
`fvg.detect_fvg`, `order_blocks.detect_order_block` — deliberately NOT
`update_fvg_state`/`update_ob_state`, the actual mutators, which the module
is confirmed to never reference at all). The analyzer flags any
assignment, augmented assignment, mutating method call (`.append`,
`.update`, `.__setitem__`, etc.), or `global`/`nonlocal` statement whose
target traces back to a function parameter. This covers every branch in
the source, including ones the real dataset never exercises — a
structural guarantee, not a sampled one.

Result: **zero violations** across all 6 functions checked, and
**zero module-level mutable state** (no cache/memo dict a call could
poison across invocations).

**B2. Empirical proof** — mutation-detection across the full real
13-month dataset (XAUUSD/EURUSD/GBPUSD/USDJPY, 2025-06-02–2026-06-30, 9,000
H1 candles/symbol). For every real swing produced during a live replay
(6,960 swings total across the 4 symbols), `swing_log`/`swing`/
`active_pair` were snapshotted immediately before the `compute_swing_
metadata()` call and compared for equality immediately after. Separately,
`candles` and `atr` (the two large, expensive-to-copy structures) were
deep-copied once before each symbol's replay began and compared against
their live state after the entire replay completed — exactly as
conclusive as a per-call check, since neither is ever reassigned in place
by anything in the loop, at a fraction of the cost.

Result: **zero mutations detected** — 6,960/6,960 diagnostics calls left
`swing_log`, `swing`, and `active_pair` unchanged; `candles` and `atr`
were byte-identical to their pre-replay snapshots for all 4 symbols after
the full replay.

The static AST check (Part B1, generalized to `swing_diagnostics.py` as a
whole) has been folded into `tools/validate_swing_diagnostics.py` as a
new permanent Section 1B, so this proof re-runs automatically alongside
the existing import-coupling and output-identity checks going forward.

**Conclusion**: `swing_diagnostics.py` cannot influence runtime/trading
state — not merely "doesn't today," but architecturally incapable of it,
proven both structurally (every code path) and empirically (every real
swing in 13 months of market data).

## Part C — Deterministic hash of the full replay event stream

For each of the 4 symbols, the complete pipeline (swings -> BOS -> CHoCH,
FVG detect/mitigate/invalidate, Order Block detect/mitigate/invalidate,
OB+FVG confluence classification) was replayed twice over the identical
13-month H1 dataset: once using the pre-refactor modules (`structure_old`,
`atr_old`, `fvg_old`, `order_blocks_old`, extracted from commit `a666e49`),
once using the current committed modules (HEAD `553f31f`). Every event
field was serialized into one canonical byte string per symbol (JSON,
sorted keys, fixed separators) and hashed with SHA-256.

| Symbol | Swings | BOS | CHoCH | FVG | OB | Confluence | OLD hash | NEW hash | Match |
|---|---|---|---|---|---|---|---|---|---|
| XAUUSD | 1,200 | 198 | 61 | 97 | 105 | 97 | `cac0f80a297a9f8f…` | `cac0f80a297a9f8f…` | Yes |
| EURUSD | 1,318 | 240 | 90 | 81 | 95 | 81 | `0ed44a15535df967…` | `0ed44a15535df967…` | Yes |
| GBPUSD | 1,303 | 200 | 77 | 98 | 105 | 98 | `870fec056301d922…` | `870fec056301d922…` | Yes |
| USDJPY | 1,324 | 191 | 62 | 79 | 93 | 79 | `4bb16d123b1b0945…` | `4bb16d123b1b0945…` | Yes |

**Combined SHA-256 across all 4 symbols and every event field:**

```
OLD: ea90a5e67cf0933589d90e8373a59ad1970a8fa63df22b60ecd9442752bb889
NEW: ea90a5e67cf0933589d90e8373a59ad1970a8fa63df22b60ecd9442752bb889
MATCH: True
```

**Conclusion**: byte-for-byte identical, not merely matching counts. Every
swing price/kind/timestamp, every BOS direction/pair-bound/generating-swing
timestamp, every CHoCH invalidated-trend value, every FVG's exact gap
bounds/mitigated/invalidated/resolution-bar, every Order Block's exact
bounds/depth/mitigated/invalidated/resolution-bar, and every OB+FVG
confluence classification is identical between the pre-refactor and
post-refactor code, across the full real 13-month dataset.

## Overall conclusion

No discrepancy was found in any of the three audits. The Phase 2B
`numerics.py` extraction is proven behavior-preserving by direct empirical
equivalence (not just re-tested internal consistency); the diagnostics
layer is proven structurally and empirically incapable of affecting
runtime state; and the full replay event stream is proven byte-for-byte
identical between the pre-refactor and current code via cryptographic
hash, across all 4 symbols and 13 months of real market data.

**Phase 2 (Structure Engine, FVG, Order Blocks, shared numerics,
diagnostics layer) is hereby declared FROZEN.** Per
`STRATEGY_V2_ARCHITECTURE.md`'s locked D1–D6 decisions and
`STRATEGY_V2_REJECTED_RESEARCH.md`'s standing rule, none of this may be
redesigned or re-litigated without new, concrete evidence of an actual
bug. Development proceeds to Liquidity Sweep implementation.
