"""POST /api/v1/decide must return the same SHAPE whatever FAST_DECIDE is set to.

`FAST_DECIDE` is documented as a performance toggle. It was also, quietly, an API-contract
toggle: the fast path returned `fast_decide()`'s dict verbatim, and that dict had **no
`decision_id` key** -- the word appears nowhere in fast_decide.py. The slow path has returned
one since it existed, and the reason it exists is to let an integrator deep-link
`GET /api/v1/decisions/{id}/reason-codes` or open the case without a second lookup. So an
operator flipping a flag described as "make it faster" broke every such integration, silently,
with a 200 response that simply lacked a field.

fast_decide.py warns about exactly this class of divergence three times -- for fail-closed
semantics, for PII redaction, for the client_id collision. Each was found the same way: someone
compared the two paths. The response shape was the one nobody compared.

This file compares it, both directions. The `policy_version` half is the same defect
introduced from the other side while fixing the first: the fast path started reporting which
version decided, and for a moment the slow path did not.
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

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

# Keys every /decide response carries, on either path. Asserted as a SET rather than one key at
# a time so a future field added to one branch and not the other fails here.
CONTRACT_KEYS = {
    "policy_id", "decision_id", "policy_version", "outcome", "score", "variables",
    "variable_errors", "rule_results", "scorecard_result", "trace", "latency_ms",
}


class DecideResponseShapeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "shape.db"))
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant = app_main.storage.default_tenant_id
        # A policy the fast path will actually ACCEPT. The seeded lending policies mostly carry
        # `action` / `review_gate` steps, which are in _IO_STEP_TYPES and fail eligibility
        # outright -- which is why the repo's own load test has never exercised the fast path.
        from app.fast_decide import fast_path_eligible

        self.policy_id = None
        for policy in app_main.storage.list_policies(tenant_id=self.tenant):
            if fast_path_eligible(app_main.storage, policy, self.tenant):
                self.policy_id = policy["id"]
                break
        self.assertIsNotNone(
            self.policy_id,
            "no seeded policy is fast-path eligible, so this test would silently compare the "
            "slow path with itself -- which is the failure it exists to catch")

    def _decide(self, fast: str):
        with mock.patch.dict(os.environ, {"FAST_DECIDE": fast}):
            response = self.client.post("/api/v1/decide", headers=self.headers,
                                        json={"policy_id": self.policy_id, "payload": {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_both_paths_were_actually_exercised(self):
        """The guard that makes every other assertion here mean something. If eligibility
        failed, `FAST_DECIDE=1` silently falls through to the executor and this file would
        compare the slow path with the slow path and pass."""
        self.assertEqual(self._decide("1").get("source", "api_fast"), "api_fast")
        rows = app_main.storage.list_decisions(tenant_id=self.tenant, limit=5)
        self.assertIn("api_fast", {row.get("source") for row in rows},
                      "FAST_DECIDE=1 did not take the fast path")

    def test_the_two_paths_return_the_same_keys(self):
        slow = set(self._decide("0"))
        fast = set(self._decide("1"))
        self.assertEqual(slow - fast, set(), f"keys the FAST path dropped: {slow - fast}")
        self.assertEqual(fast - slow, set(), f"keys the SLOW path lacks: {fast - slow}")

    def test_both_paths_carry_the_whole_contract(self):
        for flag in ("0", "1"):
            with self.subTest(fast_decide=flag):
                self.assertEqual(CONTRACT_KEYS - set(self._decide(flag)), set())

    def test_the_fast_path_decision_id_names_a_row_that_exists(self):
        """A decision_id that does not resolve is worse than none: the integration looks fixed
        and 404s in production. This is the assertion that would have failed had the id been
        minted separately inside _log_decision, as it was."""
        body = self._decide("1")
        self.assertTrue(body["decision_id"])
        stored = app_main.storage.get_decision(body["decision_id"], tenant_id=self.tenant)
        self.assertIsNotNone(stored, "the returned decision_id names no Decision row")
        self.assertEqual(stored["outcome"], body["outcome"])

    def test_the_slow_path_decision_id_names_a_row_that_exists(self):
        body = self._decide("0")
        self.assertTrue(body["decision_id"])
        self.assertIsNotNone(
            app_main.storage.get_decision(body["decision_id"], tenant_id=self.tenant))

    def test_both_paths_report_the_policy_version(self):
        policy = app_main.storage.get_policy(self.policy_id, tenant_id=self.tenant)
        for flag in ("0", "1"):
            with self.subTest(fast_decide=flag):
                self.assertEqual(self._decide(flag)["policy_version"], policy.get("version"))

    def test_a_simulation_gets_no_decision_id_rather_than_a_fabricated_one(self):
        """log=False writes no row. Returning an id there would name nothing -- the same class
        of fabrication as the hardcoded adverse-action reason codes this round removed. The KEY
        stays present, because a key that comes and goes is its own divergence."""
        from app.fast_decide import fast_decide

        policy = app_main.storage.get_policy(self.policy_id, tenant_id=self.tenant)
        result = fast_decide(app_main.storage, policy, {}, self.tenant, log=False)
        self.assertIn("decision_id", result)
        self.assertIsNone(result["decision_id"])


if __name__ == "__main__":
    unittest.main()
