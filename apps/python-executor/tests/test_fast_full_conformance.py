"""Fast path == full executor — the guard against the two decision paths drifting.

The A/B bug came from the fast path and the executor diverging on behaviour. This runs a
rules-only (fast-servable) policy through BOTH engines over many payloads spanning decision
boundaries and asserts identical outcomes, plus checks the single fast-eligibility authority.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402
from app.fast_decide import fast_decide, fast_path_eligible, is_fast_servable  # noqa: E402


class FastFullConformanceTests(unittest.TestCase):
    def setUp(self):
        self.storage = app_main.storage
        self.tenant_id = self.storage.default_tenant_id
        # Build a rules-only (fast-servable) policy from seeded loan rules + an outcome step.
        rule_ids = [r["id"] for r in self.storage.list_rules(status="prod", tenant_id=self.tenant_id)
                    if r["id"].startswith("rule_loan_")][:3]
        self.assertTrue(rule_ids, "expected seeded loan rules")
        self.policy = {
            "id": "conf_fast_policy",
            "name": "Conformance",
            "defaultOutcome": "review",
            "steps": [{"type": "rule", "ref_id": rid} for rid in rule_ids]
                     + [{"type": "outcome", "ref_id": "approve"}],
        }

    def _payloads(self):
        payloads = []
        for bureau in (300, 620, 690, 700, 720, 800, 850):
            for dti in (0.1, 0.4, 0.45, 0.5, 0.9):
                payloads.append({"bureau_score": bureau, "dti_ratio": dti, "pan_verified": 1,
                                 "liveness_score": 95, "geo_flag": 0, "amount": 5000, "income": 50000})
        return payloads

    def _full(self, payload):
        ctx = asyncio.run(PolicyExecutor(self.storage).execute(
            policy=self.policy, payload=payload, tenant_id=self.tenant_id, source="conformance", simulate=True))
        return ctx.outcome if ctx.outcome != "pending" else (self.policy.get("defaultOutcome") or "review")

    def test_policy_is_fast_servable(self):
        self.assertTrue(is_fast_servable(self.policy))

    def test_fast_and_full_agree_on_every_payload(self):
        mismatches = []
        for payload in self._payloads():
            fast = fast_decide(self.storage, self.policy, payload, self.tenant_id, log=False)["outcome"]
            full = self._full(payload)
            if fast != full:
                mismatches.append((payload, fast, full))
        self.assertEqual(mismatches, [], "fast path diverged from the executor: {0}".format(mismatches[:5]))

    def test_fast_eligibility_is_the_single_authority(self):
        # Eligible when rules-only and no running experiment...
        self.assertTrue(fast_path_eligible(self.storage, self.policy, self.tenant_id))
        # ...not eligible once a running experiment targets it (fast path skips overrides).
        exp = self.storage.create_or_update_experiment(
            {"id": "conf_exp", "name": "c", "status": "running", "target_policy_id": self.policy["id"],
             "variants": [{"id": "champion", "role": "champion", "weight": 100}]},
            tenant_id=self.tenant_id,
        )
        try:
            self.assertFalse(fast_path_eligible(self.storage, self.policy, self.tenant_id))
        finally:
            self.storage.create_or_update_experiment({**exp, "status": "completed"}, tenant_id=self.tenant_id)
        # ...and never eligible for a policy with an I/O step (action/review_gate/transform/model).
        io_policy = {"id": "io", "steps": [{"type": "action", "ref_id": "x"}, {"type": "rule", "ref_id": "y"}]}
        self.assertFalse(fast_path_eligible(self.storage, io_policy, self.tenant_id))


if __name__ == "__main__":
    unittest.main()
