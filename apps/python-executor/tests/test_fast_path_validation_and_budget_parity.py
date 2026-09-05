"""FAST_DECIDE=1 must be a performance flag, not a correctness switch (parity, third pass).

Two guards were executor-only, so turning the flag on silently removed both:

  * NESTED INPUT VALIDATION (P06). `app/input_schema.py` was wired into PolicyExecutor
    (`_validate_input`) and imported by `fast_decide` nowhere at all. A tenant who had opted into
    `engine_config.input_validation` got a misspelled nested key rejected with 422 on the slow
    path and ACCEPTED with 200 on the fast path -- the bad key resolving to None, and None
    comparing False against every operator, so the typo did not fail, it re-phrased the rule.
    The direction it then broke was decided by how the author happened to word the gate.

  * THE EVALUATION BUDGET (Tier 1.3). `fast_decide` had no deadline of any kind: no
    `EvaluationBudgetExceeded`, so main.py's 504 handler could never fire for a fast decision.
    The bound matters MORE here, because the fast path computes the tenant's whole variable
    catalog with no COMPUTE_ONLY_USED_VARS filter -- the seeded demo tenant is 55 variables at
    the 2000ms per-variable timeout, i.e. 110 seconds of one threadpool thread per request.

Every test that can be phrased as "both paths agree" is phrased that way and runs the SAME policy
and the SAME payload through both, because an assertion about the fast path alone cannot tell a
fixed fast path from a fast path that drifted somewhere new.
"""
from __future__ import annotations

import asyncio
import copy
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # the seeded `bank` source declares a nested schema
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import fast_decide as fast_decide_module  # noqa: E402
from app import decision_log, input_schema  # noqa: E402
from app.executor import EvaluationBudgetExceeded, PolicyExecutor  # noqa: E402
from app.fast_decide import fast_decide, fast_path_eligible  # noqa: E402
from app.storage import Storage  # noqa: E402

# The seeded `bank` connector declares summary.avgBalance, salary[].amount, bounces[].amount and
# emiPayments[].amount -- real nesting with arrays, the shape that had no validation at all.
BANK_COMPLETE = {
    "summary": {"avgBalance": 45200},
    "salary": [{"amount": 62000}],
    "bounces": [{"amount": 5000}],
    "emiPayments": [{"amount": 8500}],
}

BALANCE_VARIABLE = {
    "id": "bank_balance",
    "name": "Average Bank Balance",
    "category": "Bank",
    "source_id": "bank",
    "description": "Average balance from the statement summary",
    "code": (
        '@variable(source="bank")\n'
        "def bank_balance(payload, variables, apis):\n"
        '    return payload.get("summary", {}).get("avgBalance")\n'
    ),
    "status": "prod",
}

# The dangerous outcome sits on the pass branch, which is how every "flag the bad ones" rule is
# written -- so a nested key that silently becomes None approves instead of rejecting.
THIN_FILE_RULE = {
    "id": "rule_thin_file",
    "name": "Thin File Gate",
    "nodes": [],
    "tree": {
        "type": "group", "id": "root", "logic": "AND",
        "children": [{"type": "condition", "id": "c1",
                      "variable": "bank_balance", "operator": "<", "value": 10000}],
        "onPass": "reject",
        "onFail": "approve",
    },
    "rule_format": "v1",
    "expression": "IF (Average Bank Balance < 10000) -> REJECT ELSE -> APPROVE",
    "status": "prod",
}

BALANCE_SCORECARD = {
    "id": "sc_bank",
    "name": "Bank Scorecard",
    "base_score": 300,
    "max_score": 900,
    "bins": [{"variable_id": "bank_balance",
              "ranges": [{"min": 0, "max": 1000000, "points": 50}],
              "weight": 1.0}],
    "status": "prod",
}

# Rule + outcome only, so fast_path_eligible says yes. If this ever stops being fast-servable the
# parity tests below would quietly compare the slow path against itself, so it is asserted.
BANK_POLICY = {
    "id": "pol_bank_fast",
    "name": "Bank Policy (fast-servable)",
    "defaultOutcome": "review",
    "steps": [{"id": "s1", "type": "rule", "ref_id": "rule_thin_file", "label": "Thin File Gate"}],
}

# Same policy plus a scorecard step. `_build_serving_bundle` hands rules-only policies to the
# Rust core, which returns an EMPTY trace -- so a trace-ordering assertion made against
# BANK_POLICY would hold no matter where the entry was inserted. The scorecard routes this one
# through the Python core, which produces a per-step trace to be ordered against.
BANK_POLICY_WITH_SCORECARD = {
    "id": "pol_bank_fast_sc",
    "name": "Bank Policy (fast-servable, scored)",
    "defaultOutcome": "review",
    "steps": [{"id": "s1", "type": "rule", "ref_id": "rule_thin_file", "label": "Thin File Gate"},
              {"id": "s2", "type": "scorecard", "ref_id": "sc_bank", "label": "Bank Scorecard"}],
}


def bank_payload(**overrides):
    """A COMPLETE bank source payload, so the only issue is the one the test introduced.

    The source-keyed form is authoritative (see the /decide docstring): omitted fields are absent,
    not defaulted, so a partial payload would produce a pile of missing_required noise.
    """
    payload = copy.deepcopy(BANK_COMPLETE)
    payload.update(overrides)
    return {"bank": payload}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "fastparity.db"))
        self.tenant = app_main.storage.default_tenant_id
        app_main.storage.create_variable(dict(BALANCE_VARIABLE), tenant_id=self.tenant)
        app_main.storage.create_rule(dict(THIN_FILE_RULE), tenant_id=self.tenant)
        app_main.storage.create_scorecard(dict(BALANCE_SCORECARD), tenant_id=self.tenant)
        app_main.storage.create_policy(dict(BANK_POLICY), tenant_id=self.tenant)
        app_main.storage.create_policy(dict(BANK_POLICY_WITH_SCORECARD), tenant_id=self.tenant)
        self.policy = app_main.storage.get_policy("pol_bank_fast", tenant_id=self.tenant)
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        fast_decide_module.invalidate()

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()
        fast_decide_module.invalidate()

    def configure(self, config):
        """Write the tenant's input_validation block and make it take effect NOW.

        Settings are served from the cross-request catalog cache with a 5s TTL backstop, and
        update_settings does not bump the epoch -- without the invalidate a test could decide
        against the previous block.
        """
        settings = app_main.storage.get_settings(tenant_id=self.tenant)
        engine_config = dict(settings.get("engine_config") or {})
        engine_config["input_validation"] = config
        app_main.storage.update_settings({"engine_config": engine_config}, tenant_id=self.tenant)
        fast_decide_module.invalidate()

    def set_budget(self, ms):
        settings = app_main.storage.get_settings(tenant_id=self.tenant)
        engine_config = dict(settings.get("engine_config") or {})
        engine_config["evaluation_budget_ms"] = ms
        app_main.storage.update_settings({"engine_config": engine_config}, tenant_id=self.tenant)
        fast_decide_module.invalidate()

    def decide_http(self, payload, fast, policy_id="pol_bank_fast"):
        env = dict(os.environ)
        env["FAST_DECIDE"] = "1" if fast else "0"
        with mock.patch.dict(os.environ, env, clear=True):
            return self.client.post("/api/v1/decide", headers=self.headers,
                                    json={"policyId": policy_id, "payload": payload})

    def decide_slow_direct(self, payload):
        return asyncio.run(PolicyExecutor(app_main.storage).execute(
            policy=self.policy, payload=payload, tenant_id=self.tenant, source="api"))

    def decide_fast_direct(self, payload, log=True):
        return fast_decide(app_main.storage, self.policy, payload, self.tenant, log=log)

    @staticmethod
    def issue_paths(response):
        return sorted(issue["path"] for issue in response.json()["detail"]["issues"])


class TheTestFixtureItselfTest(_Base):
    """If the policy stopped being fast-servable, every parity test below would be comparing the
    slow path against itself and would pass while the fast path stayed broken."""

    def test_the_policies_under_test_really_take_the_fast_path(self):
        for policy_id in ("pol_bank_fast", "pol_bank_fast_sc"):
            with self.subTest(policy=policy_id):
                policy = app_main.storage.get_policy(policy_id, tenant_id=self.tenant)
                self.assertTrue(fast_path_eligible(app_main.storage, policy, self.tenant))


class EnforceParityTest(_Base):
    def setUp(self):
        super().setUp()
        self.configure({"mode": "enforce"})

    def test_a_misspelled_nested_key_is_rejected_on_both_paths(self):
        """The bug, stated once: one character changed in one nested key turned a 500-rupee
        balance into an approval, but ONLY when FAST_DECIDE was on."""
        payload = bank_payload(summary={"avgBalanace": 500})
        slow = self.decide_http(payload, fast=False)
        fast = self.decide_http(payload, fast=True)
        self.assertEqual(slow.status_code, 422, slow.text[:300])
        self.assertEqual(fast.status_code, 422, fast.text[:300])
        self.assertEqual(fast.json()["detail"]["error"], "invalid_input",
                         "a caller's typo is a 422, not a 500 that pages on-call")

    def test_both_paths_report_the_identical_issue_set(self):
        """Not merely 'both refused'. A caller who fixes what the fast path named and is then
        rejected by the slow path for something else has been sent round the loop for nothing."""
        payload = {"bank": {"summary": {"avgBalance": "45200"},
                            "salary": [{"amount": 62000}],
                            "bouncees": [{"amount": 5000}]}}
        slow = self.decide_http(payload, fast=False)
        fast = self.decide_http(payload, fast=True)
        self.assertEqual(self.issue_paths(fast), self.issue_paths(slow))
        self.assertEqual(fast.json()["detail"]["issues"], slow.json()["detail"]["issues"])
        self.assertIn("bank.summary.avgBalance", self.issue_paths(fast))

    def test_a_bad_key_inside_an_array_is_named_with_its_index_on_the_fast_path(self):
        """Proves the fast path runs the NESTED validator, not a flat look-alike: a flat check
        cannot produce an element index at all."""
        response = self.decide_http(
            bank_payload(salary=[{"amount": 62000}, {"amount": "62000"}]), fast=True)
        self.assertEqual(response.status_code, 422, response.text[:300])
        self.assertIn("bank.salary[1].amount", self.issue_paths(response))

    def test_the_fast_path_raises_the_executors_own_exception_class(self):
        """Class identity, not shape. /decide's 422 comes from FastAPI's HTTPException handler,
        so a look-alike exception declared in fast_decide would surface as a 500."""
        with self.assertRaises(input_schema.InputValidationError):
            self.decide_fast_direct(bank_payload(summary={"avgBalanace": 500}))

    def test_a_rejected_payload_leaves_no_decision_on_either_path(self):
        """Fail CLOSED, not fail soft. 'Validated' and 'decided anyway' is the worst of both:
        a Decision row, a billable unit and an outcome computed from the None the typo made."""
        for fast in (False, True):
            with self.subTest(fast=fast):
                self.decide_http(bank_payload(summary={"avgBalanace": 500}), fast=fast)
        decision_log.flush()
        self.assertEqual(app_main.storage.list_decisions(tenant_id=self.tenant), [])

    def test_a_valid_payload_is_unaffected_on_both_paths(self):
        """The regression guard. Rejecting bad input is only correct if good input still runs."""
        slow = self.decide_http(bank_payload(), fast=False)
        fast = self.decide_http(bank_payload(), fast=True)
        self.assertEqual(slow.status_code, 200, slow.text[:300])
        self.assertEqual(fast.status_code, 200, fast.text[:300])
        self.assertEqual(fast.json()["outcome"], slow.json()["outcome"])

    def test_an_explicit_null_on_a_required_field_is_rejected_on_the_fast_path(self):
        """The spelling is right, so nothing else would ever notice -- and the gate still reads
        None, which is the entire problem."""
        response = self.decide_http(bank_payload(summary={"avgBalance": None}), fast=True)
        self.assertEqual(response.status_code, 422, response.text[:300])
        self.assertIn("bank.summary.avgBalance", self.issue_paths(response))

    def test_validation_happens_before_any_variable_is_computed(self):
        """Ordering is the difference between a guard and a report. Computing 55 sandboxed
        variables against a payload already known to be bad is work spent to produce an outcome
        that must be thrown away."""
        with mock.patch.object(fast_decide_module, "execute_variable") as sandbox:
            with self.assertRaises(input_schema.InputValidationError):
                self.decide_fast_direct(bank_payload(summary={"avgBalanace": 500}))
        sandbox.assert_not_called()


class WarnModeParityTest(_Base):
    """Warn mode is the migration step: it must report exactly what enforcement would reject,
    on whichever path served the decision, or an operator watching before switching enforcement
    on sees the fast path's traffic look clean."""

    def setUp(self):
        super().setUp()
        self.configure({"mode": "warn"})

    @staticmethod
    def _entry(body):
        return next((step for step in (body.get("trace") or [])
                     if step.get("type") == "input_validation"), None)

    def test_both_paths_decide_and_both_record_the_same_trace_entry(self):
        payload = bank_payload(summary={"avgBalanace": 500})
        slow = self.decide_http(payload, fast=False)
        fast = self.decide_http(payload, fast=True)
        self.assertEqual((slow.status_code, fast.status_code), (200, 200))
        slow_entry, fast_entry = self._entry(slow.json()), self._entry(fast.json())
        self.assertIsNotNone(fast_entry, "warn mode went silent the moment FAST_DECIDE was on")
        self.assertEqual(fast_entry, slow_entry)
        self.assertEqual(fast_entry["status"], "warned")

    def test_the_entry_leads_the_trace_as_it_does_on_the_slow_path(self):
        """The executor appends it before any step runs. An operator diffing the two traces must
        not see the same decision described in a different order.

        Uses the scorecard policy deliberately: a rules-only policy is served by the Rust core,
        whose trace is empty, so there would be nothing for the entry to be ordered against.
        """
        body = self.decide_http(bank_payload(summary={"avgBalanace": 500}), fast=True,
                                policy_id="pol_bank_fast_sc").json()
        self.assertGreater(len(body["trace"]), 1, "no step trace to order the entry against")
        self.assertEqual(body["trace"][0].get("type"), "input_validation")

    def test_an_error_event_is_recorded_on_the_fast_path_too(self):
        self.decide_http(bank_payload(summary={"avgBalanace": 500}), fast=True)
        events = [event for event in app_main.storage.list_error_events(tenant_id=self.tenant)
                  if event.get("stage") == "input_validation"]
        self.assertTrue(events, "the fast path flagged nothing an operator could alert on")
        self.assertFalse(events[0]["details"]["blocked"])


class OffByDefaultTest(_Base):
    """The compatibility promise, re-asserted for the fast path. Every integration in production
    today sends payloads nobody ever validated; validating them because a performance flag was
    flipped would 422 all of them at once."""

    def test_a_misspelled_nested_key_still_decides(self):
        response = self.decide_http(bank_payload(summary={"avgBalanace": 500}), fast=True)
        self.assertEqual(response.status_code, 200, response.text[:300])

    def test_nothing_is_added_to_the_fast_paths_trace(self):
        body = self.decide_http(bank_payload(summary={"avgBalanace": 500}), fast=True).json()
        self.assertEqual([step for step in (body.get("trace") or [])
                          if step.get("type") == "input_validation"], [],
                         "validation ran on a workspace that never enabled it")


class EvaluationBudgetOnTheFastPathTest(_Base):
    """The 504 contract, which had no reach into this module at all."""

    def _slow_variable(self, seconds):
        """Genuinely blocks, so the clamp is measured rather than assumed."""
        def slow(code, payload, values, timeout_ms=2000, memory_mb=128):
            time.sleep(min(seconds, timeout_ms / 1000.0))
            return {"value": 1, "error": None}
        return slow

    def test_a_healthy_fast_decision_is_unaffected(self):
        """The default budget is a safety bound, not an SLO -- it must never fire normally."""
        response = self.decide_http(bank_payload(), fast=True)
        self.assertEqual(response.status_code, 200, response.text[:300])

    def test_a_slow_fast_path_decision_returns_504_like_the_slow_path(self):
        self.set_budget(200)
        for fast in (False, True):
            with self.subTest(fast=fast):
                with mock.patch.object(fast_decide_module, "execute_variable",
                                       side_effect=self._slow_variable(0.05)):
                    with mock.patch("app.executor.execute_variable",
                                    side_effect=self._slow_variable(0.05)):
                        response = self.decide_http(bank_payload(), fast=fast)
                self.assertEqual(response.status_code, 504, response.text[:300])
                self.assertEqual(response.json()["error"], "evaluation_budget_exceeded")
                self.assertEqual(response.json()["budget_ms"], 200)

    def test_it_raises_the_executors_exception_class_so_the_504_handler_catches_it(self):
        """main.py dispatches on `@app.exception_handler(EvaluationBudgetExceeded)` by class
        identity. A budget exception declared inside fast_decide would miss that handler and turn
        a retryable timeout into a 500 that pages on-call."""
        self.set_budget(50)
        with mock.patch.object(fast_decide_module, "execute_variable",
                               side_effect=self._slow_variable(0.03)):
            with self.assertRaises(EvaluationBudgetExceeded) as caught:
                self.decide_fast_direct(bank_payload())
        self.assertEqual(caught.exception.budget_ms, 50)
        self.assertEqual(caught.exception.stage, "variable computation")

    def test_it_bounds_the_actual_wall_clock(self):
        """Not 'the caller stops waiting' -- the WORK stops. 55 variables x 50ms is ~2.75s
        unbounded."""
        self.set_budget(300)
        started = time.perf_counter()
        with mock.patch.object(fast_decide_module, "execute_variable",
                               side_effect=self._slow_variable(0.05)):
            response = self.decide_http(bank_payload(), fast=True)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(response.status_code, 504)
        self.assertLess(elapsed_ms, 1500,
                        "the request returned 504 but the evaluation kept running "
                        "({0:.0f}ms for a 300ms budget)".format(elapsed_ms))

    def test_one_variable_cannot_overshoot_the_budget(self):
        """The clamp. Without it a variable started with 5ms of budget left still runs its full
        timeout_ms, so the effective bound stays timeout_ms x variable_count.

        The budget and the per-variable sleep are sized so that MANY variables run before it
        drains. The first version used 120ms and 50ms sleeps, which fits about two variables on a
        fast lane and exactly one on a slow one -- and with one, `seen[-1]` and `seen[0]` are the
        same element, so the shrink assertion compares 49 with 49 and fails. The Postgres lane
        found that; the SQLite lane never would have. A test whose outcome depends on how fast
        the database is, is testing the database.
        """
        self.set_budget(500)
        seen = []

        def record(code, payload, values, timeout_ms=2000, memory_mb=128):
            seen.append(timeout_ms)
            time.sleep(0.02)
            return {"value": 1, "error": None}

        with mock.patch.object(fast_decide_module, "execute_variable", side_effect=record):
            self.decide_http(bank_payload(), fast=True)
        self.assertTrue(seen, "no variable ran")
        self.assertTrue(all(timeout <= 500 for timeout in seen),
                        "a variable was given more timeout than the whole budget: {0}".format(seen))
        self.assertGreater(len(seen), 1,
                           "only one variable ran, so there is nothing to compare -- raise the "
                           "budget or shorten the sleep rather than relaxing the assertion")
        self.assertLess(seen[-1], seen[0],
                        "the per-variable timeout did not shrink as the budget drained: "
                        "{0}".format(seen))

    def test_it_stops_attempting_variables_once_the_budget_is_gone(self):
        """The clamp bounds how long each variable may take; the deadline check is what stops the
        loop starting more of them. They fail differently: when the sandbox pool breaks and
        SANDBOX_FALLBACK_MODE runs code inline there is no timeout at all, and the deadline check
        is the only thing left."""
        self.set_budget(120)
        attempts = []

        def ignores_its_timeout(code, payload, values, timeout_ms=2000, memory_mb=128):
            attempts.append(timeout_ms)
            time.sleep(0.03)  # deliberately ignores timeout_ms, as the inline fallback does
            return {"value": 1, "error": None}

        total = len(app_main.storage.list_variables(tenant_id=self.tenant))
        self.assertGreater(total, 10, "the demo tenant should have many variables")
        with mock.patch.object(fast_decide_module, "execute_variable",
                               side_effect=ignores_its_timeout):
            response = self.decide_http(bank_payload(), fast=True)
        self.assertEqual(response.status_code, 504)
        self.assertLess(len(attempts), total,
                        "every variable was attempted despite the budget being exhausted after "
                        "the first few ({0} of {1})".format(len(attempts), total))

    # ------------------------------------------------------------------ the check AFTER the loop
    #
    # These two exist because an adversarial review deleted `budget.check("policy evaluation")`
    # from fast_decide() and all 22 tests above still passed. The per-variable check and the
    # per-variable clamp were both pinned; the boundary between the variable loop and the compiled
    # core was not, so the one enforcement point that survives when the loop does not raise was
    # decoration. A test that cannot fail is not a test.
    #
    # It matters in exactly the two cases below: nothing in the loop to check, and a loop that
    # exits cleanly with the budget already gone. In both, removing that line turns a 504 into a
    # 200 decision plus a billable Decision row.

    def test_the_deadline_is_checked_after_the_variable_loop_too(self):
        """The loop returns cleanly with the budget spent -- what the last clamped variable does
        when it runs right up to the deadline and returns a value rather than timing out."""
        self.set_budget(120)

        def eats_the_budget_and_returns_cleanly(bundle, payload, budget=None, seed_samples=False):
            time.sleep(0.2)
            return {}, {}, {}

        with mock.patch.object(fast_decide_module, "_compute_variables",
                               side_effect=eats_the_budget_and_returns_cleanly):
            with self.assertRaises(EvaluationBudgetExceeded) as caught:
                self.decide_fast_direct(bank_payload())
        self.assertEqual(caught.exception.stage, "policy evaluation",
                         "the budget fired, but from the variable loop rather than the check "
                         "between the loop and the compiled core")
        self.assertEqual(caught.exception.budget_ms, 120)

    def test_a_policy_with_no_variables_is_still_bounded(self):
        """With an empty variable list the loop body never runs, so the check after it is the ONLY
        enforcement left on the fast path. The budget is consumed by the bundle build here, which
        is the other thing the deadline is anchored early enough to cover -- it starts at
        fast_decide()'s own `started`, so the build and any single-flight wait count against it."""
        self.set_budget(120)
        real_bundle = fast_decide_module._serving_bundle(app_main.storage, self.tenant, self.policy)
        no_variables = dict(real_bundle)
        no_variables["variables"] = []

        def slow_build(storage, tenant_id, policy_id):
            time.sleep(0.2)
            return no_variables

        with mock.patch.object(fast_decide_module, "_serving_bundle", side_effect=slow_build):
            response = self.decide_http(bank_payload(), fast=True)
        self.assertEqual(response.status_code, 504, response.text[:300])
        self.assertEqual(response.json()["error"], "evaluation_budget_exceeded")

    def test_zero_disables_it_exactly_as_on_the_slow_path(self):
        self.set_budget(0)
        bundle = fast_decide_module._serving_bundle(app_main.storage, self.tenant, self.policy)
        budget = fast_decide_module._Budget(bundle["budget_ms"], time.perf_counter())
        self.assertEqual(budget.budget_ms, 0)
        self.assertIsNone(budget.deadline)
        self.assertIsNone(budget.remaining_ms())
        budget.check("variable computation")  # must not raise

    def test_the_two_paths_read_the_same_setting_with_the_same_default(self):
        """Same engine_config key, same default, same fallback on a malformed value. Reading
        'abc' as 'no limit' would turn a typo into the unbounded request this exists to prevent."""
        self.assertEqual(fast_decide_module._budget_ms({}), PolicyExecutor.DEFAULT_BUDGET_MS)
        self.assertEqual(fast_decide_module._budget_ms({"evaluation_budget_ms": "abc"}),
                         PolicyExecutor.DEFAULT_BUDGET_MS)
        self.assertEqual(fast_decide_module._budget_ms({"evaluation_budget_ms": -5}), 0)
        self.assertEqual(fast_decide_module._budget_ms({"evaluation_budget_ms": 250}), 250)


if __name__ == "__main__":
    unittest.main()
