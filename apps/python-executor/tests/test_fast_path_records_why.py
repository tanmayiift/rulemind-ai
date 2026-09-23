"""A decision the fast path records must be able to say WHY.

The bug: `fast_decide` had two branches. When the Rust extension was importable and the policy
had no scorecard or decision table, it served the decision from `rulemind_core_rs.Bundle` and
returned

    {"score": None, "rule_results": [], "trace": []}

hardcoded, while the Python branch one line below returned all three populated. `trace` and
`rule_results` are roughly two thirds of the stored Decision row, and together they are the
entire answer to "why was this rejected" -- the question this product exists to answer. So a
decision taken through that branch was written to the audit trail as UNEXPLAINABLE: same
outcome, no reasons, and nothing anywhere recording that the reasons were dropped.

It was live. The Rust extension IS built into this repo's venv, so `_HAVE_RUST` was true for
every host test run, and any rules-only policy took it.

These assertions are about the RECORD, not the branch, deliberately: "the outcome must be
accompanied by its reasons" survives whatever the fast path is implemented with, and would
have failed on the old code without naming Rust at all.
"""
from __future__ import annotations

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

import app.main as app_main  # noqa: E402
from app.fast_decide import fast_decide  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

RULE = {
    "id": "r_why",
    "name": "Why floor",
    "tree": {
        "type": "group", "logic": "AND",
        "children": [{"type": "condition", "variable": "why_score",
                      "operator": ">=", "value": 700}],
        "onPass": "approve", "onFail": "reject",
    },
}
# Rules and an outcome only -- no scorecard, no decision table. Exactly the shape the deleted
# Rust branch claimed, so this fixture is the one that would have exercised it.
POLICY = {
    "id": "p_why", "name": "Rules-only", "status": "dev", "version": 1,
    "steps": [{"type": "rule", "ref_id": "r_why"}, {"type": "outcome", "ref_id": "approve"}],
}


class FastPathRecordsWhyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "why.db"))
        app_main.storage = self.storage
        self.tenant = self.storage.default_tenant_id
        self.storage.create_variable(
            {"id": "why_score", "name": "Why score", "category": "custom",
             "source_id": "custom", "status": "dev",
             # The @variable decorator is required: sandbox.resolve_callable EXCLUDES a bare
             # function literally named `variable` from its fallback scan, so the obvious
             # `def variable(payload, variables)` resolves to nothing and the value comes back
             # null with "No callable variable function found." -- which then fails the rule on
             # a null and looks like a rule bug.
             "code": "@variable(source=\"custom\")\ndef why_score(payload, variables, apis):\n"
                     "    return payload.get(\"why_score\")\n"},
            tenant_id=self.tenant)
        self.storage.create_rule(RULE, tenant_id=self.tenant)
        self.storage.create_policy(POLICY, tenant_id=self.tenant)
        self.policy = self.storage.get_policy("p_why", tenant_id=self.tenant)

    def _decide(self, score):
        return fast_decide(self.storage, self.policy, {"why_score": score}, self.tenant)

    def test_a_rejection_carries_the_rule_that_rejected_it(self):
        decision = self._decide(650)
        self.assertEqual(decision["outcome"], "reject")
        self.assertTrue(decision["rule_results"],
                        "the decision recorded no rule results -- it cannot say why")
        self.assertIn("r_why", {r.get("rule_id") or r.get("ruleId")
                                for r in decision["rule_results"]})

    def test_a_rejection_carries_the_condition_that_failed(self):
        """The level that matters to an adverse-action notice: not "a rule failed" but WHICH
        comparison, against WHAT threshold, with WHAT value. `adverse_reason_codes` reads
        exactly this, so an empty trace makes the reason-code endpoint return nothing --
        which is how the UI came to hardcode reason codes in the first place."""
        conditions = [c for r in self._decide(650)["rule_results"]
                      for c in r.get("conditions", [])]
        self.assertTrue(conditions, "no conditions recorded")
        failed = [c for c in conditions if not c.get("passed")]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["variable_id"], "why_score")
        self.assertEqual(failed[0]["threshold"], 700)
        self.assertEqual(failed[0]["value"], 650)

    def test_the_step_trace_is_not_empty(self):
        trace = self._decide(650)["trace"]
        self.assertTrue(trace, "the decision recorded an empty trace")
        self.assertIn("rule", {(entry.get("step") or {}).get("type") for entry in trace})

    def test_the_stored_row_carries_the_same_reasons_as_the_response(self):
        """The response and the row must agree. The bug emptied both, so asserting only the
        response would have passed on a half-fix that populated one of them."""
        decision = self._decide(650)
        stored = self.storage.get_decision(decision["decision_id"], tenant_id=self.tenant)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["rule_results"], decision["rule_results"])
        self.assertEqual(stored["trace"], decision["trace"])

    def test_an_approval_records_its_reasons_too(self):
        decision = self._decide(750)
        self.assertEqual(decision["outcome"], "approve")
        self.assertTrue(decision["rule_results"])
        self.assertTrue(decision["trace"])

    def test_reason_codes_can_be_derived_from_what_the_fast_path_stored(self):
        """End to end on the property that matters: the deterministic reason-code
        implementation reads the stored rule_results. With those empty it returns nothing, and
        'declined for no stated reason' is what a lender cannot send."""
        from app.logic import adverse_reason_codes

        decision = self._decide(650)
        stored = self.storage.get_decision(decision["decision_id"], tenant_id=self.tenant)
        codes = adverse_reason_codes(stored)
        self.assertTrue(codes, "no adverse-action reason codes could be derived")

    def test_the_fast_path_builds_no_rust_bundle(self):
        """Source-level, and the only assertion here that names the mechanism. The branch is
        gone; this stops it coming back as an optimisation without the trace question being
        answered first. If it does come back populated from `decide_traced`, delete this and
        keep everything above -- those assertions are the real contract."""
        from app import fast_decide as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("rulemind_core_rs.Bundle", code)
        self.assertNotIn("rust_bundle", code)


if __name__ == "__main__":
    unittest.main()
