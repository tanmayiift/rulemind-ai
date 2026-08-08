"""
Operator-parity tests for the server runtime.

Guards the fix that brought the Python runtime up to the full 12-operator set
(== != > >= < <= between in not_in regex exists !exists) so it matches the
TypeScript engine (packages/rule-engine) and the MECE analyzer. Before the fix,
in/not_in/regex/exists/!exists/between silently evaluated to False.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")

from app.logic import compare, evaluate_rule_tree

# The canonical cross-engine operator contract, shared verbatim with the TS,
# Kotlin, and Dart engines. See packages/shared/operators.spec.json.
SPEC_PATH = APP_ROOT.parents[1] / "packages" / "shared" / "operators.spec.json"


class OperatorConformanceTests(unittest.TestCase):
    """Python arm of the cross-engine operator conformance suite."""

    def setUp(self) -> None:
        self.spec = json.loads(SPEC_PATH.read_text())

    def test_all_operators_have_fixtures(self) -> None:
        covered = {case["operator"] for case in self.spec["cases"]}
        for entry in self.spec["operators"]:
            self.assertIn(entry["op"], covered, f"no fixture for operator {entry['op']}")

    def test_every_fixture_case(self) -> None:
        for case in self.spec["cases"]:
            with self.subTest(case=case["name"]):
                result = compare(
                    case.get("actual"),
                    case["operator"],
                    case.get("value"),
                    case.get("value2"),
                    case.get("fieldType"),
                )
                self.assertEqual(
                    result,
                    case["expected"],
                    f"{case['name']}: {case['operator']} expected {case['expected']}",
                )


class CompareOperatorTests(unittest.TestCase):
    def test_numeric_ordering(self) -> None:
        self.assertTrue(compare(750, ">=", 700))
        self.assertFalse(compare(650, ">=", 700))
        self.assertTrue(compare(650, "<", 700))
        self.assertTrue(compare("720", ">", "700"))  # string-coerced numbers
        self.assertFalse(compare("abc", ">=", 700))  # non-numeric -> False

    def test_equality(self) -> None:
        self.assertTrue(compare(1, "==", 1))
        self.assertTrue(compare("KYC", "==", "KYC"))
        self.assertTrue(compare("KYC", "!=", "AML"))

    def test_boolean_is_distinct_type_untyped_equality(self) -> None:
        # Booleans do NOT numerically coerce and stringify lowercase — matching the Rust/TS/Kotlin/
        # Dart cores. Python's native True==1 and str(True)=="True" would otherwise diverge.
        self.assertFalse(compare(True, "==", 1))          # bool is not the number 1
        self.assertFalse(compare(1, "==", True))
        self.assertFalse(compare("1", "==", True))
        self.assertTrue(compare("true", "==", True))      # lowercase string form matches
        self.assertTrue(compare(True, "==", True))
        self.assertFalse(compare(True, "==", False))
        self.assertTrue(compare(True, "!=", 1))
        self.assertFalse(compare(True, ">=", 0, field_type="number"))  # bool not numeric in ordering

    def test_null_only_equals_null(self) -> None:
        # A missing/null value never loosely-equals an empty string (or any value) — only null.
        self.assertFalse(compare(None, "==", ""))
        self.assertTrue(compare(None, "!=", ""))
        self.assertFalse(compare(None, "==", "null"))

    def test_between_inclusive(self) -> None:
        self.assertTrue(compare(50000, "between", 40000, 60000))
        self.assertTrue(compare(40000, "between", 40000, 60000))  # inclusive low
        self.assertTrue(compare(60000, "between", 40000, 60000))  # inclusive high
        self.assertFalse(compare(60001, "between", 40000, 60000))
        self.assertFalse(compare(50000, "between", 40000, None))  # missing upper bound

    def test_in_and_not_in_with_list(self) -> None:
        self.assertTrue(compare("KA", "in", ["KA", "MH", "DL"]))
        self.assertFalse(compare("TN", "in", ["KA", "MH", "DL"]))
        self.assertTrue(compare("TN", "not_in", ["KA", "MH", "DL"]))

    def test_in_with_comma_string_and_numeric_coercion(self) -> None:
        self.assertTrue(compare("MH", "in", "KA, MH, DL"))
        self.assertTrue(compare(2, "in", "1, 2, 3"))  # numeric loose match
        self.assertFalse(compare(5, "in", "1, 2, 3"))

    def test_regex(self) -> None:
        self.assertTrue(compare("ABCDE1234F", "regex", r"^[A-Z]{5}[0-9]{4}[A-Z]$"))
        self.assertFalse(compare("bad-pan", "regex", r"^[A-Z]{5}[0-9]{4}[A-Z]$"))
        self.assertFalse(compare("x", "regex", r"([unclosed"))  # invalid pattern -> False

    def test_regex_redos_guard_fails_closed_fast(self) -> None:
        # Catastrophic backtracking `^(a+)+$` against a crafted input used to pin the worker for
        # minutes (CPython `re` holds the GIL). The guard must return quickly and fail closed.
        import time
        t0 = time.time()
        result = compare("a" * 40 + "!", "regex", r"^(a+)+$")
        elapsed = time.time() - t0
        self.assertFalse(result)                 # no match, fails closed
        self.assertLess(elapsed, 1.0, "regex evaluation must be bounded (ReDoS guard)")

    def test_non_finite_numeric_operands_fail(self) -> None:
        # Infinity/NaN must NOT clear numeric gates (else a hostile payload passes any threshold).
        self.assertFalse(compare("Infinity", ">=", 9999999))
        self.assertFalse(compare("Infinity", ">", 0))
        self.assertFalse(compare("NaN", ">", 0))
        self.assertFalse(compare("Infinity", "==", 9999999, field_type="number"))

    def test_date_timezone_offset_normalized(self) -> None:
        # +05:30 local and the equivalent Z instant compare equal; ordering respects the offset.
        self.assertTrue(compare("2026-08-08T03:22:19+05:30", "==", "2026-08-07T21:52:19Z", field_type="date"))
        self.assertTrue(compare("2026-01-01T00:00:00-05:00", "==", "2026-01-01T05:00:00Z", field_type="date"))
        self.assertTrue(compare("2026-08-08T03:22:19+05:30", ">", "2026-08-07T21:00:00Z", field_type="date"))

    def test_exists(self) -> None:
        self.assertTrue(compare("something", "exists", None))
        self.assertFalse(compare(None, "exists", None))
        self.assertFalse(compare("", "exists", None))
        self.assertTrue(compare(None, "!exists", None))
        self.assertTrue(compare("", "!exists", None))

    def test_boolean_field_type(self) -> None:
        self.assertTrue(compare("true", "==", True, field_type="boolean"))
        self.assertTrue(compare(1, "==", True, field_type="boolean"))
        self.assertTrue(compare(0, "!=", True, field_type="boolean"))

    def test_unknown_operator_is_false(self) -> None:
        self.assertFalse(compare(1, "NOPE", 1))


class TreeEvaluationOperatorTests(unittest.TestCase):
    def _tree(self, condition: dict) -> dict:
        return {
            "type": "group",
            "logic": "AND",
            "children": [condition],
            "onPass": "approve",
            "onFail": "reject",
        }

    def test_in_operator_flows_through_tree(self) -> None:
        tree = self._tree(
            {"type": "condition", "variable": "state", "operator": "in", "value": ["KA", "MH"]}
        )
        approved = evaluate_rule_tree(tree, {"state": "KA"})
        rejected = evaluate_rule_tree(tree, {"state": "TN"})
        self.assertTrue(approved["passed"])
        self.assertEqual(approved["outcome"], "approve")
        self.assertFalse(rejected["passed"])
        self.assertEqual(rejected["outcome"], "reject")

    def test_between_flows_through_tree(self) -> None:
        tree = self._tree(
            {
                "type": "condition",
                "variable": "income",
                "operator": "between",
                "value": 40000,
                "value2": 60000,
            }
        )
        self.assertTrue(evaluate_rule_tree(tree, {"income": 50000})["passed"])
        self.assertFalse(evaluate_rule_tree(tree, {"income": 90000})["passed"])

    def test_regex_flows_through_tree(self) -> None:
        tree = self._tree(
            {
                "type": "condition",
                "variable": "pan",
                "operator": "regex",
                "value": r"^[A-Z]{5}[0-9]{4}[A-Z]$",
            }
        )
        self.assertTrue(evaluate_rule_tree(tree, {"pan": "ABCDE1234F"})["passed"])
        self.assertFalse(evaluate_rule_tree(tree, {"pan": "nope"})["passed"])


if __name__ == "__main__":
    unittest.main()
