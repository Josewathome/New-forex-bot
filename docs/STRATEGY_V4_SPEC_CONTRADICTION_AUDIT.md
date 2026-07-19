# V4 Specification — Contradiction & Feasibility Audit

**No code changed. This audit treats the implementation as frozen and
correct against the locked spec, and critiques the spec text itself.**

Scope: the verbatim "SMC Strategy — Final Specification (V4) & Readiness
Verdict" (transcript line 2620) plus the user's 12-item "Final
Clarifications Before Implementation" (transcript line 2627) — the same
two documents `STRATEGY_V2_IMPLEMENTATION_PARITY_AUDIT.md` used as the
binding spec. Quotes below are verbatim from those two documents.

## Headline finding

The spec contains one real, load-bearing logical gap — not a strict "A
and not-A" contradiction, but a conflation of two different concepts
(*temporal recency* and *price relevance*) that, combined with a
reactive-only invalidation model, makes the Entry Zone / Stop-Loss /
CHoCH-trigger geometry effectively unachievable in real market data. This
single gap is responsible for the corrected implementation's zero-trade
result (`STRATEGY_V2_FINAL_EVALUATION.md`), independent of any threshold
value. Three smaller findings are secondary or editorial.

---

## Finding 1 (primary): "most recent" is conflated with "price-relevant," and nothing requires the two to coincide

### The chain of spec text

> **§16, M1 Confirmation**: "No M1 trend state, no M1 BOS state, no M1
> external structure — M1 CHoCH is a stateless trigger: identify the
> **most recent confirmed M1 swing** (§2, same universal rule) and test a
> body close beyond it."

> **§17, Entry**: "**Most recent M1 OB or FVG.** If OB+FVG overlap
> exists: Entry Zone = intersection of the OB range and FVG range;
> Equilibrium Entry = 50% of that intersection. If no overlap: use the
> deeper discount/premium structure."

> **§18, Stop Loss**: "Below/above **the confirmation swing (§16)**.
> Buffer: 2 pips forex, 0.02 JPY, 30 points/3 pips XAUUSD (§9)."

> **§6, FVG**: "Mitigated FVGs are untradeable." Mitigation = a wick
> touching the 50% midpoint; **invalidation** = a body close beyond the
> far side. Absent either event, an FVG sits in the active pool forever.

> **§8, Order Block**: "Mitigated on first touch; cannot be retraded
> after. Invalidated on an opposite-side body close." Same reactive-only
> model.

### Why this is a real defect, not just an implementation detail

Both `§16`'s CHoCH-trigger swing and `§17`'s Entry Zone are selected by
**pure temporal recency** ("most recent confirmed M1 swing," "most recent
M1 OB or FVG"). Neither selection rule has any price-distance or
price-relevance qualifier attached to it *in the spec text*. That would be
harmless if recency implied relevance — but `§6`/`§8` make mitigation and
invalidation **purely reactive**: an OB or FVG only leaves the active
pool when price *actually returns* to touch or blow through it. If price
runs away and never comes back, that zone stays "the most recent" active
one indefinitely, regardless of how far away current price has moved.

The consequence: `§16`'s swing and `§17`'s OB/FVG are two **independently
time-ordered** reference points, with **no stated requirement that they
be geometrically close to each other, to the just-touched HTF POI, or to
current price**. The spec never asks "is this OB/FVG anywhere near the
swing that's about to trigger the trade?" — it only asks "is this the
most recently created one that hasn't been invalidated yet?"

This single gap produces **two distinct, compounding failure modes**,
not just one:

**1a. The Entry Zone frequently doesn't exist within any workable
distance.** Measured directly from the real ~10-month, 4-symbol dataset
(the same data behind `STRATEGY_V2_FINAL_EVALUATION.md`): of the **18**
real moments where Absorption + CHoCH-trigger + displacement (§15/§16)
all held simultaneously, the *unbounded-nearest* available M1 OB/FVG —
computed with no distance limit, genuinely whichever was geometrically
closest — sat between **4.4× and 24.9× the M1 ATR(10)** away from the
triggering swing, in every single case:

| Symbol | Confirmed cases | Nearest zone distance, in M1-ATR(10) multiples |
|---|---|---|
| XAUUSD | 5 | 5.5×, 6.5×, 9.6×, 12.1×, 13.6× |
| EURUSD | 2 | 12.9×, 17.1× |
| GBPUSD | 2 | 6.6×, 14.1× |
| USDJPY | 9 | 4.4×, 4.8×, 5.3×, 6.6×, 7.9×, 13.5×, 14.3×, 19.6×, 24.9× |

0 of 18 have a candidate anywhere close to "the entry zone for this
reversal" under any reasonable reading of §17's intent.

**1b. Even where a nearby zone did exist, SL and Entry can land on the
wrong sides of each other.** `§18` anchors SL to the CHoCH-trigger swing
±2-3 pips — i.e., SL sits *very close to the swing's own price*, not
to the entry zone. `§17`'s Entry Zone (a discount/premium OB or FVG) is,
by the strategy's own mean-reversion logic, expected to sit *away from*
that swing (the swing is where the preceding micro-move reversed; the
entry zone is the pullback level the trade is buying/selling into). For
a bullish setup, that means: SL (swing − small buffer) can easily land
**above** Entry (a discount-zone price meaningfully below the swing) —
inverted risk geometry for a buy. This isn't hypothetical: the frozen
implementation carries an explicit safety gate for exactly this case
(`entry_confirmation.py`'s "Safety gate" comment, unit-tested as case 9n),
because real data produced it. `§18` gives no rule for what should happen
when this occurs — the spec is silent on the failure mode entirely.

**1c. RR≥5 becomes harder to clear precisely because of the same
coupling gap.** Risk = |Entry − SL| ≈ |Entry − Swing| (since SL is swing
± a fixed small buffer per §18). When the swing and the entry zone are
decoupled (1a), risk is inflated by construction, so `§19`'s RR≥5 bar
requires an even larger reward to clear. Fixing the coupling gap would
also make RR≥5 more achievable, as a side effect — this is not an
independent contradiction, but confirms 1a/1b/1c share one root cause
rather than being three unrelated problems.

### A note on the frozen implementation itself, since it's directly relevant here

The frozen implementation does **not** literally follow §17's text.
`_select_zone_near_swing` in `entry_confirmation.py` selects the OB/FVG
**closest in price** to the confirmation swing, with a distance cutoff
(`config_v2.MAX_ENTRY_ZONE_DISTANCE_ATR = 3.0`) — not "the most recent"
as §17 literally states. This was a deliberate, user-approved
implementation decision (documented in that file's own comments), made
*because* the literal "most recent" reading was tested first and found
to select even more nonsensical, farther-away zones purely by virtue of
creation order. In other words: **the frozen implementation already
contains a hand-built workaround for this exact spec gap.** That
workaround is a reasonable patch at the code level, but it invented a
threshold (3× ATR) that the spec itself never specifies or implies —
which is precisely why this audit does not treat "add a distance cutoff"
as a spec-level fix (see "What this audit does not recommend," below).

---

## Finding 2 (secondary, compounding): §5 clears POIs on CHoCH but is silent on the M1 swing log

> **§5**: "On CHoCH: cancel all pending orders, **delete all active POIs
> (both OB and FVG, all timeframes)**, immediately close all open
> positions aligned with the invalidated trend."

This sentence names POIs (OB/FVG) explicitly. It says nothing about the
raw confirmed-swing log that `§16` draws "the most recently confirmed M1
swing" from. Read literally, `§2`'s swing log is never pruned or reset by
a CHoCH at all — it simply accumulates forever.

This creates a **lifecycle asymmetry** that compounds Finding 1
specifically at the moment a new trend is confirmed: immediately after
an H1 CHoCH, the M1 OB/FVG pool is wiped clean (empty, per §5) and must
rebuild from nothing, while the M1 swing-log candidate pool for §16's
CHoCH-trigger is unaffected and can still point to a swing from well
before the CHoCH, or from a prior trend regime entirely. Right when
trading is meant to resume (a fresh BOS, new trend), the two anchors are
maximally likely to be decoupled — one is brand-new, the other can be
arbitrarily old.

This is not a contradiction on its own (nothing in §5 or §16 is mutually
exclusive), but it is a genuine silence that sharpens Finding 1's
practical severity, and any fix to Finding 1 should account for it (see
the proposed amendment below, which resolves both with one change).

---

## Finding 3 (editorial, no behavioral impact): a mislabeled cross-reference

> **§11**: "Previous Day High/Low (tracked for sweep detection; **not** a
> TP target — TP priority explicitly lists only 3 target types, **§14**,
> so this omission reads as intentional)."

The actual TP-priority rule with exactly 3 target types is `§19` ("H1
External Swing → Equal High/Low → Opposite Asian Range"), not `§14`
(which is "POI Priority / Trade Priority," a different 3-tier scheme
about OB+FVG overlap / discount depth / sweep penetration, reused for POI
selection and cross-symbol tie-breaking — unrelated to TP target types).
This is a wrong section number in the spec's own internal cross-reference
— purely editorial, does not change any rule's behavior, but worth fixing
so a future reader doesn't chase the wrong section trying to verify this
point.

---

## Finding 4 (documentation completeness, minor): V4's own text never restates what gates on session hours

> **§13**: "Fixed UTC, no DST. Asian 00:00–06:00. London 07:00–10:00. New
> York 13:00–16:00."

V4 declares itself "the complete, regenerated specification — not a
diff" (line 3 of the document). Yet §13, as written, only *defines* the
three session windows — it never states that entries are gated by them.
That gating rule ("Fail: outside session → do not proceed to
confirmation/entry... the setup remains 'pending'... assuming the
POI/sweep haven't expired — expiry itself is unresolved") exists only in
an *earlier* draft (the "Revision 2" blueprint, transcript line 2599),
which V4 does not repeat. Two things follow from this:

- The session-gating rule the implementation enforces (`in_trading_
  session`, gating confirmation+entry only) is inherited from a
  document V4 superseded, not restated in the "complete" V4 text itself
  — a real completeness gap, even though the inherited rule is almost
  certainly what was intended.
- That earlier draft *also* explicitly flagged as unresolved: how long a
  POI touch or liquidity sweep remains "live" while waiting for the
  session window to open (or for M1 confirmation generally). V4 never
  resolves this either. The frozen implementation's `max_touch_age`/
  `max_sweep_age` (6 hours) are therefore flagged assumptions filling a
  gap that has existed, unresolved, since before V4 — not a new one this
  build introduced.

Low severity: this did not drive the zero-trade result (the funnel never
reaches the session-window gate in the corrected implementation — it's
rejected earlier, at Finding 1's gate, 18 times out of 18). Worth closing
for documentation completeness regardless.

---

## Minimum specification changes required

Ordered by how much of the problem each closes. All are textual
amendments to the spec; none require inventing a new numeric threshold
not implied by the strategy's own philosophy.

**1. Amend §16 and §17: scope "most recent" to "most recent *since the
current POI touch*," not "most recent, unbounded."**

> §16 (amended): "...identify the most recent confirmed M1 swing **formed
> at or after the current HTF POI touch** and test a body close beyond
> it."
> §17 (amended): "Most recent M1 OB or FVG **formed at or after the
> current HTF POI touch**. If OB+FVG overlap exists: ..."

This is the single highest-leverage change. It directly ties both
anchors to the same causal episode (sweep → POI touch → local reversal →
confirmation) that the strategy's own philosophy already describes as
one continuous event, rather than to independent, unbounded history. It
requires no invented distance constant (unlike the frozen
implementation's 3×-ATR patch) — it uses an event the pipeline already
detects (the POI touch) as the natural scope boundary. It resolves
Finding 1a directly (a stale zone from long before the touch can no
longer be "most recent" once scoped) and Finding 1b as a side effect (a
swing and a zone that both formed during the same short post-touch
window are, by construction, far more likely to sit in a coherent price
relationship to each other than two independently-unbounded "most
recent" picks).

**2. Amend §5 to explicitly state whether the M1 swing log is also
scoped/reset on CHoCH.**

> §5 (amended, appended sentence): "The confirmed-swing log used for
> M1 CHoCH-trigger identification (§16) is not deleted on CHoCH, but
> candidates from before the most recent H1 CHoCH are not eligible as a
> confirmation-trigger swing."

This closes Finding 2's asymmetry explicitly, and is largely subsumed by
amendment 1 if amendment 1 is adopted (scoping to "since the touch" is a
tighter bound than "since the last CHoCH" in practice) — include it
mainly for clarity/robustness in the edge case where a POI touch occurs
long after the most recent CHoCH but the swing log still contains
pre-CHoCH entries no "since touch" filter would otherwise catch if the
touch-scoping were ever loosened later.

**3. Fix the §11 cross-reference from "§14" to "§19."** Editorial only,
zero behavioral effect, but should be corrected so the spec is internally
navigable.

**4. Add one explicit sentence to §13 (or §18) stating that entries are
gated by session hours and specifying what happens to a POI
touch/sweep that occurs outside session hours while waiting for the
window to open** (a maximum "pending" age, even if generously long).
This formalizes what V4 currently only inherits by omission from an
earlier draft, and gives the still-unresolved touch/sweep-expiry
question (flagged since the pre-V4 blueprint) an actual answer instead of
leaving it to each implementation's own judgment.

## What this audit does not recommend

- **Does not recommend formalizing an arbitrary ATR-multiple (or pip)
  distance cutoff as the spec-level fix**, even though that is what the
  frozen implementation currently does. A cutoff is a reasonable
  *code-level* safety net, but it invents a magic number the strategy's
  own logic doesn't derive. Amendment 1 (scope to "since the touch")
  achieves the same practical effect using a boundary the pipeline
  already has for free, and is more faithful to the original "one
  continuous sweep→touch→confirmation episode" philosophy than a
  threshold picked by trial and error.
- **Does not recommend loosening RR≥5, the session windows, or any of
  the risk/exposure caps.** Those all matched the spec exactly and were
  never implicated by this audit or the prior one.
- **Does not recommend redesigning §14/§19's priority-fallback logic** —
  those rules are internally consistent and were not found to
  contradict anything else.

## Caveat

These amendments are proposed as the **minimum textual change set**, not
as a verified fix. Per this task's instructions, no code was changed and
no re-test was run against the amended wording — whether "scope to since
the touch" actually restores a non-trivial trade frequency in real data
is a testable hypothesis, not a demonstrated result. That test would be
the natural next step if these spec amendments are accepted.
