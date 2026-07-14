"""WS3 validation — drives the REAL ExecutionService.entries_used_for_symbol /
_broker_close bookkeeping against a stubbed self, reproducing the Jul-13
instant-close cascade and proving the per-symbol cap now blocks it. Independent
of WS2 (no dedup ledger used here)."""
import sys, types
from datetime import datetime, timezone, timedelta
sys.path.insert(0, ".")
import src.config as config
from src.application.execution_service import ExecutionService

class Repo:
    def __init__(self): self.open = []          # list of open symbols
    def get_open_positions(self):
        return [types.SimpleNamespace(symbol=s) for s in self.open]
    def get_position_result(self, ticket): return {"net_profit": -0.1, "gross_profit":-0.05,
        "commission":-0.05,"swap":0.0,"entry_price":4015.0,"close_price":4015.7}

def mk_self(now):
    slf = types.SimpleNamespace()
    slf._closed_this_bar = {}
    slf._tr = Repo()
    slf._now = lambda: now[0]
    slf._journal = None
    # bind the real open_count_for_symbol so entries_used_for_symbol can call it
    slf.open_count_for_symbol = lambda sym: ExecutionService.open_count_for_symbol(slf, sym)
    slf._journal_close = lambda *a, **k: None   # stub journal write (WS1 covered separately)
    return slf

# Bind the real methods to our stub
def entries_used(slf, sym): return ExecutionService.entries_used_for_symbol(slf, sym)
def open_count(slf, sym):   return ExecutionService.open_count_for_symbol(slf, sym)
def broker_close(slf, sym): ExecutionService._broker_close(slf, 1, "WIN_FULL", 0.0, symbol=sym)

ok = True
# ── Reproduce Jul-13: within ONE H1 bar (15:00-16:00), a signal opens+instant-
#    closes repeatedly. Phase-1 cap = "entries_used_for_symbol(sym) >= 1 → skip".
now = [datetime(2026,7,13,15,0,tzinfo=timezone.utc)]
slf = mk_self(now)
ExecutionService.open_count_for_symbol  # ensure importable
executed = 0
for minute in range(20):   # 15:00 .. 15:19, one attempt per minute
    now[0] = datetime(2026,7,13,15,minute,tzinfo=timezone.utc)
    # Phase-1 gate (exact main_stream logic): skip if entries_used >= 1
    if entries_used(slf, "XAUUSD") >= 1:
        continue
    # else: open (market), instant TP1 close → record close via _broker_close
    executed += 1
    slf._tr.open.append("XAUUSD")            # opened
    broker_close(slf, "XAUUSD")              # instant close (records close time this bar)
    slf._tr.open.remove("XAUUSD")            # closed → 0 open again (the old bypass)
t1 = (executed == 1)
ok &= t1
print(f"TEST 1 cascade within one H1 bar: executed={executed}/20  {'PASS' if t1 else 'FAIL'} (expect 1; old behaviour = 20)")

# Prove the OLD open-only logic would NOT have blocked it:
slf2 = mk_self(now); old_exec = 0
for minute in range(20):
    now[0] = datetime(2026,7,13,15,minute,tzinfo=timezone.utc)
    if open_count(slf2, "XAUUSD") >= 1:   # old cap: open positions only
        continue
    old_exec += 1
    slf2._tr.open.append("XAUUSD"); slf2._tr.open.remove("XAUUSD")  # instant close
print(f"   (control) old open-only cap would allow: {old_exec}/20  → confirms the loophole")

# ── Test 2: a NEW H1 bar resets the budget — legitimate future trade allowed ──
now[0] = datetime(2026,7,13,16,0,tzinfo=timezone.utc)   # next H1 bar
t2 = (entries_used(slf, "XAUUSD") == 0)   # prior bar's closes pruned
ok &= t2
print(f"TEST 2 next H1 bar resets budget: entries_used={entries_used(slf,'XAUUSD')}  {'PASS' if t2 else 'FAIL'} (expect 0)")

# ── Test 3: a currently-OPEN position still counts (legit concurrent cap) ──
now[0] = datetime(2026,7,13,16,5,tzinfo=timezone.utc)
slf._tr.open.append("XAUUSD")
t3 = (entries_used(slf, "XAUUSD") == 1)
ok &= t3
print(f"TEST 3 open position counts: entries_used={entries_used(slf,'XAUUSD')}  {'PASS' if t3 else 'FAIL'} (expect 1)")

# ── Test 4: other symbols unaffected ──
t4 = (entries_used(slf, "GBPUSD") == 0)
ok &= t4
print(f"TEST 4 per-symbol isolation: GBPUSD entries_used={entries_used(slf,'GBPUSD')}  {'PASS' if t4 else 'FAIL'} (expect 0)")

print("\nWS3 VALIDATION:", "ALL PASS" if ok else "FAILURES PRESENT")
sys.exit(0 if ok else 1)
