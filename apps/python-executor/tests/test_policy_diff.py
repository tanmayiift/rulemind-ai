"""Policy diff before promotion — see what decision logic changed, recorded on the approval.

Each promotion snapshots the policy's decision definition; the diff endpoint compares the working
policy against the last promoted snapshot (steps added/removed, rules/scorecards/tables changed).
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.policy_diff import diff_snapshots, policy_snapshot  # noqa: E402


class PolicyDiffTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        self.tenant_id = app_main.storage.default_tenant_id

    def test_snapshot_captures_steps_and_referenced_rules(self):
        policy = app_main.storage.list_policies(status="prod", tenant_id=self.tenant_id)[0]
        snap = policy_snapshot(app_main.storage, self.tenant_id, policy)
        self.assertIn("steps", snap)
        self.assertTrue(snap["steps"])
        rule_ids = {s["ref_id"] for s in snap["steps"] if s["type"] == "rule"}
        self.assertTrue(rule_ids.issubset(set(snap["rules"].keys())), "referenced rule defs captured")

    def test_diff_detects_added_and_removed_steps(self):
        base = {"steps": [{"type": "rule", "ref_id": "a"}], "rules": {"a": {"tree": {}}}, "scorecards": {}, "decisionTables": {}}
        current = {"steps": [{"type": "rule", "ref_id": "a"}, {"type": "rule", "ref_id": "b"}],
                   "rules": {"a": {"tree": {}}, "b": {"tree": {"x": 1}}}, "scorecards": {}, "decisionTables": {}}
        d = diff_snapshots(current, base)
        self.assertTrue(d["changed"])
        self.assertEqual(d["steps"]["added"], [{"type": "rule", "ref_id": "b"}])
        self.assertEqual(d["steps"]["removed"], [])
        self.assertIn("b", d["rules"]["added"])

    def test_diff_detects_changed_rule_definition(self):
        base = {"steps": [{"type": "rule", "ref_id": "a"}], "rules": {"a": {"tree": {"op": "<", "value": 700}}}, "scorecards": {}, "decisionTables": {}}
        current = {"steps": [{"type": "rule", "ref_id": "a"}], "rules": {"a": {"tree": {"op": "<", "value": 680}}}, "scorecards": {}, "decisionTables": {}}
        d = diff_snapshots(current, base)
        self.assertTrue(d["changed"])
        self.assertIn("a", d["rules"]["changed"])

    def test_no_baseline_reports_changed_true(self):
        current = {"steps": [{"type": "rule", "ref_id": "a"}], "rules": {}, "scorecards": {}, "decisionTables": {}}
        d = diff_snapshots(current, None)
        self.assertFalse(d["hasBaseline"])
        self.assertTrue(d["changed"], "first promotion is all-new")

    def test_diff_endpoint_after_promotion(self):
        # Build a draft policy referencing a seeded rule, promote it through to prod, then diff.
        rule = app_main.storage.list_rules(status="prod", tenant_id=self.tenant_id)[0]
        pid = "diff_pol_" + uuid.uuid4().hex[:8]
        created = app_main.storage.create_policy(
            {"id": pid, "name": "Diff Policy", "status": "dev",
             "steps": [{"type": "rule", "ref_id": rule["id"]}, {"type": "outcome", "ref_id": "approve"}]},
            tenant_id=self.tenant_id,
        )
        # Record a promotion snapshot as the live baseline (the promote endpoint's own test-gate
        # is exercised elsewhere; here we assert the snapshot -> diff flow).
        app_main.storage.add_promotion(
            "policy", pid, "dev", "uat", "tester", "ship",
            tenant_id=self.tenant_id, snapshot=policy_snapshot(app_main.storage, self.tenant_id, created),
        )
        # No edits since the last promotion -> diff reports no change.
        diff = self.client.get("/api/v1/policies/{0}/diff".format(pid), headers=self.headers).json()
        self.assertTrue(diff["hasBaseline"])
        self.assertFalse(diff["changed"])
        # Now add a step -> the diff surfaces it.
        current = app_main.storage.get_policy(pid, tenant_id=self.tenant_id)
        steps = current["steps"]
        steps.insert(0, {"type": "rule", "ref_id": "some_other_rule"})
        app_main.storage.update_policy(pid, {"steps": steps}, tenant_id=self.tenant_id)
        diff2 = self.client.get("/api/v1/policies/{0}/diff".format(pid), headers=self.headers).json()
        self.assertTrue(diff2["changed"])
        self.assertTrue(any(s["ref_id"] == "some_other_rule" for s in diff2["steps"]["added"]))


if __name__ == "__main__":
    unittest.main()
