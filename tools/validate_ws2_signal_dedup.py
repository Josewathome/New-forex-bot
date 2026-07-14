"""WS2 validation — drives the REAL ExecutionService._signal_fingerprint and
_evict_stale_signals against a stubbed self, replicating the exact dedup gate
from execute_entry_candidate. Proves: cascade blocked, legit signal fires,
vol-drift ignored, survives remap + restart."""
import sys, types
from datetime import datetime, timezone, timedelta
sys.path.insert(0, ".")
import src.config as config
from src.domain.entities import Direction, SignalType
from src.application.execution_service import ExecutionService

def mk_candidate(symbol, direction, entry, disp_iso, zone_created_iso, zone_id, vol_ratio, digits=2):
    sig = types.SimpleNamespace(
        direction=direction, signal_type=SignalType.BULLISH_ENGULFING,
        entry_price=entry, created_at=datetime.fromisoformat(disp_iso),
        volume_ratio=vol_ratio,
        zone=types.SimpleNamespace(id=zone_id, created_at=datetime.fromisoformat(zone_created_iso)),
    )
    return types.SimpleNamespace(symbol=symbol, digits=digits, signal=sig)

def fresh_self():
    return types.SimpleNamespace(_executed_signals={}, _now=lambda: datetime(2026,7,13,15,25,tzinfo=timezone.utc))

def gate(slf, cand):
    """Exact replica of the dedup gate in execute_entry_candidate."""
    fp = ExecutionService._signal_fingerprint(slf, cand)
    ExecutionService._evict_stale_signals(slf)
    if fp in slf._executed_signals:
        return "SKIP"
    slf._executed_signals[fp] = cand.signal.created_at.isoformat()
    return "EXECUTE"

ok = True
# ── Test 1: Jul-13 cascade — 37 detections of ONE signal (same disp/entry/zone
#            anchor), only vol_ratio drifts; zone.id regenerates on remap. ──
slf = fresh_self()
outs = []
vol_seq = [1.6,1.7,1.8,2.2] * 10  # drifting vol_ratio like the real cascade
for i in range(37):
    zid = f"uuid-remap-{i//5}"   # zone.id regenerates every ~5 min (remap)
    c = mk_candidate("XAUUSD", Direction.BEARISH, 4035.86,
                     "2026-07-13T15:00:00+00:00",      # constant displacement time
                     "2026-07-09T06:00:00+00:00",      # constant zone anchor
                     zid, vol_seq[i])
    outs.append(gate(slf, c))
execs = outs.count("EXECUTE")
t1 = (execs == 1)
ok &= t1
print(f"TEST 1 cascade: 37 detections → EXECUTE={execs} SKIP={outs.count('SKIP')}  {'PASS' if t1 else 'FAIL'} (expect exactly 1 EXECUTE)")

# ── Test 2: a genuinely NEW signal (different displacement time) still fires ──
c_new = mk_candidate("XAUUSD", Direction.BEARISH, 4020.00,
                     "2026-07-13T15:40:00+00:00", "2026-07-13T14:00:00+00:00", "uuid-x", 1.9)
t2 = (gate(slf, c_new) == "EXECUTE")
ok &= t2
print(f"TEST 2 new signal (diff disp/entry): {'PASS' if t2 else 'FAIL'} (expect EXECUTE)")

# ── Test 3: vol_ratio drift alone does NOT create a new identity ──
a = mk_candidate("GBPUSD", Direction.BULLISH, 1.33369, "2026-07-06T07:50:00+00:00","2026-07-06T06:00:00+00:00","z1",1.6)
b = mk_candidate("GBPUSD", Direction.BULLISH, 1.33369, "2026-07-06T07:50:00+00:00","2026-07-06T06:00:00+00:00","z1",2.9)
fp_a=ExecutionService._signal_fingerprint(slf,a); fp_b=ExecutionService._signal_fingerprint(slf,b)
t3=(fp_a==fp_b)
ok &= t3
print(f"TEST 3 vol-drift ignored: fp_equal={t3}  {'PASS' if t3 else 'FAIL'}")

# ── Test 4: zone.id regeneration (remap) does NOT change identity ──
c1 = mk_candidate("GBPUSD", Direction.BULLISH, 1.33369, "2026-07-06T07:50:00+00:00","2026-07-06T06:00:00+00:00","ID-A",1.6)
c2 = mk_candidate("GBPUSD", Direction.BULLISH, 1.33369, "2026-07-06T07:50:00+00:00","2026-07-06T06:00:00+00:00","ID-B",1.6)
t4=(ExecutionService._signal_fingerprint(slf,c1)==ExecutionService._signal_fingerprint(slf,c2))
ok &= t4
print(f"TEST 4 survives H1 remap (zone.id differs): fp_equal={t4}  {'PASS' if t4 else 'FAIL'}")

# ── Test 5: restart survival — persist ledger, reload into fresh self, re-check ──
persisted = dict(slf._executed_signals)               # save_runtime_state
slf2 = fresh_self()
for fp,iso in persisted.items(): slf2._executed_signals[str(fp)] = str(iso)  # load_runtime_state
# the cascade signal, re-detected after restart, must still be blocked
c_after = mk_candidate("XAUUSD", Direction.BEARISH, 4035.86, "2026-07-13T15:00:00+00:00","2026-07-09T06:00:00+00:00","uuid-new",2.0)
t5 = (gate(slf2, c_after) == "SKIP")
ok &= t5
print(f"TEST 5 restart survival: cascade signal after reload → {'SKIP' if t5 else 'EXECUTE'}  {'PASS' if t5 else 'FAIL'}")

print("\nWS2 VALIDATION:", "ALL PASS" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
