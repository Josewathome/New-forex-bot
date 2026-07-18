# Phase 2B — Engineering Cleanup Validation Report

Pure engineering cleanup: technical-debt extraction + documentation. No
SMC logic, thresholds, or behavior changed. This report is the evidence.

## What changed

- **New**: `src/application/strategy_v2/numerics.py` — `EPSILON`,
  `approx_gte`/`approx_lte`/`approx_eq`, `intersect()`. Extracted from
  three independently-defined copies of `_EPSILON = 1e-9` (in `atr.py`,
  `fvg.py`, `order_blocks.py`) and the duplicated
  `max(lows)`/`min(highs)` intersection math in `fvg.overlap_range` /
  `order_blocks.ob_fvg_confluence`.
- **New**: `src/application/strategy_v2/swing_diagnostics.py` — optional,
  decoupled diagnostics layer (see `STRATEGY_V2_ARCHITECTURE.md`), plus
  `tools/validate_swing_diagnostics.py`.
- **Modified**: `atr.py`, `fvg.py`, `order_blocks.py` now import the
  shared helpers instead of defining their own. Every call site's
  existing comparison semantics were preserved exactly — where a function
  used a strict comparison with no tolerance (`fvg.overlap_range`), it
  still does; where a function used epsilon-tolerant comparisons
  (`is_displacement`, FVG size gate, OB mitigation/invalidation,
  `ob_fvg_confluence`), the tolerance behavior is byte-identical to
  before, just expressed via the shared helper instead of inline
  arithmetic.
- **New docs**: `STRATEGY_V2_ARCHITECTURE.md`, `STRATEGY_V2_REJECTED_RESEARCH.md`,
  this file.
- **Not touched**: `structure.py`, `swings.py`, `config_v2.py`, domain
  entities. No changes at all — the identity checks below confirm this
  independently of that claim.

## Verification 1 — full synthetic regression suites

All 4 existing suites re-run after the refactor, unmodified test code:

| Suite | Assertions | Result |
|---|---|---|
| `validate_structure_engine.py` | 33 | PASS |
| `replay_structure_engine.py` | 8 scenarios | PASS |
| `validate_fvg.py` | 20 | PASS |
| `validate_order_blocks.py` | 20 | PASS |
| `validate_swing_diagnostics.py` (new) | 5 | PASS |

## Verification 2 — real 13-month dataset, byte-for-byte diff against pre-refactor recorded output

This is the stronger proof: not just "the synthetic tests still pass,"
but "the exact same real-world output is produced, event for event."

**Structure engine** (XAUUSD/EURUSD/GBPUSD/USDJPY, full BOS direction +
pair bounds + CHoCH invalidated-trend, per symbol):

| Symbol | BOS | CHoCH | Identical to pre-refactor recording |
|---|---|---|---|
| XAUUSD | 198 | 61 | Yes |
| EURUSD | 240 | 90 | Yes |
| GBPUSD | 200 | 77 | Yes |
| USDJPY | 191 | 62 | Yes |

**FVG** (full zone list per symbol — direction, gap bounds, mitigated,
invalidated flags):

| Symbol | Count | Identical to pre-refactor recording |
|---|---|---|
| XAUUSD | 97 | Yes |
| EURUSD | 81 | Yes |
| GBPUSD | 98 | Yes |
| USDJPY | 79 | Yes |

**Order Blocks** (counts, fallback-depth histogram, invariant violations):

| Symbol | Count | Identical to pre-refactor recording |
|---|---|---|
| XAUUSD | 105 | Yes |
| EURUSD | 95 | Yes |
| GBPUSD | 105 | Yes |
| USDJPY | 93 | Yes |

Total 398, depth histogram `{1:191, 2:113, 3:43, 4:31, 5:8, 6:7, 7:1,
8:4}`, 0 invariant violations — identical to the pre-refactor recording
in every field.

## Conclusion

**Zero behavioral change confirmed** across synthetic tests and the full
real-market dataset. This was pure engineering cleanup. Development may
proceed to Liquidity Sweep implementation.
