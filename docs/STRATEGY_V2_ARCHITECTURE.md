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
  poi_manager.py       Aggregates FVG/OB/Liquidity zones into per-symbol
                       storage; evolves them forward via their own frozen
                       update functions; CHoCH-driven expiry of same-
                       direction FVG/OB zones (liquidity levels untouched).
  entry_confirmation.py M1 entry sequence: sweep confirmation -> POI touch
                       -> discount/premium validation -> local (M1)
                       structure confirmation -> entry-candle confirmation
                       -> SL/TP -> RR>=MIN_RR filter. Reuses the frozen
                       StructureEngine unmodified on M1 candles for local
                       structure confirmation (see invariant #13 below).
  trade_decision.py    Qualifies an EntrySignal into an executable trade:
                       direction filtering (one trade/symbol), portfolio-
                       level risk limits, session window, news validation
                       (via a generic callable -- see NewsManager below).
                       Returns an explicit RejectionReason on the first
                       failing gate, never silently drops a candidate.
  risk_management.py   Position sizing (delegates to the existing
                       RiskParameters.lot_size), fixed risk % selection,
                       correlated-exposure limiting (shared currency legs),
                       spread filter (delegates to NewsManager.
                       broker_cost_exceeds_atr), slippage acceptance +
                       realized-risk recomputation.

tools/validate_structure_engine.py   Structure regression suite (33 assertions)
tools/replay_structure_engine.py     8-scenario continuous structure replay
tools/validate_fvg.py                FVG regression suite (20 assertions)
tools/validate_order_blocks.py       Order Block regression suite (20 assertions)
tools/validate_numerics.py           numerics.py regression suite (20 assertions)
tools/validate_swing_diagnostics.py  Diagnostics decoupling, static no-mutation
                                      proof, and well-formedness
tools/validate_liquidity.py          Liquidity Sweep regression suite (51 assertions)
tools/validate_premium_discount.py   Premium/Discount regression suite (31 assertions)
tools/validate_poi_manager.py        POI Manager regression suite (24 assertions)
tools/validate_entry_confirmation.py Entry Confirmation regression suite (33 assertions)
tools/validate_trade_decision.py     Trade Decision Engine regression suite (18 assertions)
tools/validate_risk_management.py    Risk Management regression suite (18 assertions)
tools/validate_mt5_pending_orders.py MT5 Execution (pending orders) regression suite (20 assertions)
```

## MT5 Execution (Module 7)

`src/infrastructure/mt5_bridge/mt5_gateway.py` gained three new, purely
additive methods (nothing existing removed or modified — confirmed via
`git diff` showing 111 insertions, 0 deletions): `place_pending_order`
(TRADE_ACTION_PENDING, BUY_LIMIT/SELL_LIMIT, with retry logic that only
retries transient/retryable retcodes — a permanent rejection like
"no money" fails immediately rather than wasting the retry budget),
`poll_pending_orders` (via `orders_get()`), and `cancel_pending_order`
(TRADE_ACTION_REMOVE). Partial fills, modification, and close logic reuse
the already-existing `partial_close_position`/`modify_position`/
`close_position` — not rebuilt.

Neither `MetaTrader5` nor `mt5linux` is importable on this development
host (this code runs inside the Wine/MT5 container in production) — so,
matching this repo's own established convention
(`tools/validate_ws2_signal_dedup.py`), the new methods are validated by
driving the real bound methods against a stubbed `self` and a fake `mt5`
module object monkey-patched into the gateway module's namespace, rather
than a live connection. No real order was placed against the live/demo
account as part of this validation — actually submitting test orders
would touch live account state and was treated as outside this build's
authorization; the request-building and retry/response-handling logic is
what's being proven here, and is fully exercised by the stub-based suite.

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

poi_manager.POIManager composes fvg.py/order_blocks.py/liquidity.py's
  detected zones into one per-symbol store: add_fvg/add_ob/add_liquidity
  on detection, update(candle) once per bar to evolve every stored zone
  forward via its own existing mitigation/invalidation/sweep function,
  on_choch(invalidated_trend) to expire same-direction FVG/OB zones when
  StructureEvent.choch_fired. active_fvgs()/active_obs()/active_liquidity()
  are the query surface the future Entry Confirmation layer will use.

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

Liquidity Sweep, Premium/Discount, POI assembly, Entry Confirmation,
Trade Decision Engine, and Risk Management are now built (see the module
list above). MT5 Execution, Trade Management, and Monitoring are not yet
built — see the phased plan tracked in this session.

`trade_decision.py` reuses the existing framework-level `NewsManager`
(`src/application/news_manager.py`, already built, untouched) for news
validation via its `is_blocked(symbol, now)` method, passed in as a plain
callable — no news infrastructure was rebuilt. `risk_management.py`
similarly reuses `RiskParameters.lot_size`/`PipCalculator`/`BrokerCost`
(`src/domain/value_objects.py`, already built, untouched) and
`NewsManager.broker_cost_exceeds_atr` for the spread filter — no
position-sizing or cost-vs-ATR formula was rebuilt.

Real M1/M15 data (in addition to the existing 13-month H1 dataset) was
pulled from the live IC Markets Raw demo account for XAUUSD/EURUSD/
GBPUSD/USDJPY to validate Entry Confirmation: M15 spans ~20 months
(2024-11 to 2026-07), M1 spans ~10 months (2025-09 to 2026-07). AUDUSD and
D1 bias data are still not pulled (Phase 1 from the original plan remains
incomplete for those).

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
13. **StructureEngine is legitimately reusable, unmodified, on any
    timeframe's candle stream** — it is already fully timeframe-generic
    (only ever reads `Candle.time/open/high/low/close`; nothing is H1-
    specific). `entry_confirmation.py` runs a second, independent instance
    on M1 candles for local structure confirmation. Confirmed on real
    data: 1.2M M1 bar-closes across all 4 symbols (~10 months), 0
    trend-transition invariant violations — the same "trend only changes
    via valid BOS/CHoCH" invariant proven on H1 holds identically on M1.
14. **The full Entry Confirmation gate stack (sweep + POI touch +
    favorable-zone + local-structure + displacement entry-candle + RR>=5)
    is extremely selective by design** — confirmed on the same real M1
    dataset: of 1.2M M1 bar-closes with an active pair and direction,
    only 1 fully qualified entry signal was produced (funnel: 224,211 bars
    had a matching-direction active POI -> 7,706 touched one -> 875 were
    in the favorable premium/discount half -> 14 had a qualifying
    displacement entry candle -> 1 cleared RR>=5). This matches the
    original plan's disclosed-risk note verbatim ("RR>=5 + sweep + narrow
    session windows is a tight funnel... expect low trade frequency...
    may make reaching n>=100 slow") — it is an accepted, anticipated
    property of the locked spec, not a defect. Carried forward as a power-
    analysis fact for the Trade Decision Engine and final evaluation
    phases, not something to loosen ad hoc.

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
