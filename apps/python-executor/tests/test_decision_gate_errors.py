"""Decision-path errors fail closed + are observable — never a silent wrong decision.

Accuracy is paramount: a decision gate (rule/scorecard/decision_table) that raises must not be
silently skipped (which could let a decision pass a gate it should have failed). It escalates to
`review` and records an error event. On the fast path, a variable that errors is defaulted to
null but the error is surfaced as an observable event, not dropped.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.fast_decide as fast_decide  # noqa: E402
import app.main as app_main  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class DecisionGateErrorTests(unittest.TestCase):
    """These two tests had a BUILT-IN EXPIRY DATE and finally reached it on 2026-09-23.

    They used the process-global `app_main.storage` -- a shared, persistent database -- and
    asserted that a filtered count went UP:

        self.assertGreater(len(self._recent_errors("gate_error")), before)

    `list_error_events` returns the most recent 200 rows (`storage.py:2492`, `limit: int = 200`)
    and `_recent_errors` filters that WINDOW by stage. Once the shared database holds 200 error
    events the window is full, so writing one more evicts the globally-oldest row. Whether the
    filtered count rises then depends on which STAGE that evicted row happened to be -- and as
    one stage comes to dominate the window, the odds move steadily against the assertion.

    Measured at the moment of failure: the window held 158 `gate_error` + 42 `variable_error`,
    exactly 200. Every previous run had been adding to it. So this was not flake in the usual
    sense of noise; it was a coin weighted a little further against the test every time anyone
    ran it, and the coin came up tails on 2026-09-23.

    That also means the fix cannot be mutation-checked cleanly: reverting to the shared global
    storage does NOT reliably fail, because whether it fails depends on the accumulated contents
    of a database outside the test. Being unable to write a deterministic failing case against
    the old code is itself the argument for the new one.

    They also passed in a full suite while failing alone, because some earlier test's storage
    global leaked in and handed them a smaller database -- a pass that depended on a bug
    elsewhere.

    Own the storage, like every other test here does, and ask for a window big enough that no
    single test can fill it.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "gate-errors.db"))
        app_main.storage = self.storage
        self.tenant_id = self.storage.default_tenant_id

    def _recent_errors(self, stage):
        # limit is explicit and far above anything one test can produce: the default 200 is what
        # made these tests expire, and leaving it implicit would just move the date.
        return [e for e in self.storage.list_error_events(tenant_id=self.tenant_id, limit=1000)
                if e.get("stage") == stage]

    def test_gate_error_fails_closed_to_review_and_records(self):
        before = len(self._recent_errors("gate_error"))
        policy = {"id": "gate_test", "name": "g", "steps": [{"type": "rule", "ref_id": "r1", "id": "s1"}], "defaultOutcome": "review"}
        executor = PolicyExecutor(self.storage)
        original = executor._execute_step_body

        async def boom(step, *args, **kwargs):
            if step.get("type") == "rule":
                raise RuntimeError("kaboom")
            return await original(step, *args, **kwargs)

        executor._execute_step_body = boom
        ctx = asyncio.run(executor.execute(policy=policy, payload={}, tenant_id=self.tenant_id, source="test", simulate=True))
        # Failed CLOSED — not silently approved.
        self.assertEqual(ctx.outcome, "review")
        self.assertNotEqual(ctx.outcome, "approve")
        # And observable — an error event was recorded.
        after = self._recent_errors("gate_error")
        self.assertGreater(len(after), before)
        self.assertTrue(any("failed closed" in e.get("message", "") for e in after))

    def test_fast_path_variable_error_is_recorded_not_swallowed(self):
        prod = self.storage.list_policies(status="prod", tenant_id=self.tenant_id)
        policy = prod[0]
        before = len(self._recent_errors("variable_error"))

        # Force the first variable to error; it should default to null AND surface an event.
        original = fast_decide.execute_variable
        state = {"errored": False}

        def failing(code, source_payload, values, timeout_ms=2000, memory_mb=128):
            if not state["errored"]:
                state["errored"] = True
                return {"value": None, "error": "simulated variable failure"}
            return original(code, source_payload, values, timeout_ms=timeout_ms, memory_mb=memory_mb)

        fast_decide.invalidate(self.tenant_id)  # rebuild the serving bundle fresh
        fast_decide.execute_variable = failing
        try:
            fast_decide.fast_decide(self.storage, policy, {"amount": 1000}, self.tenant_id, log=False)
        finally:
            fast_decide.execute_variable = original
            fast_decide.invalidate(self.tenant_id)

        after = self._recent_errors("variable_error")
        self.assertGreater(len(after), before, "a fast-path variable error must be recorded, not silently dropped")


if __name__ == "__main__":
    unittest.main()
