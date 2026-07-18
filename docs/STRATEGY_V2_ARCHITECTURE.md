# strategy_v2 — Architecture & Engineering Invariants

**PHASE 2 STATUS: FROZEN** (Structure Engine, FVG, Order Blocks, shared
`numerics.py`, diagnostics layer) — see `STRATEGY_V2_PHASE2_FREEZE_AUDIT.md`
for the property-based equivalence, read-only, and deterministic-hash
proofs backing this. Locked design decisions below (D1–D6) and proven
invariants must not be redesigned or re-litigated without new, concrete
evidence of an actual bug.

This document describes the new SMC signal-generation engine that replaces the
archived `legacy/application/{analysis,trade_monitor,backtester}.py` in full.
It is the permanent reference for module boundaries, locked design decisions,
and the invariants proven during Phase 1.5/2A/2B research and validation.

## Module layout

```
src/domain/strategy_v2_entities.py   Domain types: TrendState, SwingPoint,
                                      ExternalSwingPair, FVGZone, OrderBlockZone,
                                      LiquidityLevel. Kept separate from the
                                      generic entities.py (Candle, Direction,
                                      Timeframe, Trade — reused as-is).

src/application/strategy_v2/
  config_v2.py        Strategy-specific constants (unit conventions,
                       thresholds, R-multiples, risk/RR limits). Framework
                       config (credentials, position caps, sessions dict
                       shape) stays in src/config.py, untouched.
  numerics.py          Shared floating-point-tolerant comparison helpers
                       (EPSILON, approx_gte/lte/eq) and range-intersection
                       math (intersect()). Extracted in Phase 2B cleanup.
  atr.py               Wilder ATR(10) + the displacement test.
  swings.py            Universal 7-candle swing-high/low detection.
  structure.py         External Swing Pair / BOS / CHoCH / Trend state
                       machine (D1-D6). LOCKED.
  fvg.py               Fair Value Gap detection, mitigation, invalidation.
  order_blocks.py       Order Block detection (incl. Fallback Rule),
                       mitigation, invalidation, OB+FVG confluence.
  swing_diagnostics.py OPTIONAL, decoupled diagnostics layer. NOT imported
                       by any of the above. See "Diagnostics layer" below.
  liquidity.py         Liquidity Sweep detection: Asian range, previous-day,
                       and equal-high/low resting levels; sweep confirmation
                       (stop-hunt-then-reverse) vs. invalidation (breakout).
                       Not yet consumed by anything (POI Manager is next).
  premium_discount.py  Premium/Discount classification off the active
                       External Swing Pair: equilibrium, Fib anchoring,
                       trend-aligned favorable zone. See invariant #12 below
                       re: pair.low/.high normalization.

tools/validate_structure_engine.py   Structure regression suite (33 assertions)
tools/replay_structure_engine.py     8-scenario continuous structure replay
tools/validate_fvg.py                FVG regression suite (20 assertions)
tools/validate_order_blocks.py       Order Block regression suite (20 assertions)
tools/validate_numerics.py           numerics.py regression suite (20 assertions)
tools/validate_swing_diagnostics.py  Diagnostics decoupling, static no-mutation
                                      proof, and well-formedness
tools/validate_liquidity.py          Liquidity Sweep regression suite (51 assertions)
tools/validate_premium_discount.py   Premium/Discount regression suite (31 assertions)
```

## Data flow (as built so far)

```
H1 candle close
      │
      ▼
StructureEngine.process_new_candle_close()
      │  confirms swings (swings.py) → BOS/CHoCH/Trend (D1-D6)
      │
      ├──► swing_log, active_pair, trend  (consumed by future POI/Fib logic)
      │
      └──► (OPTIONAL, not wired in) swing_diagnostics.compute_swing_metadata()
                — a caller may invoke this per confirmed swing, passing the
                  engine's own active_pair snapshot at that moment. structure.py
                  has zero import-level awareness this module exists.

Independently, per 3-candle window (c1, c2, c3):
      atr.wilder_atr_series / atr.is_displacement
             │
             ├──► fvg.detect_fvg  ──► FVGZone (mitigation/invalidation tracked
             │                        forward via fvg.update_fvg_state)
             │
             └──► order_blocks.detect_order_block ──► OrderBlockZone (tracked
                                     forward via order_blocks.update_ob_state)

order_blocks.ob_fvg_confluence(ob, fvg) — inclusive overlap, tags
  'positive_width' vs 'boundary_touch', feeds the future POI-priority /
  Entry-Zone-intersection logic (not yet built).

premium_discount.* reads StructureEngine.active_pair directly (read-only,
  same pattern as swing_diagnostics/liquidity) to classify prices/zones as
  premium, discount, or equilibrium, and to expose the trend-aligned
  favorable half for the future POI Manager / Entry Confirmation.

Independently, per symbol:
      liquidity.detect_asia_levels / detect_prev_day_levels (per-candle scan)
      liquidity.detect_equal_level (called once per confirmed swing, same
             calling convention as compute_swing_metadata — reads
             StructureEngine.swing_log, writes nothing back to it)
             │
             ▼
      LiquidityLevel (ASIA_HIGH/LOW, PREV_DAY_HIGH/LOW, EQUAL_HIGH/LOW)
             │
             └──► liquidity.update_sweep_state tracks sweep/reaction
                  forward, one-way, mirroring fvg/order_blocks lifecycle
                  tracking. active_unswept_levels() is the pool-query
                  surface the future POI Manager will use.
```

Liquidity Sweep, Premium/Discount, POI assembly, Entry Confirmation, and
Strategy integration are **not yet built** — see the phased plan tracked
in this session.

## Locked design decisions (D1–D6)

These are final and must not be redesigned or re-litigated without new,
concrete evidence of a bug (not a preference or an untested idea):

- **D1**: A BOS test level is edge-triggered — once it fires, it's retired
  so it can't re-fire on a later bar that merely remains beyond it.
- **D2**: The External Swing Pair's generating member = the most recently
  confirmed opposite-type swing whose own candle chronologically precedes
  the broken level — not the lowest/highest in range, not the highest-
  scoring by any metadata composite (see Rejected Research).
- **D3**: A pending level cannot fire a trend-establishing BOS until a
  qualifying opposite-type swing exists that precedes it (WAIT state).
  The level is not retired while waiting.
- **D4 (revised)**: CHoCH clears only active structure state (trend,
  active pair, the two BOS-test bootstrap pointers) — historical
  confirmed swings are preserved in `swing_log` for audit/recovery,
  never deleted.
- **D5**: While a trend is active, only CHoCH (close vs. the active
  pair's own low/high) can end or flip it. An independently-tracked
  opposite-type pending swing is never tested for a direct flip.
- **D6**: A continuation BOS rolls the active pair forward to the newest
  pair rather than freezing the original.

## Proven invariants

Each of these has been verified against the real 13-month H1 dataset
(XAUUSD/EURUSD/GBPUSD/USDJPY, 2025-06-02–2026-06-30), not just synthetic
fixtures:

1. **Trend may only change via a valid BOS-from-NEUTRAL or a valid
   CHoCH-to-NEUTRAL.** Zero violations across ~26,000 real bar-transitions.
2. **BOS requires a body close, not a wick.**
3. **A swing confirming after an already-pending opposite-type level can
   never retroactively pair with that level** (D2's "must precede"
   requirement is permanent and chronological) — a corollary of D2+D3,
   not a separate rule.
4. **Invalidation implies mitigation was already true**, for both FVG and
   Order Block — geometrically guaranteed (the far side is farther than
   the near side/midpoint), confirmed with zero counter-examples across
   355 real FVGs and 398 real Order Blocks.
5. **Mitigation is a one-way flag** — once True, permanently ends the
   zone's tradability ("maximum 1 mitigation" for OB; FVG mirrors this).
6. **Displacement's ATR reference must be computed THROUGH the candle
   BEFORE the displacement candle, not through the displacement candle
   itself** — a self-inclusive ATR would let the candle's own volatility
   inflate the baseline used to judge it, making an exact-boundary test
   circular and unconstructible.
7. **Floating-point boundary comparisons require an epsilon tolerance**
   (`numerics.EPSILON = 1e-9`) — binary floats cannot represent most
   decimal price fractions exactly (e.g. `1.1010 - 1.1005 ==
   0.0004999999999999449`, not `0.0005`), so a raw `<`/`>`/`==` would
   wrongly reject genuine exact-boundary cases. This applies everywhere a
   boundary comparison occurs, not only where it's used today.
8. **OB+FVG confluence uses inclusive (`<=`) overlap semantics** — a
   boundary-touch (the common case, since an Order Block is usually the
   same candle as the FVG's own Candle1) counts as valid confluence, but
   is explicitly tagged `boundary_touch` vs. `positive_width` and never
   silently widened into a fake positive-width range. Confirmed on real
   data: 100% of no-fallback-needed Order Blocks produce exactly
   `boundary_touch` confluence with their paired FVG; genuine
   `positive_width` overlap occurs only in Fallback Rule cases.
9. **No look-ahead is permitted anywhere in structure/FVG/OB detection or
   lifecycle tracking.** Every function operates only on data through the
   current candle (or, for swing confirmation specifically, the same
   3-bar-forward window the rule itself requires — never beyond it).
10. **A liquidity level's `swept` flag is one-way**, same as FVG/OB
    mitigation — confirmed with zero counter-examples across 5,697 real
    liquidity levels (all 4 symbols, 13 months). Its `reaction`
    classification (`confirmed_reversal` vs. `breakout`) is recorded once,
    at the first qualifying candle only, and never changes afterward.
11. **Equal-high/low liquidity levels are an independent concept from the
    rejected equal-hl swing-selection tie-break** (`STRATEGY_V2_REJECTED_
    RESEARCH.md` #4) — `liquidity.detect_equal_level` marks a resting-
    liquidity target for a future POI/entry layer; it has zero effect on
    which swing the structure engine selects, zero import coupling into
    `structure.py`, and is never used as a filter.
12. **`ExternalSwingPair.low`/`.high` are NOT guaranteed `low <= high`.**
    They are labels for "the swing-LOW-type member" and "the swing-HIGH-
    type member" of the pair (per structure.py's own docstring), not
    sorted range bounds — D2 pairs the most-recently-confirmed opposite-
    type swing chronologically, not the numerically lowest/highest one.
    Confirmed on real data: 17 real bar-closes across 3 symbols (13-month
    dataset) had an active pair with `low > high` numerically, all in
    genuine `_try_continue_bearish`/choppy conditions — structure.py is
    unchanged and behaved exactly as designed. Any consumer needing an
    ordered Fibonacci range (first: `premium_discount.py`) MUST normalize
    via `(min(low,high), max(low,high))`, never use `.low`/`.high` as
    range bounds directly.

## Diagnostics layer (swing_diagnostics.py)

Optional, descriptive-only. Architecturally decoupled from structure.py
by construction: `structure.py` never imports it (verified by AST
inspection in `tools/validate_swing_diagnostics.py`), so disabling it
means simply never calling it — there is no code path connecting
diagnostics output to any trading decision by default. It must **never**
be wired into swing selection, structure formation, or trade filtering —
see `STRATEGY_V2_REJECTED_RESEARCH.md` for why three independent attempts
to do exactly that were rejected.

## Validation framework convention

Every module gets its own `tools/validate_<module>.py`, following the
established pattern (no pytest — matches this repo's existing
`tools/validate_ws2_signal_dedup.py`/`validate_ws3_cap.py` convention):
plain script, direct calls against the real committed code (no
reimplementation), `check()`/PASS-FAIL prints, non-zero exit on failure.
Structure validation (swing/BOS/CHoCH/trend correctness, invariants,
deterministic replay) and strategy validation (does the full assembled
system have a trading edge) are kept strictly separate — structure
validation never runs a P&L proxy as its pass/fail criterion, and
strategy validation does not begin until Liquidity Sweep, Premium/
Discount, Order Blocks, FVG, Entry Confirmation, and Risk Management are
all present together. The same per-module pattern (unit/synthetic tests
first, then a real-market adversarial replay, then a locked commit) will
be used for Liquidity Sweep, Premium/Discount, and Entry Confirmation.
