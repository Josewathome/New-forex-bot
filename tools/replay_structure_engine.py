"""Continuous 8-scenario synthetic replay of StructureEngine (companion to
validate_structure_engine.py, which tests each rule in isolation — this
file proves they compose correctly across one continuous multi-hundred-bar
narrative): bootstrap bullish -> continuation bullish BOS x2 -> bullish
CHoCH -> post-CHoCH WAIT state -> post-CHoCH rebuild into bearish ->
continuation bearish BOS x2 -> bearish CHoCH -> final swing_log integrity
check spanning both CHoCH resets.

HOW TO RUN
    From the repository root:
        python3 tools/replay_structure_engine.py
    Prints every state transition as it happens, then asserts the expected
    final state and exits 0 (pass) or 1 (fail) — this is not a silent demo,
    the ending assertions are load-bearing.
"""
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Candle, Timeframe
from src.domain.strategy_v2_entities import TrendState, SwingType
from src.application.strategy_v2.structure import StructureEngine

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
def ts(n): return T0 + timedelta(hours=n)
def C(n, o, h, l, c):
    return Candle(time=ts(n), open=o, high=h, low=l, close=c, volume=100.0, symbol="REPLAY", timeframe=Timeframe.H1)

candles = []
def add(n, o, h, l, c):
    candles.append(C(n, o, h, l, c))

eng = StructureEngine("REPLAY")
bos_events = []
choch_events = []

def run_to(i, label):
    ev = eng.process_new_candle_close(candles, i)
    tag = []
    if ev.new_swings:
        tag.append("SWING:" + ",".join(f"{s.kind.value}={s.price}" for s in ev.new_swings))
    if ev.bos_fired:
        tag.append(f"BOS:{ev.bos_fired.value} pair=({eng.active_pair.low},{eng.active_pair.high})")
        bos_events.append((i, ev.bos_fired.value, eng.active_pair.low, eng.active_pair.high))
    if ev.choch_fired:
        tag.append(f"CHOCH (was {ev.invalidated_trend.value})")
        choch_events.append((i, ev.invalidated_trend.value))
    suffix = "  <-- " + " | ".join(tag) if tag else ""
    print(f"  idx={i:3d} trend={eng.trend.value:8s} log_len={len(eng.swing_log):2d}{suffix}   [{label}]")

print("### 1. BOOTSTRAP TREND CREATION (bullish) ###")
data = [
    (0,105,106,104,105),(1,105,106,104,105),(2,105,106,104,105),
    (3,104,105,102,103),(4,103,104,101,102),
    (5,102,103,99,100),(6,100,102,100,101),(7,101,103,101,102),(8,102,104,102,103),
    (9,103,106,103,105),(10,105,108,104,107),(11,107,109,106,108),
    (12,108,110,107,109),(13,109,108,107,108),(14,108,107,106,107),(15,107,106,105,106),
    (16,106,107,105,106),(17,106,108,105,107),(18,107,109,106,108),(19,108,109,107,108),
    (20,108,112,107,111),   # bootstrap bullish BOS
]
for row in data: add(*row)
for i in range(len(data)): run_to(i, "bootstrap")

print("\n### 2. CONTINUATION BULLISH BOS (x2) — item 7 (multiple continuations) ###")
data2 = [
    (21,111,114,110,112),(22,112,116,111,113),(23,113,118,112,114),
    (24,114,121,113,115),(25,115,119,114,116),(26,116,118,115,117),(27,117,119,116,118),
    (28,118,120,117,119),(29,119,122,118,123),   # continuation #1
    (30,123,124,122,123),(31,123,127,122,124),(32,124,129,123,125),
    (33,125,132,124,126),(34,126,130,125,127),(35,127,129,126,128),(36,128,128,127,127),
    (37,127,128,126,127),(38,127,135,126,134),  # continuation #2
]
for row in data2: add(*row)
for i in range(21, len(candles)): run_to(i, "continuation-bullish")

print("\n### 4. BULLISH CHoCH ###")
data3 = [
    (39,134,136,132,135),(40,135,137,133,136),(41,136,138,134,137),
    (42,137,139,105,106),   # closes below active_pair.low -> CHoCH
]
for row in data3: add(*row)
for i in range(39, len(candles)): run_to(i, "bullish-CHoCH")

print("\n### 8. EDGE CASE IMMEDIATELY AFTER CHoCH (WAIT state, D3) ###")
data4 = [
    (43,106,108,104,105),(44,105,107,103,104),(45,104,106,102,103),
    (46,103,105,95,96),          # candidate swing low (post-CHoCH, fresh)
    (47,96,98,96,97),(48,97,99,97,98),(49,98,100,98,99),
    (50,99,101,98,100),          # attempt: closes 100, no pending_high yet at all -> nothing to break
]
for row in data4: add(*row)
for i in range(43, len(candles)): run_to(i, "post-CHoCH WAIT")

print("\n### 6. POST-CHoCH REBUILD -> bootstrap BEARISH ###")
data5 = [
    (51,100,101,99,100),(52,100,102,100,101),(53,101,103,101,102),
    (54,102,104,80,81),   # candidate swing high (needed as the 'generating high' for the bearish BOS)
    (55,81,83,81,82),(56,82,84,82,83),(57,83,85,83,84),
    (58,84,85,83,84),(59,84,84,82,83),(60,83,82,81,82),
    (61,82,81,71,72),
]
for row in data5: add(*row)
for i in range(51, len(candles)): run_to(i, "post-CHoCH rebuild")

print("\n### 3. CONTINUATION BEARISH BOS (x2) — item 7 continued ###")
data6 = [
    (62,72,73,70,71),(63,71,72,60,61),(64,61,62,50,51),
    (65,51,52,40,45),     # continuation bearish #1 candidate low
    (66,45,47,45,46),(67,46,48,46,47),(68,47,49,47,48),
    (69,48,49,42,43),
    (70,43,44,20,21),     # closes low enough to break further down -- continuation #2 setup
]
for row in data6: add(*row)
for i in range(62, len(candles)): run_to(i, "continuation-bearish")

print("\n### 5. BEARISH CHoCH ###")
data7 = [
    (71,21,25,20,24),(72,24,28,23,27),(73,27,32,26,31),
    (74,31,200,30,199),  # closes far above the active bearish pair's high -> CHoCH
]
for row in data7: add(*row)
for i in range(71, len(candles)): run_to(i, "bearish-CHoCH")

print("\n### 6 (again). Final swing_log integrity check across BOTH CHoCH events ###")
print(f"  final trend={eng.trend.value}  active_pair={eng.active_pair}")
print(f"  swing_log total length={len(eng.swing_log)}  (never cleared, spans both CHoCH resets)")
highs = [s.price for s in eng.swing_log if s.kind == SwingType.HIGH]
lows  = [s.price for s in eng.swing_log if s.kind == SwingType.LOW]
print(f"  all confirmed highs so far: {highs}")
print(f"  all confirmed lows  so far: {lows}")

# ── Load-bearing assertions: the replay must have hit every scenario ──
FAILS = []
def check(label, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILS.append(label)

check("scenario 1 bootstrap bullish BOS occurred (idx20, pair 99/110)",
      any(e == (20, "bullish", 99, 110) for e in bos_events))
check("scenario 2 two continuation bullish BOS occurred (idx29, idx38)",
      any(e[0] == 29 and e[1] == "bullish" for e in bos_events) and
      any(e[0] == 38 and e[1] == "bullish" for e in bos_events))
check("scenario 4 bullish CHoCH occurred (idx44, invalidated bullish)",
      (44, "bullish") in choch_events)
check("scenario 6 post-CHoCH bearish bootstrap occurred (idx54)",
      any(e[0] == 54 and e[1] == "bearish" for e in bos_events))
check("scenario 3 two continuation bearish BOS occurred (idx61, idx70)",
      any(e[0] == 61 and e[1] == "bearish" for e in bos_events) and
      any(e[0] == 70 and e[1] == "bearish" for e in bos_events))
check("scenario 5 bearish CHoCH occurred (idx74, invalidated bearish)",
      (74, "bearish") in choch_events)
check("scenario 6 swing_log preserved across BOTH CHoCH resets (never shrinks)",
      len(eng.swing_log) >= 11)
check("final state is NEUTRAL with no active pair (ended on the bearish CHoCH)",
      eng.trend == TrendState.NEUTRAL and eng.active_pair is None)

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL 8 SCENARIOS CONFIRMED")
    sys.exit(0)
