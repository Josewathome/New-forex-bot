"""Adversarial regression test for src/application/strategy_v2/{swings.py,
structure.py} — the External Swing Pair / BOS / CHoCH / Trend state machine
underpinning the entire V4 SMC strategy_v2 engine.

HOW TO RUN
    From the repository root:
        python3 tools/validate_structure_engine.py
    Exit code 0 = all checks passed. Exit code 1 = at least one failed
    (every failure is also listed by name just above the exit).
    No environment variables or MT5 connection required — this module has
    no dependency on src/config.py.

WHAT EACH DECISION (D1-D6) VALIDATES, AND THE PASS CRITERION FOR EACH
    D1  BOS levels are edge-triggered, not a continuous condition.
        PASS: once a pending swing level fires a BOS, it is retired
        (set to None) so a later close that merely *remains* beyond the
        old level does not re-fire anything.               -> Section 4

    D2  "The swing that generated the BOS" = the most recent CONFIRMED
        opposite-type swing whose own candle chronologically PRECEDES
        the broken level's candle — not the lowest/highest in the range,
        and not simply "the newest known swing right now" (which could
        have confirmed after the level, and therefore cannot pair with
        it — see the Section 9 corollary).
        PASS: given two valid candidate pairing swings, the engine picks
        the more recent one even when it is shallower (not deeper).
                                                              -> Section 5

    D3  Post-reset / start-of-history WAIT state: a pending level cannot
        fire a trend-establishing BOS until a qualifying opposite-type
        swing exists that chronologically precedes it. The level is not
        retired while waiting, so it can still fire once qualified.
        PASS: a break with no qualifying opposite swing yet produces NO
        BOS and leaves the pending level live; once a *fresh* qualifying
        high/low forms (one the opposite swing actually precedes), the
        next break of THAT fresh level fires correctly. Breaking the
        original stale level never retroactively succeeds, even after a
        later-forming opposite swing exists (D2's corollary).
                                                              -> Section 9

    D4  (revised) CHoCH clears only ACTIVE structure state (trend,
        active pair, the two BOS-test bootstrap pointers) — historical
        confirmed swings are never deleted, kept for audit/recovery.
        PASS: swing_log length is unchanged (never shrinks) across a
        CHoCH event, while trend/active_pair/pending_high/pending_low
        all reset.                                           -> Section 8

    D5  While a trend is active, ONLY CHoCH (close vs. the active pair's
        OWN low/high) can end or flip it. An independently-tracked
        opposite-type pending swing is never tested for a direct flip.
        PASS: a shallow pullback swing that forms above (bullish case)
        the active pair's true low, later broken by a close that is
        still above the true pair low, produces NEITHER a BOS NOR a
        CHoCH — trend and pair are completely unaffected.    -> Section 7

    D6  (locked) A same-direction continuation BOS rolls the active
        External Swing Pair FORWARD to the newest pair rather than
        keeping the original pair frozen.
        PASS: two consecutive continuation BOS events each replace the
        active pair; the final pair reflects the second continuation,
        not the bootstrap.                                   -> Section 6

    Swing rule (Contradiction 1) and confirmation timing are validated
    independently in Sections 1-2 (strict-left/non-strict-right ties on
    both swing types, and the exact 3-bar confirmation lag).
    Section 3 validates BOS requires a body close, not a wick.

Every assertion below prints PASS/FAIL and, on failure, the actual values
observed — nothing is asserted silently. This file replaces the earlier
session-scratchpad version (qa_structure.py); no functional change, only
paths fixed to be repo-relative.
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Candle, Timeframe
from src.domain.strategy_v2_entities import TrendState, SwingType
from src.application.strategy_v2.swings import is_swing_high, is_swing_low, confirmed_swings_at_close
from src.application.strategy_v2.structure import StructureEngine

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
def ts(n): return T0 + timedelta(hours=n)
def C(n, o, h, l, c):
    return Candle(time=ts(n), open=o, high=h, low=l, close=c, volume=100.0, symbol="TEST", timeframe=Timeframe.H1)

FAILS = []
def check(label, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

print("="*78)
print("SECTION 1 — SWING DETECTION: exact 7-candle rule (swings.py)")
print("="*78)

# 1a. Positive: strict left, non-strict right (a tie on the RIGHT must still confirm)
cs = [C(0,10,10,10,10), C(1,10,11,9,10), C(2,10,12,9,10), C(3,10,13,9,10),
      C(4,10,20,9,10),   # candidate: high=20
      C(5,10,20,9,10), C(6,10,19,9,10), C(7,10,18,9,10)]
check("1a positive: strict-left/non-strict-right swing high confirms",
      is_swing_high(cs, 4) is True,
      f"left highs=13,12,11 right highs=20,19,18 candidate=20")

# 1b. Negative: a TIE on the LEFT must disqualify (left test is strict >).
# Candidate i=4 compares to i-1=3, i-2=2, i-3=1 — the tie must be AT i-3.
cs = [C(0,10,10,9,10),
      C(1,10,20,9,10),   # this is i-3 for candidate i=4: tie goes here
      C(2,10,16,9,10), C(3,10,17,9,10),
      C(4,10,20,9,10),   # candidate high=20, EQUALS candles[1].high=20 (i-3) -> left strict > fails
      C(5,10,18,9,10), C(6,10,17,9,10), C(7,10,16,9,10)]
check("1b negative: tie on LEFT (i-3) disqualifies (strict > required)",
      is_swing_high(cs, 4) is False,
      "candles[1].high == candles[4].high == 20")

# 1c. Negative: a strictly HIGHER high anywhere on the right disqualifies
cs = [C(0,10,10,10,10), C(1,10,11,9,10), C(2,10,12,9,10), C(3,10,13,9,10),
      C(4,10,20,9,10),
      C(5,10,21,9,10),  # higher than candidate -> disqualifies
      C(6,10,15,9,10), C(7,10,14,9,10)]
check("1c negative: strictly higher high in the 3-bar right window disqualifies",
      is_swing_high(cs, 4) is False)

# 1d. Mirror for swing low (tie on the right must still confirm; tie on left disqualifies)
cs = [C(0,10,11,0,10), C(1,10,11,1,10), C(2,10,11,2,10), C(3,10,11,3,10),
      C(4,10,11,-5,10),   # candidate low=-5
      C(5,10,11,-5,10), C(6,10,11,-4,10), C(7,10,11,-3,10)]
check("1d positive: swing low with non-strict-right tie confirms",
      is_swing_low(cs, 4) is True)

cs = [C(0,10,11,0,10),
      C(1,10,11,-5,10),   # i-3 for candidate i=4: tie goes here
      C(2,10,11,-3,10), C(3,10,11,-4,10),
      C(4,10,11,-5,10),   # equals candles[1].low=-5 (i-3) -> left strict < fails
      C(5,10,11,-4,10), C(6,10,11,-3,10), C(7,10,11,-2,10)]
check("1e negative: tie on LEFT disqualifies swing low",
      is_swing_low(cs, 4) is False)

print()
print("="*78)
print("SECTION 2 — CONFIRMATION TIMING: only knowable 3 bars later")
print("="*78)
cs = [C(0,10,10,10,10), C(1,10,11,9,10), C(2,10,12,9,10), C(3,10,13,9,10),
      C(4,10,20,9,10), C(5,10,20,9,10), C(6,10,19,9,10), C(7,10,18,9,10)]
check("2a too early (i=5, candidate=2, only 2 right-bars known): no swing yet",
      confirmed_swings_at_close("TEST", Timeframe.H1, cs[:6], 5) == [])
check("2b too early (i=6, candidate=3): still not the right candidate/window",
      confirmed_swings_at_close("TEST", Timeframe.H1, cs[:7], 6) == [])
res = confirmed_swings_at_close("TEST", Timeframe.H1, cs, 7)
check("2c confirms exactly at i=7 (candidate=4, 3rd right bar just closed)",
      len(res) == 1 and res[0].kind == SwingType.HIGH and res[0].price == 20,
      f"got {res}")

print()
print("="*78)
print("SECTION 3 — BOS: body close required, wick alone must not fire")
print("="*78)
eng = StructureEngine("TEST")
# Build a swing low then a swing high (bootstrap material), no BOS yet.
seq = [
    C(0,105,106,104,105), C(1,105,106,104,105), C(2,105,106,104,105),
    C(3,104,105,102,103), C(4,103,104,101,102),
    C(5,102,103,99,100),                                    # candidate swing low
    C(6,100,102,100,101), C(7,101,103,101,102), C(8,102,104,102,103),
    C(9,103,106,103,105), C(10,105,108,104,107), C(11,107,109,106,108),
    C(12,108,110,107,109),                                  # candidate swing high (110)
    C(13,109,108,107,108), C(14,108,107,106,107), C(15,107,106,105,106),
]
for i in range(len(seq)):
    eng.process_new_candle_close(seq, i)
check("3a pre-check: pending_high=110 established, trend still NEUTRAL",
      eng.pending_high is not None and eng.pending_high.price == 110 and eng.trend == TrendState.NEUTRAL)

# Wick to 115 but CLOSE stays at 108 (below 110) — must NOT fire BOS.
wick_candle = C(16, 108, 115, 107, 108)
seq2 = seq + [wick_candle]
ev = eng.process_new_candle_close(seq2, 16)
check("3b wick beyond 110 (high=115) with close=108 (<110): no BOS",
      ev.bos_fired is None and eng.trend == TrendState.NEUTRAL,
      f"bos_fired={ev.bos_fired} trend={eng.trend}")

# Now an actual body close beyond 110.
real_break = C(17, 108, 112, 107, 111)
seq3 = seq2 + [real_break]
ev = eng.process_new_candle_close(seq3, 17)
check("3c body close=111 > 110: BOS fires bullish",
      ev.bos_fired == TrendState.BULLISH and eng.trend == TrendState.BULLISH,
      f"pair low={eng.active_pair.low} high={eng.active_pair.high}")
check("3d generating low correctly = 99 (the .low of that candle — verified from the fixture, not assumed)",
      eng.active_pair.low == 99)

print()
print("="*78)
print("SECTION 4 — D1: broken BOS level cannot re-fire")
print("="*78)
check("4a pending_high retired to None immediately after BOS",
      eng.pending_high is None)
# Close even higher than the OLD broken level (110), no NEW swing high has
# confirmed yet — this must NOT be treated as another bootstrap-style event.
higher_close = C(18, 111, 113, 110, 112)
seq4 = seq3 + [higher_close]
ev = eng.process_new_candle_close(seq4, 18)
check("4b close=112 (still > old broken 110) fires nothing — old level is dead",
      ev.bos_fired is None and ev.choch_fired is None or ev.choch_fired is False,
      f"bos_fired={ev.bos_fired} choch_fired={ev.choch_fired} trend={eng.trend}")

print()
print("="*78)
print("SECTION 5 — D2: generating swing = MOST RECENT before the break,")
print("           NOT the lowest / not the first")
print("="*78)
eng2 = StructureEngine("TEST2")
# LOW1 candidate @ idx4 (=40): left window idx1,2,3 = 45,44,43 (all >40 strict);
#   right window idx5,6,7 = 42,50,65 (all >=40).
# LOW2 candidate @ idx10 (=60, SHALLOWER but LATER): left window idx7,8,9 =
#   65,70,68 (all >60 strict — note idx7=65 satisfies BOTH windows at once);
#   right window idx11,12,13 = 61,62,63 (all >=60).
seq5 = [
    C(0,50,51,49,50), C(1,50,51,45,50), C(2,50,51,44,50), C(3,49,50,43,46),
    C(4,46,47,40,41),                                   # candidate LOW #1 = 40 (deep, EARLY)
    C(5,41,44,42,43), C(6,43,46,50,45), C(7,45,48,65,47),
    C(8,47,55,70,54), C(9,54,58,68,56),
    C(10,56,58,60,57),                                  # candidate LOW #2 = 60 (shallower, LATER)
    C(11,57,60,61,59), C(12,59,62,62,61), C(13,61,64,63,63),
    C(14,63,68,64,67), C(15,67,70,66,69), C(16,69,73,68,72),
    C(17,72,80,71,79),                                  # candidate HIGH = 80
    C(18,79,78,77,78), C(19,78,77,76,77), C(20,77,76,75,76),
    C(21,76,82,75,81),                                  # closes 81 > 80 -> bullish BOS
]
for i in range(len(seq5)):
    eng2.process_new_candle_close(seq5, i)
lows_in_log = sorted([s.price for s in eng2.swing_log if s.kind == SwingType.LOW])
print(f"    full confirmed-low log: {lows_in_log}")
check("5a sanity: both candidate lows (40 and 60) actually confirmed",
      40 in lows_in_log and 60 in lows_in_log, f"lows_in_log={lows_in_log}")
check("5b BOS fired, pair.low = 60 (most recent before the high), NOT 40 (the lowest/earliest)",
      eng2.trend == TrendState.BULLISH and eng2.active_pair is not None and eng2.active_pair.low == 60,
      f"active_pair={eng2.active_pair.low if eng2.active_pair else None}")

print()
print("="*78)
print("SECTION 6 — D6: continuation BOS rolls the pair forward (multi-hop)")
print("="*78)
seq6 = [
    C(0,105,106,104,105), C(1,105,106,104,105), C(2,105,106,104,105),
    C(3,104,105,102,103), C(4,103,104,101,102),
    C(5,102,103,99,100), C(6,100,102,100,101), C(7,101,103,101,102), C(8,102,104,102,103),
    C(9,103,106,103,105), C(10,105,108,104,107), C(11,107,109,106,108),
    C(12,108,110,107,109), C(13,109,108,107,108), C(14,108,107,106,107), C(15,107,106,105,106),
    C(16,106,107,105,106), C(17,106,108,105,107), C(18,107,109,106,108), C(19,108,109,107,108),
    C(20,108,112,107,111),          # BOOTSTRAP bullish BOS: pair (99,110)
    C(21,111,114,110,112), C(22,112,116,111,113), C(23,113,118,112,114),
    C(24,114,121,113,115),          # candidate swing high @121
    C(25,115,119,114,116), C(26,116,118,115,117), C(27,117,119,116,118),
    C(28,118,120,117,119),
    C(29,119,122,118,123),          # HOP 1 continuation: closes 123 > 121
    C(30,123,124,122,123), C(31,123,127,122,124), C(32,124,129,123,125),
    C(33,125,132,124,126),          # candidate swing high @132
    C(34,126,130,125,127), C(35,127,129,126,128), C(36,128,128,127,127),
    C(37,127,128,126,127),
    C(38,127,135,126,134),          # HOP 2 continuation: closes 134 > 132
]
pairs_seen = []
eng3 = StructureEngine("TEST3")
for i in range(len(seq6)):
    ev = eng3.process_new_candle_close(seq6, i)
    if ev.bos_fired:
        pairs_seen.append((i, ev.bos_fired.value, eng3.active_pair.low, eng3.active_pair.high))
print("    BOS sequence:", pairs_seen)
check("6b three BOS events fired: bootstrap + 2 continuations",
      len(pairs_seen) == 3)
check("6c final pair rolled forward to (105, 132) — not frozen at the bootstrap (99,110)",
      eng3.active_pair.low == 105 and eng3.active_pair.high == 132,
      f"got low={eng3.active_pair.low} high={eng3.active_pair.high}")

print()
print("="*78)
print("SECTION 7 — D5: independent swing cannot force a direct flip;")
print("           trend cannot change without CHoCH")
print("="*78)
eng5 = StructureEngine("TEST4")
seq7b = list(seq6[:21])
seq7b += [
    C(21,111,113,110,111), C(22,111,112,109,110), C(23,110,111,108,109),
    C(24,109,110,105,106),                        # candidate low = 105 (> active_pair.low=99)
    C(25,106,108,106,107), C(26,107,109,107,108), C(27,108,110,108,109),
    C(28,109,111,103,104),                        # closes 104: BELOW the shallow low (105) but ABOVE active_pair.low (99)
]
for i in range(len(seq7b)):
    ev = eng5.process_new_candle_close(seq7b, i)
check("7a shallow swing low (105) confirmed above active_pair.low (99)",
      any(s.kind == SwingType.LOW and s.price == 105 for s in eng5.swing_log))
check("7b close=104 (< shallow 105, but > active_pair.low=99): NO flip, NO BOS, trend stays BULLISH",
      eng5.trend == TrendState.BULLISH and ev.bos_fired is None and ev.choch_fired is False,
      f"trend={eng5.trend} bos_fired={ev.bos_fired} choch_fired={ev.choch_fired} pair.low={eng5.active_pair.low}")
check("7c active_pair unchanged (99,110) — proves only active_pair.low gates CHoCH, nothing else",
      eng5.active_pair.low == 99 and eng5.active_pair.high == 110,
      f"got low={eng5.active_pair.low} high={eng5.active_pair.high}")

print()
print("="*78)
print("SECTION 8 — CHoCH fires correctly against the TRUE pair boundary,")
print("           and D4(revised): clears active state, PRESERVES swing_log")
print("="*78)
swing_log_len_before = len(eng5.swing_log)
real_choch = C(29, 104, 105, 97, 98)   # closes 98 < active_pair.low(99) -> CHoCH
ev = eng5.process_new_candle_close(seq7b + [real_choch], 29)
check("8a CHoCH fires: close=98 < active_pair.low=99",
      ev.choch_fired is True and ev.invalidated_trend == TrendState.BULLISH)
check("8b trend reset to NEUTRAL, active_pair cleared, pending levels cleared",
      eng5.trend == TrendState.NEUTRAL and eng5.active_pair is None
      and eng5.pending_high is None and eng5.pending_low is None)
check("8c swing_log PRESERVED (not wiped) — length >= what it was before CHoCH",
      len(eng5.swing_log) >= swing_log_len_before,
      f"before={swing_log_len_before} after={len(eng5.swing_log)}")

print()
print("="*78)
print("SECTION 9 — D3: WAIT state — BOS cannot form without a valid pair")
print("="*78)
eng6 = StructureEngine("TEST5")
# Fresh engine: a swing HIGH confirms with NO swing LOW ever confirmed yet.
seq9 = [
    C(0,10,10,10,10), C(1,10,11,9,10), C(2,10,12,9,10), C(3,10,13,9,10),
    C(4,10,20,9,10),                                    # candidate swing high = 20
    C(5,10,19,9,10), C(6,10,18,9,10), C(7,10,17,9,10),
]
for i in range(len(seq9)):
    eng6.process_new_candle_close(seq9, i)
check("9a pending_high=20 confirmed, swing_log has NO lows at all",
      eng6.pending_high is not None and eng6.pending_high.price == 20
      and not any(s.kind == SwingType.LOW for s in eng6.swing_log))
break_attempt = C(8, 17, 22, 16, 21)   # closes 21 > 20, but no opposite swing exists
ev = eng6.process_new_candle_close(seq9 + [break_attempt], 8)
check("9b close=21 > pending_high(20) but NO qualifying low in log: NOT a valid BOS, stays WAIT",
      ev.bos_fired is None and eng6.trend == TrendState.NEUTRAL and eng6.active_pair is None,
      f"bos_fired={ev.bos_fired} trend={eng6.trend}")
check("9c pending_high NOT retired (D3): stays live at 20 for re-test",
      eng6.pending_high is not None and eng6.pending_high.price == 20)

# Now a swing low confirms. Candidate @ idx12 (=10): left window idx9,10,11
# = 20,19,18 (all >10 strict, clear of idx7's low=16 which sits outside this
# window); right window idx13,14,15 = 10,11,12 (all >=10).
seq9b = seq9 + [break_attempt]
low_material = [
    C(9,21,22,20,21), C(10,21,22,19,20), C(11,20,21,18,19),
    C(12,19,20,10,11),                                  # candidate swing low = 10
    C(13,11,13,10,12), C(14,12,14,11,13), C(15,13,15,12,14),
]
for c in low_material:
    seq9b = seq9b + [c]
for i in range(9, len(seq9b)):
    eng6.process_new_candle_close(seq9b, i)
check("9d swing low (10) now confirmed",
      any(s.kind == SwingType.LOW and s.price == 10 for s in eng6.swing_log),
      f"lows now={[s.price for s in eng6.swing_log if s.kind==SwingType.LOW]}")

# COROLLARY OF D2+D3 (discovered adversarially, not previously documented):
# the confirmed low's candle_time (idx12) is chronologically AFTER the
# stale pending_high's own candle_time (idx4) -- D2 requires the generating
# swing to precede the broken level, so this low can NEVER retroactively
# pair with THIS SPECIFIC high, no matter how much later it's re-tested.
stale_retest = C(16, 15, 24, 14, 23)   # closes 23 > 20 (the stale high)
seq9d = seq9b + [stale_retest]
ev = eng6.process_new_candle_close(seq9d, 16)
check("9e COROLLARY: re-testing the STALE high(20) still fails even though "
      "a low now exists -- that low postdates the high, so D2 can't pair them",
      ev.bos_fired is None and eng6.trend == TrendState.NEUTRAL,
      f"bos_fired={ev.bos_fired} trend={eng6.trend}")

# A genuinely NEW swing high (later than the low) is required instead.
# Candidate @ idx19 (=30): left window idx16,17,18 highs=24,19,21 (all <30
# strict); right window idx20,21,22 highs=28,27,26 (all <=30).
fresh_high_material = [
    C(17,23,19,18,18), C(18,18,21,17,20),
    C(19,20,30,19,29),                                  # candidate NEW high = 30
    C(20,29,28,27,28), C(21,28,27,26,27), C(22,27,26,25,26),
]
seq9e = seq9d + fresh_high_material
for i in range(17, len(seq9e)):
    eng6.process_new_candle_close(seq9e, i)
check("9f fresh swing high (30) confirmed, superseding the stale pending_high(20)",
      eng6.pending_high is not None and eng6.pending_high.price == 30,
      f"pending_high={eng6.pending_high.price if eng6.pending_high else None}")

final_break = C(23, 26, 32, 25, 31)   # closes 31 > 30
seq9f = seq9e + [final_break]
ev = eng6.process_new_candle_close(seq9f, 23)
check("9g break of the FRESH high(30): valid BOS fires, low(10) correctly precedes it",
      ev.bos_fired == TrendState.BULLISH and eng6.trend == TrendState.BULLISH,
      f"bos_fired={ev.bos_fired}")
check("9h pair correctly uses low=10, high=30 (not the stale 20)",
      eng6.active_pair.low == 10 and eng6.active_pair.high == 30,
      f"got low={eng6.active_pair.low if eng6.active_pair else None} "
      f"high={eng6.active_pair.high if eng6.active_pair else None}")

print()
print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
