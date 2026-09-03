"""A decision must be bounded in wall-clock time (Tier 1.3).

There was no evaluation timeout anywhere in the system. The only limit was per-variable
(`engine_config.timeout_ms`, default 2000ms), applied independently to each variable in a
serial loop -- so the real bound was `timeout_ms x variable_count`. The seeded demo tenant
alone has 55 variables, which is 110 seconds for ONE request, holding a Starlette threadpool
thread the whole time. Forty of those and the API is gone, with no error and no metric moving.

Why this is enforced cooperatively rather than with asyncio.wait_for: `/decide` is a sync `def`,
so Starlette runs it in a threadpool. Cancelling an await there returns 504 to the caller while
the work keeps running and keeps its thread -- the timeout would make an overload worse instead
of shedding it. The budget is therefore checked between variables and between steps, and each
variable's own timeout is clamped to the remaining budget so one variable cannot overshoot.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.executor import EvaluationBudgetExceeded, PolicyExecutor  # noqa: E402
from app.storage import Storage  # noqa: E402


class EvaluationBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "budget.db"))
        self.tenant = app_main.storage.default_tenant_id
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.policy_id = self.client.get("/api/v1/policies", headers=self.headers).json()[0]["id"]

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _set_budget(self, ms):
        settings = app_main.storage.get_settings(tenant_id=self.tenant)
        engine_config = dict(settings.get("engine_config") or {})
        engine_config["evaluation_budget_ms"] = ms
        app_main.storage.update_settings({"engine_config": engine_config}, tenant_id=self.tenant)

    def _slow_variable(self, seconds):
        """A variable that genuinely blocks, so the clamp is measured and not assumed."""
        def slow(code, payload, values, timeout_ms=2000, memory_mb=128):
            time.sleep(min(seconds, timeout_ms / 1000.0))
            return {"value": 1, "error": None}
        return slow

    def test_a_healthy_decision_is_unaffected(self):
        """The default budget is a safety bound, not an SLO -- it must never fire normally."""
        res = self.client.post("/api/v1/decide", headers=self.headers,
                               json={"policyId": self.policy_id, "payload": {"age": 30}})
        self.assertEqual(res.status_code, 200, res.text[:200])

    def test_a_slow_decision_returns_504(self):
        from app import executor as executor_module

        self._set_budget(200)
        with mock.patch.object(executor_module, "execute_variable",
                               side_effect=self._slow_variable(0.05)):
            res = self.client.post("/api/v1/decide", headers=self.headers,
                                   json={"policyId": self.policy_id, "payload": {"age": 30}})
        self.assertEqual(res.status_code, 504, res.text[:200])
        self.assertEqual(res.json()["error"], "evaluation_budget_exceeded")
        self.assertEqual(res.json()["budget_ms"], 200)

    def test_it_bounds_the_actual_wall_clock(self):
        """The point of the whole exercise. Not "the caller stops waiting" -- the WORK stops.

        55 variables x 50ms would be ~2.75s unbounded. With a 300ms budget the request must
        return in roughly that, not merely time out while the loop keeps burning threads.
        """
        from app import executor as executor_module

        self._set_budget(300)
        started = time.perf_counter()
        with mock.patch.object(executor_module, "execute_variable",
                               side_effect=self._slow_variable(0.05)):
            res = self.client.post("/api/v1/decide", headers=self.headers,
                                   json={"policyId": self.policy_id, "payload": {"age": 30}})
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(res.status_code, 504)
        self.assertLess(elapsed_ms, 1500,
                        "the request returned 504 but the evaluation kept running "
                        "({0:.0f}ms for a 300ms budget)".format(elapsed_ms))

    def test_one_variable_cannot_overshoot_the_budget(self):
        """The clamp. Without it, a variable started with 5ms of budget left still runs its
        full timeout_ms -- so the effective bound stays `timeout_ms x variable_count`."""
        from app import executor as executor_module

        self._set_budget(120)
        seen = []

        def record(code, payload, values, timeout_ms=2000, memory_mb=128):
            seen.append(timeout_ms)
            time.sleep(0.05)
            return {"value": 1, "error": None}

        with mock.patch.object(executor_module, "execute_variable", side_effect=record):
            self.client.post("/api/v1/decide", headers=self.headers,
                             json={"policyId": self.policy_id, "payload": {"age": 30}})
        self.assertTrue(seen, "no variable ran")
        self.assertTrue(all(t <= 120 for t in seen),
                        "a variable was given more timeout than the whole budget: {0}".format(seen))
        self.assertLess(seen[-1], seen[0],
                        "the per-variable timeout did not shrink as the budget drained")

    def test_it_can_be_disabled(self):
        self._set_budget(0)
        executor = PolicyExecutor(app_main.storage)
        policy = app_main.storage.get_policy(self.policy_id, tenant_id=self.tenant)
        import asyncio

        ctx = asyncio.run(executor.execute(policy=policy, payload={}, tenant_id=self.tenant,
                                           simulate=True))
        self.assertIsNone(ctx.deadline)
        self.assertEqual(ctx.budget_ms, 0)

    def test_a_nested_workflow_cannot_escape_the_budget(self):
        """A sub-workflow step recurses into _run_steps, and the step loop catches Exception to
        fail gates closed. Without an explicit re-raise the budget would apply only to the top
        level, which is exactly where it is least needed."""
        import inspect

        from app import executor as executor_module

        source = inspect.getsource(executor_module.PolicyExecutor._run_steps)
        self.assertIn("except EvaluationBudgetExceeded:", source)
        self.assertLess(source.index("except EvaluationBudgetExceeded:"),
                        source.index("except Exception as exc:"),
                        "the broad handler would catch the budget exception first")

    def test_the_budget_covers_steps_not_just_variables(self):
        """An action step makes outbound HTTP with its own timeoutMs x retries, so a policy can
        blow the budget without computing a single variable."""
        executor = PolicyExecutor(app_main.storage)
        policy = app_main.storage.get_policy(self.policy_id, tenant_id=self.tenant)
        import asyncio

        self._set_budget(50)
        with mock.patch.object(PolicyExecutor, "_compute_variables", lambda self, ctx, needed_ids=None: None):
            with self.assertRaises(EvaluationBudgetExceeded) as caught:
                async def run():
                    ex = executor
                    ctx_holder = {}

                    async def slow_body(*a, **kw):
                        time.sleep(0.08)
                        return None, None

                    with mock.patch.object(PolicyExecutor, "_execute_step_body", slow_body):
                        ctx_holder["ctx"] = await ex.execute(
                            policy=policy, payload={}, tenant_id=self.tenant, simulate=True)
                asyncio.run(run())
        self.assertTrue(caught.exception.stage.startswith("step"),
                        "expected the step loop to catch it, got {0!r}".format(caught.exception.stage))

    def test_it_stops_attempting_variables_once_the_budget_is_gone(self):
        """The clamp bounds how long each variable may take; the deadline check is what stops
        the loop from starting more of them at all.

        Both are needed, and they fail differently. If a variable ignores its timeout -- which
        is exactly what happens when the sandbox pool breaks and SANDBOX_FALLBACK_MODE runs the
        code inline, with no timeout and no process boundary -- the clamp is a suggestion and
        the deadline check is the only thing left. So assert the loop actually stops rather than
        merely running 50 more one-millisecond variables.
        """
        from app import executor as executor_module

        self._set_budget(120)
        attempts = []

        def ignores_its_timeout(code, payload, values, timeout_ms=2000, memory_mb=128):
            attempts.append(timeout_ms)
            time.sleep(0.03)  # deliberately ignores timeout_ms, as inline fallback does
            return {"value": 1, "error": None}

        total_variables = len(app_main.storage.list_variables(tenant_id=self.tenant))
        self.assertGreater(total_variables, 10, "the demo tenant should have many variables")
        with mock.patch.object(executor_module, "execute_variable", side_effect=ignores_its_timeout):
            res = self.client.post("/api/v1/decide", headers=self.headers,
                                   json={"policyId": self.policy_id, "payload": {"age": 30}})
        self.assertEqual(res.status_code, 504)
        self.assertLess(len(attempts), total_variables,
                        "every variable was attempted despite the budget being exhausted "
                        "after the first few ({0} of {1})".format(len(attempts), total_variables))


if __name__ == "__main__":
    unittest.main()
