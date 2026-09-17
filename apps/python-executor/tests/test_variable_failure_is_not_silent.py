"""A variable that fails to compute must not silently change the decision (Tier 1.2).

The most dangerous bug in this repo, and the reason it is dangerous is the DIRECTION it fails.

`execute_variable` returns `{"value": None, "error": "..."}` on a sandbox timeout -- it degrades
rather than raising. The executor discarded that error, so the variable became a null
indistinguishable from a legitimately-null value. And `logic.py` returns False for every
comparison against None, so which way the decision breaks is decided by how the author phrased
the rule:

    "reject if fraud_score >= 90"    -> False -> NOT rejected     (fails OPEN)
    "approve if bureau_score >= 700" -> False -> not approved      (fails closed)

Every "flag the bad ones" rule -- the whole fraud and risk category -- failed open. Scorecards
were worse: a null factor scores 0 points, so the total inflates toward approve.

And it is load-triggered, not just a bug in slow code. `future.result(timeout=...)` times out on
WAITING, not executing, so a saturated sandbox pool converts healthy variables into nulls. Peak
traffic is exactly when this approved the most and logged nothing unusual.

The response layer compounded it: /decide hardcoded `"error": None, "passed": True` for every
variable, so a caller could not have detected it either.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

TIMEOUT = {"value": None, "error": "Variable execution timed out."}


class VariableFailureTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "varfail.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.policy_id = self.client.get("/api/v1/policies", headers=self.headers).json()[0]["id"]

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _decide(self, timing_out: bool):
        """Run a real decision, optionally with EVERY variable timing out."""
        from app import executor as executor_module

        if not timing_out:
            return self.client.post("/api/v1/decide", headers=self.headers,
                                    json={"policyId": self.policy_id, "payload": {"age": 30}})
        with mock.patch.object(executor_module, "execute_variable", return_value=dict(TIMEOUT)):
            return self.client.post("/api/v1/decide", headers=self.headers,
                                    json={"policyId": self.policy_id, "payload": {"age": 30}})

    def test_a_timed_out_variable_does_not_approve(self):
        """The headline. A decision computed entirely on nulls must not come back approved."""
        res = self._decide(timing_out=True)
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertNotEqual(res.json()["outcome"], "approve",
                            "a decision made on failed inputs must never approve")

    def test_it_fails_closed_to_review(self):
        body = self._decide(timing_out=True).json()
        self.assertEqual(body["outcome"], "review",
                         "the safe landing spot is a human, not a guess")

    def test_the_response_reports_the_failure(self):
        """/decide hardcoded error=None and passed=True for every variable, so the API asserted
        there was no failure. A caller must be able to see it."""
        body = self._decide(timing_out=True).json()
        # "variables" is a flat id->value map, so a failed variable is just null there --
        # indistinguishable from a legitimately-null value. The error map is the only signal.
        self.assertIn("variable_errors", body, "/decide gives a caller no way to see a failure")
        self.assertTrue(body["variable_errors"],
                        "every variable timed out and the response reported no errors")
        self.assertTrue(all(body["variables"][k] is None for k in body["variable_errors"]),
                        "an errored variable should be null in the value map")

    def test_the_failure_is_alertable(self):
        """Recorded as an error event, so an operator sees it without reading decisions."""
        self._decide(timing_out=True)
        events = app_main.storage.list_error_events(tenant_id=self.tenant)
        variable_errors = [e for e in events if e.get("stage") == "variable_error"]
        self.assertTrue(variable_errors, "a failed variable produced no error event")

    def test_the_trace_says_why_it_went_to_review(self):
        """An operator looking at one decision must be able to tell this from a genuine review."""
        body = self._decide(timing_out=True).json()
        guards = [s for s in (body.get("trace") or []) if s.get("type") == "variable_error_guard"]
        self.assertEqual(len(guards), 1, "the fail-closed step is not in the trace")
        self.assertTrue(guards[0].get("failed_variables"))
        # The pre-guard outcome is kept so an operator can see what it would have been.
        self.assertIn(guards[0].get("original_outcome"), ("approve", "reject", "pending"))

    def test_a_healthy_decision_is_unaffected(self):
        """The regression guard. Failing closed is only correct if it does not fire normally."""
        body = self._decide(timing_out=False).json()
        self.assertIn(body["outcome"], ("approve", "reject", "review"))
        self.assertEqual([s for s in (body.get("trace") or [])
                          if s.get("type") == "variable_error_guard"], [],
                         "the guard fired on a decision with no failed variables")
        self.assertEqual(body.get("variable_errors"), {},
                         "a healthy decision reported variable errors")

    def test_an_operator_can_opt_out(self):
        """Deliberately conservative by default, but tunable by someone who has measured their
        own exposure -- otherwise a saturated pool becomes a review flood with no escape."""
        settings = app_main.storage.get_settings(tenant_id=self.tenant)
        engine_config = dict(settings.get("engine_config") or {})
        engine_config["fail_open_on_variable_error"] = True
        app_main.storage.update_settings({"engine_config": engine_config}, tenant_id=self.tenant)

        body = self._decide(timing_out=True).json()
        self.assertEqual([s for s in (body.get("trace") or [])
                          if s.get("type") == "variable_error_guard"], [],
                         "opting out must disable the guard")
        # Even opted out, the failure is still recorded -- opting out of the guard is not
        # opting out of observability.
        events = app_main.storage.list_error_events(tenant_id=self.tenant)
        self.assertTrue([e for e in events if e.get("stage") == "variable_error"])


class FailOpenDirectionTest(unittest.TestCase):
    """The concrete production bug, reproduced.

    The demo policy phrases its gate as "approve if score >= 700", which happens to fail CLOSED.
    That is luck, not design. This builds the other phrasing -- "reject if fraud_score >= 90",
    the shape every fraud rule takes -- and shows that without the guard a 95-fraud-score
    applicant is APPROVED, because the condition compares None and returns False.
    """

    FRAUD_RULE = {
        "id": "rule_fraud_gate",
        "name": "Fraud Gate",
        "nodes": [],
        "tree": {
            "type": "group", "id": "root", "logic": "AND",
            "children": [{"type": "condition", "id": "c1",
                          "variable": "fraud_score", "operator": ">=", "value": 90}],
            # Phrased as a catch: the DANGEROUS outcome is on the pass branch.
            "onPass": "reject",
            "onFail": "approve",
        },
        "rule_format": "v1",
        "expression": "IF (Fraud Score >= 90) -> REJECT ELSE -> APPROVE",
        "status": "prod",
    }

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "failopen.db"))
        self.tenant = app_main.storage.default_tenant_id
        app_main.storage.create_rule(dict(self.FRAUD_RULE), tenant_id=self.tenant)
        app_main.storage.create_variable(
            {"id": "fraud_score", "name": "Fraud Score", "code": "result = 95",
             "category": "risk", "status": "prod", "source_id": "loan",
             "description": "Fraud model score"}, tenant_id=self.tenant)
        app_main.storage.create_policy(
            {"id": "pol_fraud", "name": "Fraud Policy", "defaultOutcome": "review",
             "steps": [{"id": "s1", "type": "rule", "ref_id": "rule_fraud_gate",
                        "label": "Fraud Gate"}]}, tenant_id=self.tenant)
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _decide(self, variable_result):
        from app import executor as executor_module
        with mock.patch.object(executor_module, "execute_variable", return_value=dict(variable_result)):
            return self.client.post("/api/v1/decide", headers=self.headers,
                                    json={"policyId": "pol_fraud", "payload": {}}).json()

    def test_a_real_fraud_score_is_rejected(self):
        """Baseline: when the variable computes, the rule works."""
        self.assertEqual(self._decide({"value": 95, "error": None})["outcome"], "reject")

    def test_the_same_applicant_is_not_approved_when_the_check_times_out(self):
        """THE BUG. Identical applicant, identical policy -- only the fraud check failed.

        Without the guard this returns "approve": `None >= 90` is False, so the rule takes its
        onFail branch, which for a catch-shaped rule is the permissive one. HTTP 200, no error,
        nothing in the logs to distinguish it from a clean approval.
        """
        body = self._decide(TIMEOUT)
        self.assertNotEqual(body["outcome"], "approve",
                            "a fraud check that timed out approved a 95-score applicant")
        self.assertEqual(body["outcome"], "review")

    def test_the_guard_reports_what_it_overrode(self):
        guard = [s for s in self._decide(TIMEOUT)["trace"] if s.get("type") == "variable_error_guard"]
        self.assertEqual(len(guard), 1)
        self.assertEqual(guard[0]["original_outcome"], "approve",
                         "the trace should record that this would otherwise have been approved")
        # The mock fails every variable in the tenant, seeded demo ones included, so assert
        # membership rather than an exact list.
        self.assertIn("fraud_score", guard[0]["failed_variables"])


class FastPathParityTest(unittest.TestCase):
    """FAST_DECIDE must be a PERFORMANCE flag, not a correctness one.

    fast_decide already recorded an error event for a failed variable -- but it still returned
    the fail-open outcome, so turning the flag on silently undid the slow path's guard. Since
    the flag exists to be flipped under load, and load is what causes the failures in the first
    place, that is the worst possible place for the two paths to disagree.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "parity.db"))
        self.tenant = app_main.storage.default_tenant_id
        app_main.storage.create_rule(dict(FailOpenDirectionTest.FRAUD_RULE), tenant_id=self.tenant)
        app_main.storage.create_variable(
            {"id": "fraud_score", "name": "Fraud Score", "code": "result = 95",
             "category": "risk", "status": "prod", "source_id": "loan",
             "description": "Fraud model score"}, tenant_id=self.tenant)
        app_main.storage.create_policy(
            {"id": "pol_fraud", "name": "Fraud Policy", "defaultOutcome": "review",
             "steps": [{"id": "s1", "type": "rule", "ref_id": "rule_fraud_gate",
                        "label": "Fraud Gate"}]}, tenant_id=self.tenant)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_both_paths_reach_the_same_outcome_when_a_variable_fails(self):
        from app import fast_decide as fast_module

        policy = app_main.storage.get_policy("pol_fraud", tenant_id=self.tenant)
        with mock.patch.object(fast_module, "execute_variable", return_value=dict(TIMEOUT)):
            fast = fast_module.fast_decide(app_main.storage, policy, {}, self.tenant, log=False)
        self.assertEqual(fast["outcome"], "review",
                         "the fast path approved a decision the slow path sends to review")
        self.assertTrue(fast["variable_errors"])
        guard = [s for s in fast["trace"] if s.get("type") == "variable_error_guard"]
        self.assertEqual(len(guard), 1)
        self.assertEqual(guard[0]["original_outcome"], "approve")

    def test_the_fast_path_writes_the_subject_to_user_id_not_client_id(self):
        """Tier 1.1 on the fast path. client_id carries a UNIQUE constraint, so writing the
        subject there meant a customer's second decision silently never persisted."""
        from app import fast_decide as fast_module

        policy = app_main.storage.get_policy("pol_fraud", tenant_id=self.tenant)
        for _ in range(3):
            fast_module.fast_decide(app_main.storage, policy, {}, self.tenant, user_id="cust-1")
        from app import decision_log

        decision_log.flush()
        rows = app_main.storage.list_decisions(tenant_id=self.tenant, user_id="cust-1")
        self.assertEqual(len(rows), 3,
                         "re-deciding the same customer must log every decision, not just the first")
        self.assertTrue(all(r.get("client_id") is None for r in rows),
                        "the subject leaked into the device idempotency key")


if __name__ == "__main__":
    unittest.main()
