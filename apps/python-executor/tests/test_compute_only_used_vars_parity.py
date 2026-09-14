"""COMPUTE_ONLY_USED_VARS must be a performance flag, not a correctness switch (J14).

The flag restricts variable computation to what a policy can actually reach. It was wired into
PolicyExecutor only, so turning it on made the executor compute a filtered set while
`fast_decide` still computed the tenant's entire catalog -- the two decision paths disagreeing
about the workload for the SAME policy. That is the FAST_DECIDE divergence class this codebase
has already fixed four times (input validation, evaluation budget, sample seeding, and this).

What this module pins, per policy and per payload:

  1. the two paths agree on the OUTCOME, flag on and flag off;
  2. the two paths compute the SAME VARIABLE KEYS, flag on and flag off -- an outcome-only
     assertion cannot see a path that filtered differently but happened to decide the same;
  3. the flag does not change either path's outcome;
  4. the flag ACTUALLY FILTERS -- the on-set is a strict subset of the off-set. Without this the
     whole module passes when the filter does nothing at all, which is the failure mode it
     exists to catch.

Loop, branch and workflow steps are absent deliberately, not by oversight: `_IO_STEP_TYPES`
makes those policies ineligible for the fast path, so there is no second path to compare
against. The reachability walk's arms for them are exercised by the executor suites.
"""
from __future__ import annotations

import asyncio
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

import app.fast_decide as fast_decide_module  # noqa: E402
import app.main as app_main  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402
from app.fast_decide import fast_decide, is_fast_servable  # noqa: E402
from app.storage import Storage  # noqa: E402

DECISION_TABLE = {
    "id": "dt_parity",
    "name": "Parity Table",
    "hit_policy": "first",
    "inputs": [{"id": "in_dti", "variable_id": "loan_dti_ratio"}],
    "outputs": [{"id": "band", "name": "band"}],
    "rows": [
        {"id": "r1", "when": {"in_dti": {"operator": "<", "value": 0.45}}, "then": {"band": "ok"}},
        {"id": "r2", "when": {"in_dti": {"operator": ">=", "value": 0.45}}, "then": {"band": "high"}},
    ],
    "status": "prod",
}


class ComputeOnlyUsedVarsParityTests(unittest.TestCase):
    """Both paths, both flag states, over policies covering every fast-servable step type."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "usedvars.db"))
        self.storage = app_main.storage
        self.tenant_id = self.storage.default_tenant_id

        rule_ids = [r["id"] for r in self.storage.list_rules(status="prod", tenant_id=self.tenant_id)
                    if r["id"].startswith("rule_loan_")][:3]
        self.assertTrue(rule_ids, "expected seeded loan rules")
        scorecard_ids = [s["id"] for s in self.storage.list_scorecards(tenant_id=self.tenant_id)][:1]
        self.assertTrue(scorecard_ids, "expected a seeded scorecard")
        connector_ids = [c["id"] for c in self.storage.list_connectors(tenant_id=self.tenant_id)][:1]

        self.storage.create_decision_table(dict(DECISION_TABLE), tenant_id=self.tenant_id)

        # One policy per step type the walk has an arm for AND the fast path will serve.
        self.policies = {
            "rules_only": {
                "id": "uv_rules", "name": "Rules", "defaultOutcome": "review",
                "steps": [{"type": "rule", "ref_id": rid} for rid in rule_ids]
                         + [{"type": "outcome", "ref_id": "approve"}],
            },
            "scorecard": {
                "id": "uv_scored", "name": "Scored", "defaultOutcome": "review",
                "steps": [{"type": "rule", "ref_id": rule_ids[0]},
                          {"type": "scorecard", "ref_id": scorecard_ids[0]},
                          {"type": "outcome", "ref_id": "approve"}],
            },
            "decision_table": {
                "id": "uv_table", "name": "Tabled", "defaultOutcome": "review",
                "steps": [{"type": "decision_table", "ref_id": "dt_parity"},
                          {"type": "outcome", "ref_id": "approve"}],
            },
        }
        if connector_ids:
            self.policies["connector"] = {
                "id": "uv_conn", "name": "Connected", "defaultOutcome": "review",
                "steps": [{"type": "connector", "ref_id": connector_ids[0]},
                          {"type": "rule", "ref_id": rule_ids[0]},
                          {"type": "outcome", "ref_id": "approve"}],
            }
        for policy in self.policies.values():
            self.storage.create_policy(dict(policy), tenant_id=self.tenant_id)
            self.assertTrue(is_fast_servable(policy),
                            "{0} is not fast-servable, so there is no second path to compare "
                            "against and this test would compare the executor with itself"
                            .format(policy["id"]))
        fast_decide_module.invalidate()

    def tearDown(self):
        fast_decide_module.invalidate()
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        try:
            app_main.storage = self._orig_storage
        except Exception:  # pragma: no cover
            pass

    def _payloads(self):
        """Across the decision boundaries the seeded rules gate on, not just one happy case."""
        return [
            {"bureau_score": score, "dti_ratio": dti, "pan_verified": 1, "liveness_score": 95,
             "geo_flag": 0, "amount": 5000, "income": 50000}
            for score in (300, 620, 700, 800)
            for dti in (0.1, 0.45, 0.9)
        ]

    @staticmethod
    def _with_flag(on):
        env = dict(os.environ)
        env["COMPUTE_ONLY_USED_VARS"] = "1" if on else "0"
        return mock.patch.dict(os.environ, env, clear=True)

    def _fast(self, policy, payload, flag_on):
        with self._with_flag(flag_on):
            decision = fast_decide(self.storage, policy, payload, self.tenant_id, log=False)
        return decision["outcome"], set(decision["variables"] or {})

    def _executor(self, policy, payload, flag_on):
        # simulate=False and source="api": PRODUCTION semantics on both sides. `simulate=True`
        # would filter variables regardless of the flag (executor.py reads
        # `if simulate or <flag>`), so the flag would not be the thing under test.
        with self._with_flag(flag_on):
            ctx = asyncio.run(PolicyExecutor(self.storage).execute(
                policy=policy, payload=payload, tenant_id=self.tenant_id,
                source="api", simulate=False))
        outcome = ctx.outcome if ctx.outcome != "pending" else (policy.get("defaultOutcome") or "review")
        return outcome, set(ctx.variables or {})

    # ----------------------------------------------------------------- the parity assertions

    def test_both_paths_agree_on_outcome_and_on_which_variables_they_computed(self):
        for name, policy in self.policies.items():
            for payload in self._payloads():
                for flag_on in (False, True):
                    with self.subTest(policy=name, flag=flag_on, dti=payload["dti_ratio"],
                                      score=payload["bureau_score"]):
                        fast_outcome, fast_keys = self._fast(policy, payload, flag_on)
                        full_outcome, full_keys = self._executor(policy, payload, flag_on)
                        self.assertEqual(
                            fast_outcome, full_outcome,
                            "the two paths decided differently with the flag {0}".format(
                                "ON" if flag_on else "OFF"))
                        self.assertEqual(
                            fast_keys, full_keys,
                            "the two paths computed different variables with the flag {0}: "
                            "fast-only={1} executor-only={2}".format(
                                "ON" if flag_on else "OFF",
                                sorted(fast_keys - full_keys), sorted(full_keys - fast_keys)))

    def test_the_flag_does_not_change_the_decision_on_either_path(self):
        """The definition of a performance flag. If an outcome moves, the filter dropped a
        variable the policy actually reads."""
        for name, policy in self.policies.items():
            for payload in self._payloads():
                with self.subTest(policy=name, dti=payload["dti_ratio"]):
                    self.assertEqual(self._fast(policy, payload, True)[0],
                                     self._fast(policy, payload, False)[0],
                                     "the fast path decided differently with the flag on")
                    self.assertEqual(self._executor(policy, payload, True)[0],
                                     self._executor(policy, payload, False)[0],
                                     "the executor decided differently with the flag on")

    def test_the_flag_actually_filters_on_both_paths(self):
        """Without this, every assertion above passes when the filter does nothing.

        The seeded tenant has 55 variables and none of these policies reads anywhere near all of
        them, so a strict subset is the correct expectation on BOTH paths -- and a path that
        silently ignored the flag would show an equal set here.
        """
        payload = self._payloads()[0]
        for name, policy in self.policies.items():
            with self.subTest(policy=name):
                fast_on, fast_off = self._fast(policy, payload, True)[1], self._fast(policy, payload, False)[1]
                full_on, full_off = self._executor(policy, payload, True)[1], self._executor(policy, payload, False)[1]
                # Non-empty first. An EMPTY on-set is a strict subset of anything, so a policy
                # whose variables were all dropped -- the exact failure a typo in a decision
                # table's variable_id produces -- would otherwise satisfy the subset assertion
                # and prove the reverse of what this test claims. It caught one while being
                # written: a table naming a variable that does not exist reaches nothing.
                self.assertTrue(fast_on, "the fast path computed NO variables for this policy")
                self.assertTrue(full_on, "the executor computed NO variables for this policy")
                self.assertTrue(fast_on < fast_off,
                                "the fast path ignored the flag: {0} vs {1} variables".format(
                                    len(fast_on), len(fast_off)))
                self.assertTrue(full_on < full_off,
                                "the executor ignored the flag: {0} vs {1} variables".format(
                                    len(full_on), len(full_off)))

    def test_the_flag_is_honoured_per_request_not_baked_into_the_cached_bundle(self):
        """The bundle caches the reachable SET; the flag is applied when the request is served.

        Baking the flag's answer into the cached bundle would make it sticky per cache entry --
        the first request after a rebuild would decide the mode for every later one, so flipping
        the flag would appear to do nothing until the cache expired. The cache is deliberately
        NOT invalidated between the two calls below.
        """
        policy = self.policies["rules_only"]
        payload = self._payloads()[0]
        first = self._fast(policy, payload, False)[1]     # populates the cache, unfiltered
        second = self._fast(policy, payload, True)[1]     # same cached bundle, filtered
        third = self._fast(policy, payload, False)[1]     # and back again
        self.assertTrue(second < first, "the flag was ignored on an already-cached bundle")
        self.assertEqual(third, first, "the filtered request poisoned the cached bundle")

    def test_the_reachable_set_is_on_the_bundle_and_covers_what_the_policy_reads(self):
        """The set is computed once at build. A policy gating on dti_ratio must reach it --
        that is the variable its decision table and rules read."""
        policy = self.policies["decision_table"]
        bundle = fast_decide_module._serving_bundle(self.storage, self.tenant_id, policy)
        self.assertIn("loan_dti_ratio", bundle["needed_variable_ids"],
                      "the decision table's input variable was not reachable, so under the flag "
                      "it would never be computed and the table would fall through to default")


if __name__ == "__main__":
    unittest.main()
