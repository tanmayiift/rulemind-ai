#!/usr/bin/env python3
"""Put the server into the state saturation.k6.js exists to measure, then verify it.

WHY THIS IS NEEDED, and why the sweep was green without it.

saturation.k6.js asserts a conditional: *if* a variable failed to compute, the decision must
have gone to review with the fail-closed guard in its trace. Run it against a healthy server
and nothing ever fails, so the assertion never executes -- and the run prints PASS having
proven only that the server was up. That is what shipped: workflow run 35430953581 reported
success on a sweep whose guard was never exercised, alongside an evaluate sweep whose
correctness check was failing 100% of the time behind `continue-on-error: true`.

WHAT WAS TRIED FIRST, AND WHY IT IS NOT WHAT THIS SCRIPT DOES. The script's own header says to
run it against a pool smaller than the concurrency. Measured on this machine, that is not
sufficient and not reproducible:

    pool=4, timeout_ms=2000 (defaults), 60 VUs  ->  0.0% degraded   INCONCLUSIVE
    pool=4, timeout_ms=60,   60 VUs             ->  0.0% degraded   INCONCLUSIVE
    pool=1, timeout_ms=60,   60 VUs             ->  0.0% degraded on one run, 32.9% on another

The reason is structural. `future.result(timeout=)` bounds the wait for ONE variable, not the
decision, and uvicorn's threadpool caps concurrent decisions at 40 -- so the per-call queue
never gets deep enough to cross the timeout, and whether it does at all depends on what else
the machine is doing. A threshold gated on that would flap.

So force it deterministically instead: install ONE variable whose body is slower than the
timeout. Every decision then attempts it, every attempt times out, and the guard is exercised
on every request regardless of load, pool size or machine.

    pool=4, timeout_ms=60, slow variable, 60 VUs  ->  81.9% degraded, 59 guard firings, 0 unsound
    pool=1, same                                  ->  99.6% degraded, 241 guard firings, 0 unsound

Mutation-checked in both directions with `engine_config.fail_open_on_variable_error` -- the
product's own documented escape hatch, so no source edit is needed to break it:

    guard on   -> 0 unsound, exit 0
    guard off  -> unsound decisions logged by name, thresholds crossed, exit 99

THIS IS DESTRUCTIVE TO THE TARGET WORKSPACE. It lowers the tenant's evaluation timeout and adds
a variable to its catalog. Point it only at a throwaway load-test server -- never at anything
whose decisions matter. It refuses a non-loopback host for that reason.

It defaults to 127.0.0.1:8080, which on a developer's machine is the compose stack, so it also
records the previous `timeout_ms` before overwriting it and can put everything back:

    python3 test/load/saturation_setup.py --revert

Revert restores the recorded timeout and deletes the harness variable. The recorded value lives
in `.runtime/saturation_setup_state.json` (gitignored, like the rest of .runtime).
"""
import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

# range() bound picked so the body is several times the 100ms floor that
# sandbox.execute_variable applies to any timeout (`max(timeout_ms/1000, 0.1)`), with enough
# headroom that a fast machine still times out. Measured here: ~400ms per call.
SLOW_VARIABLE = '''@variable(source="bureau")
def load_generator(payload, variables):
    """Deliberately slower than the evaluation timeout.

    Exists so test/load/saturation.k6.js can prove the fail-closed guard fires, rather than
    reporting PASS on a run where no variable ever failed. See test/load/saturation_setup.py.
    """
    total = 0
    for i in range(30000000):
        total += i % 7
    return total
'''


def call(base, path, payload, key, method):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"content-type": "application/json", "x-api-key": key},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        body = res.read().decode()
        return res.status, (json.loads(body) if body else None)


STATE = pathlib.Path(__file__).resolve().parents[2] / ".runtime" / "saturation_setup_state.json"


def revert(base, key):
    """Put the workspace back. Best-effort on each half, so one failure does not strand the other."""
    restored = "nothing recorded"
    if STATE.exists():
        previous = json.loads(STATE.read_text()).get("timeout_ms")
        if previous is not None:
            call(base, "/api/v1/settings", {"engine_config": {"timeout_ms": previous}}, key, "PUT")
            restored = "timeout_ms={0}".format(previous)
        STATE.unlink()
    try:
        call(base, "/api/v1/variables/load_generator", None, key, "DELETE")
        removed = "load_generator deleted"
    except urllib.error.HTTPError as exc:
        removed = "load_generator not removed (HTTP {0}) - delete it by hand".format(exc.code)
    print("reverted: {0}; {1}".format(restored, removed))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--api-key", default="rm_live_devlocaltenantkey000000000000")
    ap.add_argument("--policy-id", default="")
    ap.add_argument("--timeout-ms", type=int, default=60)
    ap.add_argument("--revert", action="store_true",
                    help="restore the recorded timeout and remove the harness variable")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    if not any(h in base for h in ("127.0.0.1", "localhost", "[::1]")):
        print("refusing: this rewrites tenant settings and catalog; loopback only", file=sys.stderr)
        return 2

    if args.revert:
        return revert(base, args.api_key)

    # Record the CURRENT timeout before overwriting it, so --revert has something to restore.
    # Written before the change, not after: a crash between the two must not leave a state file
    # claiming the workspace is untouched.
    try:
        _, settings = call(base, "/api/v1/settings", None, args.api_key, "GET")
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(
            {"timeout_ms": (settings.get("engine_config") or {}).get("timeout_ms", 2000)}))
    except Exception:
        # Not fatal. This is a load-test harness; failing to record an undo should not stop the
        # run, it should just mean --revert has nothing to restore.
        pass

    call(base, "/api/v1/settings", {"engine_config": {
        "timeout_ms": args.timeout_ms,
        # Explicitly OFF. This is the switch the mutation check flips, and leaving it to whatever
        # the workspace happened to have would mean the sweep silently measured a fail-open
        # configuration and still called it a pass.
        "fail_open_on_variable_error": False,
    }}, args.api_key, "PUT")

    variable = {"id": "load_generator", "name": "Load Generator", "category": "Bureau",
                "source_id": "bureau", "code": SLOW_VARIABLE,
                "description": "saturation harness - see test/load/saturation_setup.py",
                # DEV, not prod, for one reason: `DELETE /api/v1/variables/{id}` returns 409
                # "Only DEV variables can be deleted", so a prod harness variable can never be
                # reverted. The executor's catalog comes from `list_variables` with no status
                # filter, so a dev variable is computed on every decision exactly the same --
                # verified by the probe below, which would fail loudly if it were not.
                "status": "dev"}
    try:
        call(base, "/api/v1/variables", variable, args.api_key, "POST")
    except urllib.error.HTTPError:
        # Already present from an earlier run on the same workspace.
        call(base, "/api/v1/variables/load_generator", variable, args.api_key, "PUT")

    # VERIFY, do not assume. Both previous attempts at this looked correct and produced nothing;
    # the only trustworthy check is one decision that actually comes back degraded.
    policy_id = args.policy_id
    if not policy_id:
        _, policies = call(base, "/api/v1/policies", None, args.api_key, "GET")
        prod = [p for p in policies if p.get("status") == "prod"]
        chosen = (prod or policies)
        if not chosen:
            print("no policy to verify against", file=sys.stderr)
            return 1
        policy_id = chosen[0]["id"]

    _, decision = call(base, "/api/v1/decide", {
        "policyId": policy_id,
        "payload": {"age": 34, "bureau": {"score": 720}},
        "userId": "saturation-setup-probe",
    }, args.api_key, "POST")

    errors = decision.get("variable_errors") or {}
    guard = [s for s in (decision.get("trace") or [])
             if isinstance(s, dict) and s.get("type") == "variable_error_guard"]
    if "load_generator" not in errors:
        print("setup did NOT take: load_generator computed successfully in {0}ms. The sweep would "
              "report PASS having exercised nothing. Raise the loop bound or lower --timeout-ms."
              .format(decision.get("latency_ms")), file=sys.stderr)
        return 1
    if not guard or decision.get("outcome") != "review":
        print("load_generator failed as intended, but the guard did not fire (outcome={0}). That "
              "is the T1.2 regression itself -- let the sweep report it."
              .format(decision.get("outcome")), file=sys.stderr)
        return 1

    print("saturation harness armed: load_generator times out, guard sends the decision to "
          "review. policy={0} timeout_ms={1}".format(policy_id, args.timeout_ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
