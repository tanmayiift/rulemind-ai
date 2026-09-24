"""One sandbox round trip for a decision's variables (plan 4.4), against the REAL pool.

The per-call path got its guarantees from its process boundary; a group run inside one worker
has to rebuild each of them, so each one is a test here: order and accumulation, isolation of
payloads and earlier values, a per-variable deadline (including against code that catches it),
error isolation, the evaluation budget, and fail-closed on a broken pool. Then the decisions
themselves: every seeded policy decides identically one trip or one call per variable, and a
decision really is one trip.

These run the pool. The 13 older tests that replace `execute_variable` keep testing the per-call
path, which is what a replaced primitive gets -- see app/variable_runner.py.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import sandbox, variable_runner  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


def var(body: str) -> str:
    return "import datetime\n@variable\ndef v(payload, variables):\n" + "".join(
        "    " + line + "\n" for line in body.strip("\n").splitlines())


BUSY_80MS = var("""
end = datetime.datetime.now() + datetime.timedelta(milliseconds=80)
while datetime.datetime.now() < end:
    pass
return 1
""")
CATCHES_ITS_TIMEOUT = var("""
while True:
    try:
        while True:
            pass
    except:
        pass
""")


def group(items, sources=None, **kwargs):
    return sandbox.execute_variable_group(items, sources or {}, **kwargs)


@unittest.skipIf(os.environ.get("SANDBOX_MODE") == "inline", "these test the pool")
class GroupSemanticsTests(unittest.TestCase):
    def test_each_variable_sees_the_ones_before_it(self):
        results = group([("a", var("return 40"), None), ("b", var("return variables['a'] + 2"), None)])
        self.assertEqual([r["value"] for r in results], [40, 42])

    def test_a_variable_cannot_change_what_the_next_one_is_handed(self):
        """The per-call path pickled a fresh copy for every call, so mutation could not leak.
        Inside one worker that has to be done on purpose."""
        results = group([
            ("xs", var("return [1, 2]"), None),
            ("grow", var("variables['xs'].append(3)\npayload['items'].append(9)\nreturn len(variables['xs'])"), "src"),
            ("after", var("return [len(variables['xs']), len(payload['items'])]"), "src"),
        ], {"src": {"items": [1]}})
        self.assertEqual(results[1]["value"], 3)
        self.assertEqual(results[2]["value"], [2, 1], "one variable's mutation reached the next")

    def test_a_variable_that_runs_too_long_times_out_alone(self):
        pool = sandbox._get_pool()
        started = time.perf_counter()
        results = group([("wedge", var("while True:\n    pass"), None), ("after", var("return 7"), None)],
                        timeout_ms=200)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(results[0]["error"], sandbox.TIMED_OUT)
        self.assertEqual(results[1]["value"], 7)
        self.assertIs(sandbox._get_pool(), pool, "an ordinary timeout recycled the pool")

    def test_code_that_catches_its_timeout_still_times_out(self):
        """Variable code may use a bare except. It can swallow the timer; it cannot keep the
        decision waiting past the group's deadline, and the sandbox works afterwards."""
        started = time.perf_counter()
        results = group([("sly", CATCHES_ITS_TIMEOUT, None), ("after", var("return 1"), None)],
                        timeout_ms=200, budget_ms=600)
        self.assertLess(time.perf_counter() - started, 600 / 1000 + sandbox._GROUP_GRACE_S + 1.0)
        self.assertEqual(results[0]["error"], sandbox.TIMED_OUT)
        self.assertEqual(group([("ok", var("return 5"), None)])[0]["value"], 5)

    def test_a_value_returned_after_the_deadline_is_still_a_timeout(self):
        """Catch the timer once and return something: the per-call path would have stopped
        waiting at the deadline, so a late value is not a value."""
        results = group([("late", var("try:\n    while True:\n        pass\nexcept:\n    return 99"), None)],
                        timeout_ms=150)
        self.assertEqual((results[0]["value"], results[0]["error"]), (None, sandbox.TIMED_OUT))

    def test_one_variable_failing_does_not_fail_the_others(self):
        results = group([("boom", var("return 1 / 0"), None),
                         ("odd", var("return (x for x in [1])"), None),
                         ("fine", var("return 3"), None)])
        self.assertIn("division", results[0]["error"])
        self.assertIn("cannot be serialised", results[1]["error"])
        self.assertEqual((results[2]["value"], results[2]["error"]), (3, None))

    def test_the_budget_stops_the_group_and_says_where(self):
        results = group([("a", BUSY_80MS, None), ("b", BUSY_80MS, None), ("c", BUSY_80MS, None)],
                        timeout_ms=2000, budget_ms=120)
        self.assertEqual(results[0]["value"], 1)
        self.assertEqual(results[1]["error"], sandbox.TIMED_OUT, "b should be clamped to the 40 ms left")
        self.assertEqual(results[2], {"abandoned": True})

    def test_a_broken_pool_fails_the_group_closed(self):
        broken = mock.Mock()
        broken.submit.side_effect = BrokenProcessPool("worker died")
        items = [("a", var("return 1"), None), ("b", var("return variables['a'] + 1"), None)]
        with mock.patch.object(sandbox, "_get_pool", return_value=broken), \
                mock.patch.object(sandbox, "_recycle_pool"), \
                mock.patch.dict(os.environ, {"SANDBOX_FALLBACK_MODE": "fail"}):
            failed = group(items)
        self.assertTrue(all(r["value"] is None and "unavailable" in r["error"] for r in failed))
        with mock.patch.object(sandbox, "_get_pool", return_value=broken), \
                mock.patch.object(sandbox, "_recycle_pool"), \
                mock.patch.dict(os.environ, {"SANDBOX_FALLBACK_MODE": "inline"}):
            inline = group(items)
        self.assertEqual([r["value"] for r in inline], [1, 2], "the inline fallback lost accumulation")


@unittest.skipIf(os.environ.get("SANDBOX_MODE") == "inline", "these test the pool")
class RunnerTests(unittest.TestCase):
    def _run(self, variables, **kwargs):
        options = dict(execute=sandbox.execute_variable, timeout_ms=2000, memory_mb=128,
                       check_budget=lambda stage: None, remaining_ms=lambda: None)
        options.update(kwargs)
        return variable_runner.compute_variables(variables, {}, **options)

    def test_the_budget_raises_exactly_as_the_per_call_loop_did(self):
        class Spent(Exception):
            pass

        deadline = time.perf_counter() + 0.12

        def check(_stage):
            if time.perf_counter() > deadline:
                raise Spent()

        variables = [{"id": name, "code": BUSY_80MS, "source_id": None} for name in "abc"]
        with self.assertRaises(Spent):
            self._run(variables, check_budget=check,
                      remaining_ms=lambda: max(0.0, (deadline - time.perf_counter()) * 1000))

    def test_a_replaced_primitive_is_honoured_call_by_call(self):
        seen = []

        def stand_in(code, payload, values, timeout_ms, memory_mb):
            seen.append(dict(values))
            return {"value": len(seen), "error": None}

        values, _ = self._run([{"id": "a", "code": "", "source_id": None},
                               {"id": "b", "code": "", "source_id": None}], execute=stand_in)
        self.assertEqual(values, {"a": 1, "b": 2})
        self.assertEqual(seen, [{}, {"a": 1}])

    def test_the_kill_switch_restores_one_call_per_variable(self):
        with mock.patch.dict(os.environ, {"SANDBOX_BATCH": "0"}), \
                mock.patch.object(sandbox, "execute_variable_group") as grouped:
            values, _ = self._run([{"id": "a", "code": var("return 1"), "source_id": None}])
        grouped.assert_not_called()
        self.assertEqual(values, {"a": 1})


@unittest.skipIf(os.environ.get("SANDBOX_MODE") == "inline", "these test the pool")
class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "group.db"))
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def _decide(self, policy_id, batch, fast="0"):
        with mock.patch.dict(os.environ, {"SANDBOX_BATCH": batch, "FAST_DECIDE": fast}):
            body = self.client.post("/api/v1/decide", headers=self.headers,
                                    json={"policy_id": policy_id, "payload": {"bureau_score": 612}}).json()
        return {key: body[key] for key in ("outcome", "score", "variables", "variable_errors", "rule_results")}

    def test_every_seeded_policy_decides_the_same_either_way(self):
        tenant = app_main.storage.default_tenant_id
        for policy in app_main.storage.list_policies(tenant_id=tenant):
            with self.subTest(policy=policy["id"]):
                self.assertEqual(self._decide(policy["id"], "1"), self._decide(policy["id"], "0"))
        with self.subTest("the fast path"):
            self.assertEqual(self._decide("policy_pl_underwriting", "1", fast="1"),
                             self._decide("policy_pl_underwriting", "0", fast="1"))

    def test_a_decision_is_one_round_trip(self):
        pool = sandbox._get_pool()
        with mock.patch.object(pool, "submit", wraps=pool.submit) as submitted, \
                mock.patch.dict(os.environ, {"SANDBOX_BATCH": "1"}):
            body = self.client.post("/api/v1/decide", headers=self.headers,
                                    json={"policy_id": "policy_pl_underwriting", "payload": {}}).json()
        self.assertGreater(len(body["variables"]), 10)
        self.assertEqual(submitted.call_count, 1, "the variables were not sent in one trip")


if __name__ == "__main__":
    unittest.main()
