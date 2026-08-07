"""Cross-engine LARGE-policy conformance — the Python arm (source of truth).

A single v2 rule nesting 600 conditions across 750 variables (all 12 operators, mixed
AND/OR/NOT) is evaluated over 135 payloads — including ones that OMIT ~30% of variables
(the missing-variable parity case) and payloads where 500+ conditions are true. The
committed fixture packages/shared/large-policy.spec.json records, per case, the Python
outcome and the number of passed conditions; the Kotlin and Dart on-device engines must
reproduce BOTH exactly (see LargePolicyConformanceTest.kt / large_policy_conformance_test.dart).

This arm asserts the fixture still matches the live Python engine (catches any drift in
app/logic.py) and documents the ≥500-true-condition / ≥700-variable coverage.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.logic import evaluate_rule_definition, evaluate_rule_tree  # noqa: E402

SPEC = json.loads((APP_ROOT.parents[1] / "packages" / "shared" / "large-policy.spec.json").read_text())


class LargePolicyConformanceTests(unittest.TestCase):
    def test_every_case_reproduces_outcome_and_true_count(self):
        rule = SPEC["rule"]
        for i, case in enumerate(SPEC["cases"]):
            outcome = evaluate_rule_definition(rule, case["variables"])["outcome"]
            self.assertEqual(outcome, case["expectedOutcome"], f"case {i} outcome")
            passed = sum(1 for c in evaluate_rule_tree(rule["tree"], case["variables"])["conditions"] if c["passed"])
            self.assertEqual(passed, case["trueConditions"], f"case {i} passed-count")

    def test_threshold_rule_boundary_and_negatives(self):
        """The flat-AND threshold rule (onFail=reject): every case reproduces its outcome + exact
        passed-count, AND the boundary is sharp — 500/500 true approves, 499/500 rejects. This is the
        negative/boundary coverage: a sub-threshold input MUST fail, not pass."""
        rule = SPEC["thresholdRule"]
        by_target = {}
        for i, case in enumerate(SPEC["thresholdCases"]):
            outcome = evaluate_rule_definition(rule, case["variables"])["outcome"]
            passed = sum(1 for c in evaluate_rule_tree(rule["tree"], case["variables"])["conditions"] if c["passed"])
            self.assertEqual(outcome, case["expectedOutcome"], f"threshold case {i} ({case['label']}) outcome")
            self.assertEqual(passed, case["trueConditions"], f"threshold case {i} ({case['label']}) count")
            self.assertEqual(passed, case["targetTrue"], f"threshold case {i} exact target count")
            by_target[case["targetTrue"]] = outcome
        n = SPEC["meta"]["thresholdConditions"]
        self.assertEqual(by_target[n], "approve", "all-true must approve")
        self.assertEqual(by_target[n - 1], "reject", "one-below-threshold MUST reject (negative case)")
        self.assertEqual(by_target[0], "reject", "none-true must reject")
        # reject is actually represented in the corpus now (the old fixture had none)
        self.assertIn("reject", {c["expectedOutcome"] for c in SPEC["thresholdCases"]})

    def test_fixture_actually_exercises_scale(self):
        self.assertEqual(SPEC["meta"]["conditions"], 600)
        # threshold rule provides a clean count boundary + reject outcomes
        self.assertEqual(SPEC["meta"]["thresholdConditions"], 500)
        self.assertTrue(any(c["expectedOutcome"] == "reject" for c in SPEC["thresholdCases"]))
        # at least one case has 500+ conditions true, and 700+ variables supplied
        self.assertTrue(any(c["trueConditions"] >= 500 for c in SPEC["cases"]))
        self.assertTrue(any(len(c["variables"]) >= 700 for c in SPEC["cases"]))
        # missing-variable coverage: some payloads omit many variables
        self.assertTrue(any(len(c["variables"]) < 550 for c in SPEC["cases"]))

    def test_depth_guard_raises_on_pathological_nesting(self):
        from app.logic import MAX_RULE_TREE_DEPTH
        node = {"type": "condition", "variable": "x", "operator": ">=", "value": 1}
        for _ in range(MAX_RULE_TREE_DEPTH + 5):
            node = {"type": "group", "logic": "AND", "children": [node]}
        with self.assertRaises(ValueError):
            evaluate_rule_tree(node, {"x": 5})

    def test_all_twelve_operators_present(self):
        ops = set()
        def walk(node):
            if node.get("type") == "condition":
                ops.add(node.get("operator"))
            for child in node.get("children", []):
                walk(child)
            if node.get("child"):
                walk(node["child"])
        walk(SPEC["rule"]["tree"])
        self.assertEqual(ops, {"==", "!=", ">", ">=", "<", "<=", "between", "in", "not_in", "regex", "exists", "!exists"})


if __name__ == "__main__":
    unittest.main()
