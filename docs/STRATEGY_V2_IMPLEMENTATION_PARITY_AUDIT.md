# strategy_v2 — Implementation Parity Audit

**No code changed. No thresholds changed. This is analysis only.**

## How this audit was possible

The original locked specification ("SMC Strategy — Final Specification
(V4)", §1–§24, plus a follow-up "V4 – Final Clarifications Before
Implementation" resolving 12 residual items) was pasted by the user
earlier in this session, then compacted out of context before Modules
1–9 were built. Structure/Swings/ATR/FVG/Order Block (Phase 1.5/2A/2B,
frozen — see `STRATEGY_V2_PHASE2_FREEZE_AUDIT.md`) were built **with**
this spec in context and match it exactly (verified below). **Modules
1–9 (Liquidity Sweep through Monitoring) were built without it** — from
the plan file's paraphrased summary, config constants already locked
earlier, and this build's own good-faith reconstruction. This audit
recovered the verbatim spec from the pre-compaction transcript
(`416d9b86-...jsonl`, the V4 doc at transcript line 2621, the 12
clarifications at line 2628) to check that reconstruction against the
actual binding text.

**Headline finding**: several Module 1–9 choices are not defensible
interpretations of an unavailable spec — they contradict explicit spec
text that simply wasn't in context at the time. This is the single most
important input to the zero-trade question below.

---

## Part 1 — Module-by-module audit

### Structure / Swings / ATR / FVG / Order Block (frozen, built with spec in context)

| Item | Spec (§) | Implementation | Match |
|---|---|---|---|
| Swing High/Low | §2: 3-bar strict left / non-strict right | `swings.py` | ✅ Exact |
| External Swing Pair derivation | §3, clarification #1: most-recent-confirmed-opposite-swing | `structure.py` D2 | ✅ Exact |
| BOS | §5: H1 body close beyond pair extreme | `structure.py` | ✅ Exact |
| Post-CHoCH WAIT state | §3 edge case, clarification #2 | `structure.py` D3 (WAIT via pending levels) | ✅ Exact |
| FVG gap/min-size/displacement | §6, §7, §9 | `fvg.py` | ✅ Exact |
| FVG mitigation | Clarification #3: wick-touch, no close required | `fvg.py` | ✅ Exact |
| OB definition/fallback/mitigation | Locked separately (transcript line 3389, "Order Block — Complete Specification") + clarification #4 | `order_blocks.py` | ✅ Exact |
| OB+FVG confluence | Resolved as inclusive (locked in the same OB spec round) | `order_blocks.py` | ✅ Exact |

No divergence found in the frozen layer — consistent with it being built
while the spec was actually in context. **This audit recommends no
changes to the frozen layer.**

### Module 1 — Liquidity Sweep

| Spec (§) | Implementation | Match? | Impact |
|---|---|---|---|
| §11: Asian High/Low, Equal H/L, Prev-Day H/L, **detected on M15 AND H1** | Only H1 detection built (`liquidity.py`) | ❌ **No** — M15 liquidity entirely missing | High — halves the resting-liquidity pool available for sweep detection at the timeframe the POI actually lives on |
| Clarification #5: M15+H1 dup merged into one pool, H1 primary/M15 secondary | Not applicable — M15 was never detected, so no dedup logic exists | ❌ No (moot without M15) | Low on its own, but confirms the M15 gap is real, not an oversight in wording |
| §12: same-candle wick-exceeds + body-closes-back-inside, 1 pip/10 pt min penetration | `classify_sweep_reaction` | ✅ Exact | — |
| §9 unit table (tolerances/penetration) | `config_v2.py` constants | ✅ Exact | — |
| — | "London sweep classification" (`swept_during_london`) | Extra, not in spec | None — descriptive only, never gates anything |

**Assumption introduced**: none beyond the H1-only scope reduction, which
was not flagged as an assumption at build time — it was a **silent
scope gap** (§11's M15 requirement wasn't visible in the compacted
context, so its absence wasn't even flagged as a decision).

### Module 2 — Premium/Discount

| Spec (§) | Implementation | Match? | Impact |
|---|---|---|---|
| §10: H1 only, Fib range = External Swing Pair | `premium_discount.py` | ✅ Exact | — |
| §10: "Buy valid only below 50%, **preferred 61.8–78.6%**" (the OTE band) | Only binary DISCOUNT/PREMIUM/EQUILIBRIUM classification — no 61.8–78.6% sub-zone anywhere | ❌ **No** — OTE band never implemented | Medium — this band is also load-bearing for §23's risk-scaling rule (below), so its absence removes a real signal-quality dimension, not just a cosmetic preference |
| pair.low/.high normalization (min/max) | Documented, correct, real finding from this session (invariant #12) | N/A — not a spec item, a genuine implementation necessity | — |

### Module 3 — POI Manager

| Spec (§) | Implementation | Match? | Impact |
|---|---|---|---|
| §5 / clarification #2: on CHoCH, **delete** all active POIs (OB+FVG, all timeframes) | `on_choch` marks records `expired=True`, keeps them in storage | ⚠️ **Partial** — behaviorally equivalent for trade eligibility (expired zones are excluded from `active_fvgs()`/`active_obs()` exactly as deletion would exclude them), but not literally "deleted" | None on trade eligibility; a semantic/audit-trail difference only |
| §5: CHoCH scoped per-symbol | Each `POIManager`/`StructureEngine` instance is already per-symbol | ✅ Exact | — |
| §5: "immediately close all open positions aligned with the invalidated trend" (**H1** CHoCH) | Not implemented at the H1 level at all. `trade_management.should_exit_on_opposite_choch` exists but is wired to the **M1 local engine's** CHoCH in the backtester — never to H1 CHoCH | ❌ **No** — H1-CHoCH-triggered forced closure is entirely missing | Would have mattered had any trade opened; did not affect the zero-trade funnel since no position was ever open to close |

### Module 4 — Entry Confirmation (the most significant divergences)

| Spec (§) | Implementation | Match? | Impact |
|---|---|---|---|
| §16: "**No M1 trend state, no M1 BOS state, no M1 external structure** — M1 CHoCH is a stateless trigger: identify the most recent confirmed M1 swing and test a body close beyond it" | `local_structure_confirms` runs a **full, persistent** `StructureEngine` instance on M1 (the exact same D1–D6 multi-bar trend/BOS/pending-level machinery built for H1), and gates entry on that engine's own `trend` field matching the trade direction | ❌ **No — this is the single largest divergence found.** The spec explicitly forbids exactly the mechanism used | **Severe.** Requiring a persistent, multi-bar M1 BOS-established trend (which itself requires an earlier M1 BOS to have already fired and not yet been invalidated) is architecturally different from — and far more restrictive than — a single stateless per-candle test. It also changes *which bars even enter the funnel at all*: `direction` itself is derived from `m1_ev.trend`, meaning the very first funnel stage already presupposes a persistent M1 trend exists, before any spec-mandated gate is even reached. |
| §16: confirmation = **Absorption (§15)** + M1 CHoCH trigger + displacement ≥1.5×ATR(10) | Absorption (§15) **was never implemented at all** — no code anywhere checks "last 3 M1 candles strictly decreasing body size, average < 50%×ATR, no candle closing beyond the HTF POI" | ❌ **No — an entire required rule is missing**, not simplified | Directionally this makes entry *easier* (one fewer gate) — the opposite direction from the M1-trend-state error above. Net effect on frequency is ambiguous without re-testing, but the *rule itself* is absent, which is a real omission the user should know about regardless of directional effect. |
| §17: Entry price = 50% equilibrium of the **M1** OB+FVG intersection (a third, M1-specific OB/FVG layer distinct from the M15 POI) | Entry price = the confirming M1 candle's own **close**. No M1-specific OB/FVG detection exists anywhere — the "POI" touched is the H1-detected FVG/OB from Module 3 | ❌ **No** — both the price computation rule *and* the underlying M1 OB/FVG layer are missing | High — this changes the entry price, and therefore the SL distance and RR for every downstream calculation |
| §18: SL = below/above the **confirmation swing** (§16's M1 swing) | SL = beyond the **POI zone's far edge** (the H1 zone, not an M1 swing) | ❌ **No** | High — a POI-zone-based SL is a structurally different (and likely wider/differently-shaped) risk distance than a swing-based SL; changes RR directly |
| §19: TP = **try H1 External Swing → Equal High/Low → Opposite Asian Range, in that priority order; use the first one that clears RR≥5; reject only if none of the three do** | TP = whichever of only **two** candidates (H1 pair extreme, opposite Asia level) is numerically **nearer** to entry | ❌ **No — likely the second-largest contributor to the zero-trade result.** "Nearer" is close to the *opposite* of what the spec's fallback logic achieves: the spec tries the most structurally significant (typically farthest/highest-reward) target first and only steps down if RR fails, whereas "always pick the nearer candidate" mechanically suppresses reward and therefore RR. Equal High/Low is also missing entirely as a candidate. | Severe — this most plausibly explains why only 1 of 14 candidates cleared RR≥5 (7%) in the real-data funnel: a rule that actively minimizes the chosen target's distance from entry is working against, not for, an RR≥5 requirement. |
| §9: SL buffer (2 pips/30 pts) | `compute_stop_loss` uses the correct buffer values | ✅ Exact on the number, ❌ on what it's added to (POI edge, not confirmation swing) | See SL row above |

**Assumptions introduced (documented at build time, now checkable
against spec and found wrong)**: E4 (local structure confirmation) —
built as "a legitimate reuse of the frozen StructureEngine," which
directly contradicts §16's explicit prohibition. E7 (nearest-of-2 TP) —
built as a plausible reading of "next HTF external swing vs. opposite
Asia-range side, whichever nearer" from the **plan file's own
paraphrase**, not the actual spec, which has a completely different,
priority-fallback rule (§19). This is the clearest example in the whole
build of a paraphrase silently diverging from the source it was
paraphrasing.

### Module 5 — Trade Decision Engine

| Spec (§) | Implementation | Match? | Impact |
|---|---|---|---|
| §13: Sessions, London 07–10 / NY 13–16 UTC | `in_trading_session` | ✅ Exact | — |
| §23: Max 3 concurrent, 1/symbol, 6% portfolio exposure | `AccountState` gates | ✅ Exact | — |
| §24: Daily loss ≥5% / drawdown ≥10% | Thresholds match (`DAILY_LOSS_LIMIT_PCT`/`MAX_DRAWDOWN_PCT`) | ⚠️ Partial — thresholds correct, but §24's **actions** (force-close all positions, cancel pending orders, and the auto-resume-at-UTC-midnight vs. manual-re-enable distinction) were never implemented; only new-entry blocking exists | Did not affect the zero-trade funnel (no positions ever existed to force-close); a real gap for live use |
| — | **News validation** (`NewsManager.is_blocked`) | Not in spec at all — an **added filter** | None on the funnel (backtest always passed "not blocked") but worth flagging as scope beyond the locked spec |
| Same-candle priority, clarification #9: CHoCH → BOS → POI creation → entry signals | Not explicitly enforced as an ordering rule anywhere in the backtester's per-bar loop | ❌ Not verified/enforced | Unclear — likely low impact since the backtester processes structure before entry checks each bar, but the exact CHoCH-before-BOS-before-POI-before-entry sequencing was never deliberately built to this rule |

### Module 6 — Risk Management

| Spec (§) | Implementation | Match? | Impact |
|---|---|---|---|
| §23: 1% default, **binary step to 2% only when** HTF trend confirmed + sweep confirmed + OB+FVG confluence + entry inside 61.8–78.6% | `select_risk_pct(use_scaled: bool)` — a bare parameter with **no actual condition-checking logic**; the backtester always passes `False` | ❌ **No** — the entire 4-condition rule (and the OTE-band dependency noted in Module 2) is unimplemented | None on trade frequency (position sizing doesn't gate entries), but every trade that *would* occur is under-sized relative to spec whenever the 2% conditions are actually met |
| — | Correlated-exposure limiting (`MAX_TRADES_PER_CURRENCY`) | Not in spec — an **added filter** | Built but **never wired into `trade_decision.qualify_trade`** — dead code as far as the actual decision path is concerned. Zero effect on the funnel either way. |
| — | Spread filter (`spread_filter_blocks`), slippage handling (`is_slippage_acceptable`/`realized_risk_amount`) | Not in spec (spec has no ATR-cost or slippage-tolerance section) — reused existing framework infra as a **reasonable engineering addition**, but | Built, **never called** by the backtester or Trade Decision Engine. Zero effect either way — genuinely dead code relative to the actual pipeline that ran. |

### Module 7 — MT5 Execution

Not exercised by the backtest (backtest simulates fills directly). Spec's
clarification items #6–8, #11 (bid/ask candle source, broker stop/freeze-
level validation, partial-close volume-step rounding, price
normalization) are **not implemented** in `mt5_gateway.py`'s new pending-
order methods — the module implements order placement/retry/poll/cancel
correctly per §-adjacent items, but the specific broker-constraint
clarifications from the follow-up round were not built. Zero impact on
the backtest (which doesn't touch these); real impact for live use.

### Module 8 — Trade Management

| Spec (§) | Implementation | Match? | Impact |
|---|---|---|---|
| §20: 2R→BE (no fixed-pip alt) | `BREAKEVEN_R=2.0`, no alt | ✅ Exact | — |
| §20: 3R close 25%, SL→BE+0.5pips, **begin trailing behind the most recent confirmed M1 swing** | 3R close 25% ✅; **trailing basis is ATR-multiple, not swing-based** ❌; **trailing starts only after 4R** (once `is_runner_trailing` flips True), not at 3R ❌ | ❌ **No**, on two independent points | Would have mattered for any realized trade's exit quality; did not affect whether trades opened |
| §20: 4R close another 25%, remainder runs with continued swing-trailing | 4R close 25% ✅; remainder trails via ATR, not swings (same divergence as above) | ⚠️ Partial | Same as above |
| §21: Contraction Exit (±1 pip touch ×3, <1R, M1 countertrend CHoCH) | **Not implemented at all** — no code anywhere checks this | ❌ **No — entire rule missing** | Would have mattered for realized trades' early-exit behavior; did not affect the zero-trade funnel |
| §22: Re-entry (breakeven exit + valid HTF POI + fresh M1 CHoCH, max 1) | **Not implemented at all** | ❌ **No — entire rule missing** | Same as above |
| — | Opposite-CHoCH exit wired to the **M1 local engine** | Spec's actual CHoCH-driven closure (§5) is an **H1** concept | ❌ Wrong engine, same root cause as Module 4's E4 | Never exercised (0 trades), but would be wrong if it had been |

### Module 9 — Monitoring

Not addressed by the spec at all (the spec is silent on dashboards/
reporting). Built as a reasonable, spec-independent addition. No parity
question applies.

---

## Part 2 — The requested comparison table (Specification → Implementation → Match? → Impact)

| Area | Specification | Implementation | Match? | Impact |
|---|---|---|---|---|
| Liquidity Sweep detection scope | M15 **and** H1 | H1 only | ❌ | High — halves available sweep/POI coverage |
| Liquidity Sweep test itself | Same-candle wick-exceeds + body-closes-back-inside, 1pip/10pt min | Implemented exactly | ✅ | — |
| Premium/Discount range | H1 External Swing Pair Fib | Implemented exactly | ✅ | — |
| Premium/Discount preferred zone (OTE 61.8–78.6%) | Explicit preferred sub-band | Not implemented | ❌ | Medium |
| POI selection (which OB/FVG is "the POI") | M15 POI engine, separate from M1 Entry Zone OB/FVG | Only H1 POIs exist; no M15 or M1-specific OB/FVG layer | ❌ | High |
| Entry price | 50% of M1 OB+FVG intersection | M1 confirmation candle's close | ❌ | High |
| CHoCH confirmation (M1) | Stateless: body close beyond most recent confirmed M1 swing, no M1 trend/BOS state | Full persistent M1 `StructureEngine` (trend/BOS state) gating direction itself | ❌ | **Severe — largest single divergence** |
| RR / TP calculation | Priority-ordered fallback across 3 targets, first to clear RR≥5 wins | Nearest-of-2 candidates, unconditional | ❌ | **Severe — second-largest divergence** |
| Stop Loss basis | Confirmation swing (M1) | POI zone far edge (H1) | ❌ | High |
| RR threshold value | ≥5 | ≥5 | ✅ | — |
| Session filter | London 07–10 / NY 13–16 UTC | Implemented exactly | ✅ | — |
| Risk filters (concurrent/per-symbol/exposure) | 3 / 1 / 6% | Implemented exactly | ✅ | — |
| Risk filters (daily loss/drawdown thresholds) | 5% / 10% | Implemented exactly (values); actions (force-close) missing | ⚠️ | Low (never triggered) |
| Risk % scaling (1%→2%) | Binary, 4-condition rule | Unconditional 1% always | ❌ | Low (sizing only, not frequency) |
| Absorption (§15) | Required pre-condition for M1 confirmation | Not implemented | ❌ | Unknown direction, but a real missing rule |
| Trailing basis | Most recent confirmed M1 swing | ATR multiple | ❌ | Medium (exit quality only) |
| Contraction Exit | Required rule | Not implemented | ❌ | Missing rule |
| Re-entry | Required rule | Not implemented | ❌ | Missing rule |
| CHoCH → POI deletion/position closure (H1) | Delete POIs, force-close aligned positions | POIs "expired" not deleted (behaviorally equivalent); positions never force-closed (wrong engine wired) | ⚠️/❌ | Low on funnel; real gap for live use |
| News validation | Not in spec | Added | N/A (extra) | None (always permissive in backtest) |
| Correlated-exposure limit | Not in spec | Added, but never wired into the decision gate | N/A (extra, dead code) | None |
| Spread/slippage filters | Not in spec | Added, but never wired into the decision gate or fill simulation | N/A (extra, dead code) | None |

---

## Part 3 — Funnel reconstruction (from the real ~10-month M1 backtest, all 4 symbols pooled)

| Gate | Candidates in | Passing | Rejected | Rejection % | Note |
|---|---|---|---|---|---|
| M1 bar-closes total | 1,200,000 | — | — | — | Baseline |
| Has an active H1 pair **and** a persistent M1-engine trend matching a direction | 1,200,000 | 804,962 | 395,038 | 32.9% | ⚠️ This stage is not a spec gate at all — it's an artifact of requiring the M1 `StructureEngine`'s own persistent trend (§16 violation). Under the spec's actual stateless-trigger design, this stage wouldn't exist in this form; direction would come from H1 alone. |
| …has a matching-direction active POI (FVG/OB) | 804,962 | 224,211 | 580,751 | 72.1% | POIs were H1-only (should include M15) — this stage is under-populated relative to spec |
| …POI actually touched | 224,211 | 7,706 | 216,505 | 96.6% | Expected — POI zones are narrow price bands; not itself a red flag |
| …touch lands in the favorable premium/discount half | 7,706 | 875 | 6,831 | 88.6% | Plausible per spec, though the OTE-preferred-band nuance (§10) is missing from this test entirely |
| …M1 entry candle qualifies (displacement + swing-break) | 875 | 14 | 861 | 98.4% | **>90% rejection.** This gate, as built, conflates two things the spec keeps separate: it is gated behind the (non-spec) persistent M1 trend from stage 1 already, AND it omits Absorption (§15) entirely. The displacement+swing-break test itself is a reasonable proxy for §16's stateless CHoCH trigger, but arrives at this stage having already passed through the wrong upstream gate. |
| …RR ≥ 5 | 14 | 1 | 13 | 92.9% | **>90% rejection.** Explained fully by the TP-selection divergence: "nearest of 2 candidates" mechanically minimizes reward, working against an RR≥5 requirement, versus the spec's priority-fallback rule that tries the most favorable target first. |
| …session window (London/NY) | 1 | 0 | 1 | 100% | The sole survivor missed by 31 minutes — not evidence of anything systemic, a single data point |

**Both of the >90%-rejection gates are explained by implementation
divergences identified in Part 1/2, not by inherent strategy
selectivity that matches the locked spec.** The M1-entry-candle gate is
contaminated by the non-spec persistent-trend precondition from stage 1
and missing Absorption; the RR≥5 gate is actively undermined by a TP
selection rule that picks the nearest, lowest-reward candidate instead
of the spec's highest-priority, RR-maximizing fallback sequence.

---

## Part 4 — Answers to the four questions

### 1. Does the implementation faithfully reproduce the original strategy?

**No.** The frozen layer (Swings/Structure/ATR/FVG/Order Block) does,
exactly — it was built with the spec in context and matches it in every
checked respect. Modules 1–9 (built without spec access, from a
paraphrased plan) do not. At least two divergences are severe enough to
independently suppress trade frequency by a large factor: the M1
confirmation mechanism (§16 explicitly prohibited what was built) and
the TP/RR selection rule (§19's priority-fallback replaced by a
nearest-of-2 rule that worked against RR≥5). Two required rules
(Absorption §15, and the M15 half of the POI/liquidity layer §1/§11)
are missing entirely rather than simplified. Two more rules (Contraction
Exit §21, Re-entry §22) are missing outright but never mattered because
no trade ever opened for them to apply to.

### 2. If not, where does it diverge?

In descending order of estimated impact on the zero-trade result:

1. **M1 confirmation mechanism** (Module 4, §16) — built as a persistent,
   multi-bar M1 trend/BOS state machine; spec requires a stateless
   per-candle swing-break test. This also silently redefines what
   "direction" means at the very first funnel stage.
2. **TP/RR selection** (Module 4, §19) — built as "nearest of 2
   candidates"; spec requires a priority-ordered fallback across 3
   candidates, taking the first that clears RR≥5. The built rule
   actively works against clearing a high RR threshold.
3. **POI/liquidity timeframe scope** (Modules 1 & 3, §1/§11) — built
   H1-only; spec requires an M15 POI engine plus M15+H1 liquidity
   detection, and a *third*, separate M1 OB/FVG layer for the Entry Zone
   (§17) that was never built at all.
4. **Stop-loss basis** (Module 4, §18) — built from the POI zone's edge;
   spec requires the M1 confirmation swing.
5. **Absorption** (§15) — entirely missing.
6. Lower-impact, never-exercised-because-zero-trades gaps: trailing
   basis/timing (§20), Contraction Exit (§21), Re-entry (§22), risk %
   scaling (§23), H1-CHoCH-triggered forced closure (§5).
7. Added, non-spec filters that were never wired into the actual
   decision path anyway (news validation, correlated-exposure limiting,
   spread/slippage filters) — zero effect on the result either way.

### 3. Are the zero trades caused by the strategy itself or by implementation decisions?

**Primarily by implementation decisions.** Both gates that individually
rejected >90% of remaining candidates trace directly to build-time
choices that contradict the locked spec, not to genuinely-specified
strategy strictness:

- The M1-entry-candle gate (98.4% rejection) sits downstream of a
  non-spec persistent-trend precondition and is missing a required
  co-gate (Absorption) — its true selectivity under the actual spec
  design is unknown and unmeasured.
- The RR≥5 gate (92.9% rejection) is actively undermined by a TP rule
  that does the opposite of what the spec's fallback logic is designed
  to do (maximize the chance of clearing a high RR bar by trying the
  most favorable target first).

It is not possible to conclude from this data that "the strategy, as
specified, lacks trade frequency" — that claim was never actually
tested. What was tested is a materially different pipeline built under
context loss, with two of its most consequential rules replaced by
weaker or contradictory substitutes.

### 4. Which assumptions must be removed before we can honestly evaluate the strategy?

To evaluate the *actual* locked V4 specification (not this build's
substitute), the following must be corrected before any new backtest is
run:

1. Replace Module 4's M1 confirmation with the spec's stateless test:
   H1 bias sets direction; M1 CHoCH = current candle body closes beyond
   the most recently confirmed M1 swing (no persistent M1 trend/BOS
   state).
2. Add Absorption (§15) as a required co-gate.
3. Replace the TP/RR selection with the spec's priority-ordered
   fallback (H1 External Swing → Equal High/Low → Opposite Asian Range,
   first to clear RR≥5).
4. Move SL basis from the POI zone edge to the confirmation swing (§18).
5. Add the M15 POI engine (FVG/OB detected on M15, not H1) and M15
   liquidity detection (§1, §11), and add the separate M1 OB/FVG layer
   for the Entry Zone (§17).
6. Add the OTE preferred-zone check (§10) if it is to inform risk %
   scaling (§23) as specified.

Only after these are corrected — still with **no threshold changes**
(RR≥5, session windows, and the risk/exposure caps all matched spec
exactly and are not implicated) — would a re-run of
`tools/backtest_strategy_v2.py` (or its corrected successor) be a fair
test of whether the *actual* V4 strategy has an edge. The current
zero-trade result cannot honestly be read as a verdict on the strategy
itself.
