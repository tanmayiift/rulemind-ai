import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Runner
import suite_core

r = Runner()
suites = sys.argv[1:] or ["core"]
mods = {"core": suite_core}
try:
    import suite_ops; mods["ops"] = suite_ops
except ImportError: pass
try:
    import suite_perf; mods["perf"] = suite_perf
except ImportError: pass
for s in suites:
    if s in mods:
        print(f"\n{'='*70}\nSUITE: {s}\n{'='*70}", flush=True)
        mods[s].run(r)
    else:
        print(f"(no suite '{s}')")
r.dump()
