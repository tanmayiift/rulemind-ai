"""An unpromoted draft that no policy uses is not part of a production decision.

Compute-everything mode (the default) used to compute the workspace's WHOLE catalog, drafts
included, and any variable that errored sent the decision to review. So an author saving a
broken DEV variable -- used by nothing -- put every live decision into review. On 2026-09-24 two
leftover test variables did that to a running soak for 3.5 minutes.

Now a decision computes every promoted variable, every variable its policy reaches (whatever the
status), and whatever those read (`variable_reachability.promoted_or_reached`), on both paths.
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
from app import fast_decide  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

FAST_POLICY = "policy_pl_underwriting"          # FAST_DECIDE accepts it
POLICY = "policy_instant_personal_loan"         # reaches 8 variables, one of them a draft


def fn(body: str) -> str:
    return "@variable\ndef v(payload, variables):\n    " + body + "\n"


class UnpromotedVariableTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "drafts.db"))
        app_main.storage = self.storage
        self.tenant = self.storage.default_tenant_id
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.headers = {"x-api-key": self.storage.default_api_key or ""}

    def add(self, variable_id: str, code: str, status: str) -> None:
        self.storage.create_variable({"id": variable_id, "name": variable_id, "category": "Custom",
                                      "source_id": "custom", "code": code, "status": status},
                                     tenant_id=self.tenant)
        fast_decide.invalidate(self.tenant)

    def decide(self, policy_id: str, fast: str = "0", env: dict | None = None) -> dict:
        with mock.patch.dict(os.environ, {"FAST_DECIDE": fast, **(env or {})}):
            response = self.client.post("/api/v1/decide", headers=self.headers,
                                        json={"policy_id": policy_id, "payload": {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_broken_unused_draft_does_not_touch_a_production_decision(self):
        for flag, policy in (("0", POLICY), ("1", FAST_POLICY)):
            with self.subTest(fast_decide=flag):
                before = self.decide(policy, flag)
                self.add(f"zz_broken_{flag}", "value = 0", "dev")   # no function: errors if computed
                after = self.decide(policy, flag)
                self.assertNotIn(f"zz_broken_{flag}", after["variables"])
                self.assertEqual(after["variable_errors"], {}, "the unused draft was evaluated")
                self.assertEqual(after["outcome"], before["outcome"], "an unused draft changed the outcome")

    def test_a_draft_the_policy_reads_is_still_computed(self):
        """Reached ONLY through the policy -- no promoted variable reads it. (The first version
        used the seeded `avg_balance`, which a promoted variable also reads, so the dependency
        closure pulled it in and the test passed with the "reached" rule deleted.)"""
        self.add("zz_policy_draft", fn("return 3"), "dev")
        self.storage.create_rule(
            {"id": "zz_draft_gate", "name": "draft gate", "status": "prod", "outcome": "approve",
             "nodes": [{"id": "n1", "type": "condition", "variable": "zz_policy_draft", "operator": ">", "value": "1"},
                       {"id": "n2", "type": "approve"}]}, tenant_id=self.tenant)
        self.storage.create_policy(
            {"id": "zz_draft_policy", "name": "draft policy", "status": "prod", "defaultOutcome": "review",
             "steps": [{"id": "s1", "type": "rule", "ref_id": "zz_draft_gate"}]}, tenant_id=self.tenant)
        fast_decide.invalidate(self.tenant)
        body = self.decide("zz_draft_policy")
        self.assertEqual(body["variables"].get("zz_policy_draft"), 3)
        self.assertEqual(body["outcome"], "approve")

    def test_promoted_variables_the_policy_does_not_read_are_still_recorded(self):
        self.add("zz_promoted_unused", fn("return 7"), "prod")
        body = self.decide(POLICY)
        self.assertEqual(body["variables"].get("zz_promoted_unused"), 7)
        stored = self.storage.get_decision(body["decision_id"], tenant_id=self.tenant)
        self.assertEqual(stored["computed_variables"].get("zz_promoted_unused"), 7)

    def test_a_promoted_variable_brings_the_drafts_it_reads(self):
        self.add("zz_base_draft", fn("return 5"), "dev")
        self.add("zz_derived", fn("return variables['zz_base_draft'] + 1"), "prod")
        body = self.decide(POLICY)
        self.assertEqual((body["variables"].get("zz_base_draft"), body["variables"].get("zz_derived")), (5, 6))
        self.assertEqual(body["variable_errors"], {})

    def test_both_paths_compute_the_same_set(self):
        self.add("zz_unused_draft", fn("return 1"), "dev")
        self.add("zz_unused_prod", fn("return 2"), "prod")
        slow, fast = self.decide(FAST_POLICY, "0"), self.decide(FAST_POLICY, "1")
        self.assertEqual(set(slow["variables"]), set(fast["variables"]))
        self.assertNotIn("zz_unused_draft", slow["variables"])
        self.assertIn("zz_unused_prod", slow["variables"])

    def test_compute_only_used_vars_still_computes_only_what_the_policy_reads(self):
        self.add("zz_unused_prod", fn("return 2"), "prod")
        body = self.decide(POLICY, env={"COMPUTE_ONLY_USED_VARS": "1"})
        self.assertNotIn("zz_unused_prod", body["variables"])
        self.assertIn("avg_balance", body["variables"])


if __name__ == "__main__":
    unittest.main()
