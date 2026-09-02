import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Runner
import suite_core
import suite_sandbox

r = Runner()
suites = sys.argv[1:] or ["core"]
mods = {"core": suite_core, "sandbox": suite_sandbox}
try:
    import suite_ops; mods["ops"] = suite_ops
except ImportError: pass
try:
    import suite_perf; mods["perf"] = suite_perf
except ImportError: pass

# An unknown suite name is an error, not a note. This previously printed
# "(no suite 'sandbox')" and fell through to a clean exit -- so a CI step
# written as `run.py sandbox` went green having executed nothing at all,
# while appearing to attest to the 21-case sandbox-escape matrix.
unknown = [s for s in suites if s not in mods]
if unknown:
    print(f"unknown suite(s): {', '.join(unknown)}", file=sys.stderr)
    print(f"available: {', '.join(sorted(mods))}", file=sys.stderr)
    sys.exit(2)

for s in suites:
    print(f"\n{'='*70}\nSUITE: {s}\n{'='*70}", flush=True)
    mods[s].run(r)

payload = r.dump()
summary = payload.get("summary", {})

# dump() only writes JSON and returns; nothing here used to set an exit code,
# so a run with failures exited 0 and any CI step gating on it was decorative.
failed = summary.get("fail", 0) + summary.get("block", 0)
if failed:
    print(f"\nFAILED: {failed} case(s) failed or were blocked", file=sys.stderr)
    sys.exit(1)
if not payload.get("results"):
    print("\nFAILED: no cases ran", file=sys.stderr)
    sys.exit(1)
