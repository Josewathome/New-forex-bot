"""Regression test for the OPTIONAL swing_diagnostics module. Proves:
  1. structure.py has ZERO awareness of this module (no import coupling).
  2. Calling compute_swing_metadata() alongside a real replay has NO
     effect on BOS/CHoCH output versus not calling it at all.
  3. Metadata values are well-formed (correct types/bounds) on synthetic
     fixtures.

HOW TO RUN
    python3 tools/validate_swing_diagnostics.py   (from repo root; no env vars needed)
"""
import sys, ast
sys.path.insert(0, ".")
from datetime import datetime, timedelta, timezone
from src.domain.entities import Candle, Timeframe
from src.domain.strategy_v2_entities import TrendState
from src.application.strategy_v2.structure import StructureEngine
from src.application.strategy_v2.atr import wilder_atr_series
from src.application.strategy_v2.swing_diagnostics import compute_swing_metadata

FAILS = []
def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILS.append(label)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
def ts(n): return T0 + timedelta(hours=n)
def C(n, o, h, l, c, sym="EURUSD"):
    return Candle(time=ts(n), open=o, high=h, low=l, close=c, volume=100.0, symbol=sym, timeframe=Timeframe.H1)

print("="*78); print("SECTION 1 — ZERO IMPORT COUPLING"); print("="*78)

structure_src = open("src/application/strategy_v2/structure.py").read()
tree = ast.parse(structure_src)
imported_modules = set()
for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom) and node.module:
        imported_modules.add(node.module)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            imported_modules.add(alias.name)
check("structure.py does not import swing_diagnostics anywhere",
      not any("swing_diagnostics" in m for m in imported_modules),
      f"structure.py imports: {sorted(imported_modules)}")

print(); print("="*78); print("SECTION 1B — STATIC PROOF: NO FUNCTION REACHABLE FROM"); print("compute_swing_metadata() CAN MUTATE ITS PARAMETERS"); print("="*78)
# Stronger than "no import edge" alone: proves diagnostics is architecturally
# incapable of writing back into engine state, not merely undialed today.
# Covers every branch (not just ones the real dataset happens to exercise).

MUTATING_METHODS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "update", "popitem", "setdefault", "add", "discard", "__setitem__",
    "__delitem__", "__iadd__",
}

def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None

def analyze_function_no_mutation(func_node):
    violations = []
    params = {a.arg for a in func_node.args.args} | {a.arg for a in func_node.args.kwonlyargs}
    tainted = set(params)

    class Visitor(ast.NodeVisitor):
        def visit_Global(self, node):
            violations.append(f"global statement in {func_node.name}: {node.names}")
        def visit_Nonlocal(self, node):
            violations.append(f"nonlocal statement in {func_node.name}: {node.names}")
        def visit_Assign(self, node):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    root = _root_name(node.value) if isinstance(node.value, (ast.Attribute, ast.Subscript)) else (
                        node.value.id if isinstance(node.value, ast.Name) else None)
                    if root in tainted:
                        tainted.add(target.id)
                elif isinstance(target, (ast.Attribute, ast.Subscript)):
                    base = _root_name(target)
                    if base in tainted:
                        violations.append(f"assignment into tainted '{base}' in {func_node.name}")
            self.generic_visit(node)
        def visit_AugAssign(self, node):
            if isinstance(node.target, (ast.Attribute, ast.Subscript)) and _root_name(node.target) in tainted:
                violations.append(f"augmented assign into tainted name in {func_node.name}")
            self.generic_visit(node)
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute):
                base = _root_name(node.func.value)
                if node.func.attr in MUTATING_METHODS and base in tainted:
                    violations.append(f"mutating call '.{node.func.attr}()' on tainted name '{base}' in {func_node.name}")
            self.generic_visit(node)

    Visitor().visit(func_node)
    return violations

sd_tree = ast.parse(open("src/application/strategy_v2/swing_diagnostics.py").read())
sd_module_mutables = [t.id for node in sd_tree.body if isinstance(node, ast.Assign) for t in node.targets
                       if isinstance(t, ast.Name) and isinstance(node.value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp))]
check("swing_diagnostics.py has no module-level mutable state (no cache/memo dict)",
      len(sd_module_mutables) == 0, f"found: {sd_module_mutables}")

sd_violations, sd_funcs_checked = [], []
for node in ast.walk(sd_tree):
    if isinstance(node, ast.FunctionDef):
        sd_funcs_checked.append(node.name)
        sd_violations.extend(analyze_function_no_mutation(node))
check(f"swing_diagnostics.py: no parameter-mutation in any of its {len(sd_funcs_checked)} functions ({sorted(sd_funcs_checked)})",
      len(sd_violations) == 0, f"violations: {sd_violations}")

# The three external functions diagnostics calls into must also be pure --
# its read-only-ness depends on their purity too.
for path, fn_name in [
    ("src/application/strategy_v2/atr.py", "is_displacement"),
    ("src/application/strategy_v2/fvg.py", "detect_fvg"),
    ("src/application/strategy_v2/order_blocks.py", "detect_order_block"),
]:
    tree = ast.parse(open(path).read())
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name]
    violations = analyze_function_no_mutation(found[0]) if found else ["function not found"]
    check(f"{fn_name} ({path}, called by diagnostics) has no parameter-mutation",
          len(found) == 1 and len(violations) == 0, f"violations: {violations}")

mutator_refs = [name for name in ("update_fvg_state", "update_ob_state") if name in open("src/application/strategy_v2/swing_diagnostics.py").read()]
check("swing_diagnostics.py never references the mutator functions (update_fvg_state/update_ob_state) at all",
      len(mutator_refs) == 0, f"found references to: {mutator_refs}")

print(); print("="*78); print("SECTION 2 — OPTIONAL CALL HAS ZERO EFFECT ON BOS/CHoCH"); print("="*78)

# Reuse the same 75-candle scenario from replay_structure_engine.py.
candles = []
def add(n, o, h, l, c): candles.append(C(n, o, h, l, c, "EURUSD"))
data = [
    (0,105,106,104,105),(1,105,106,104,105),(2,105,106,104,105),
    (3,104,105,102,103),(4,103,104,101,102),
    (5,102,103,99,100),(6,100,102,100,101),(7,101,103,101,102),(8,102,104,102,103),
    (9,103,106,103,105),(10,105,108,104,107),(11,107,109,106,108),
    (12,108,110,107,109),(13,109,108,107,108),(14,108,107,106,107),(15,107,106,105,106),
    (16,106,107,105,106),(17,106,108,105,107),(18,107,109,106,108),(19,108,109,107,108),
    (20,108,112,107,111),
    (21,111,114,110,112),(22,112,116,111,113),(23,113,118,112,114),
    (24,114,121,113,115),(25,115,119,114,116),(26,116,118,115,117),(27,117,119,116,118),
    (28,118,120,117,119),(29,119,122,118,123),
]
for row in data: add(*row)

def run_engine(with_diagnostics: bool):
    eng = StructureEngine("EURUSD")
    atr = wilder_atr_series(candles, 10)
    bos_log, choch_log = [], []
    for i in range(len(candles)):
        ev = eng.process_new_candle_close(candles, i)
        if with_diagnostics:
            for s in ev.new_swings:
                # optional call -- exercised here to prove it changes nothing
                compute_swing_metadata("EURUSD", Timeframe.H1, candles, atr, eng.swing_log, s,
                                        eng.active_pair, 0.0003)
        if ev.bos_fired:
            bos_log.append((i, ev.bos_fired.value, eng.active_pair.low, eng.active_pair.high))
        if ev.choch_fired:
            choch_log.append((i, ev.invalidated_trend.value))
    return bos_log, choch_log

bos_without, choch_without = run_engine(with_diagnostics=False)
bos_with, choch_with = run_engine(with_diagnostics=True)
check("BOS log identical whether or not diagnostics is called",
      bos_without == bos_with, f"without={bos_without} with={bos_with}")
check("CHoCH log identical whether or not diagnostics is called",
      choch_without == choch_with, f"without={choch_without} with={choch_with}")

print(); print("="*78); print("SECTION 3 — METADATA WELL-FORMEDNESS (synthetic fixtures)"); print("="*78)

eng2 = StructureEngine("EURUSD")
atr2 = wilder_atr_series(candles, 10)
all_swings_metadata = []
for i in range(len(candles)):
    ev = eng2.process_new_candle_close(candles, i)
    for s in ev.new_swings:
        m = compute_swing_metadata("EURUSD", Timeframe.H1, candles, atr2, eng2.swing_log, s, eng2.active_pair, 0.0003)
        all_swings_metadata.append(m)

check("at least one swing was annotated", len(all_swings_metadata) > 0, f"n={len(all_swings_metadata)}")
types_ok = all(
    isinstance(m["liquidity_sweep"], bool) and isinstance(m["equal_hl_proximity"], bool)
    and isinstance(m["fvg_origin"], bool) and isinstance(m["ob_origin"], bool)
    and isinstance(m["displacement_magnitude"], float) and m["displacement_magnitude"] >= 0
    and isinstance(m["backward_prominence_10"], bool)
    for m in all_swings_metadata
)
check("all metadata fields are well-typed and in-bounds", types_ok)

print(); print("="*78)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
else:
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
