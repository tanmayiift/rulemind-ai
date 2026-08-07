"""Deterministic adverse-action reason codes derived from failing decision conditions.

For FCRA-style decline notices, the engine must turn a decision's failing rule conditions into a
stable, ranked set of reason codes — deterministically (no LLM), most-material reason first.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app.logic import adverse_reason_codes  # noqa: E402


def _cond(vid, op, threshold, value, passed):
    return {"variable_id": vid, "variable_name": vid.replace("_", " ").title(),
            "operator": op, "threshold": threshold, "value": value, "passed": passed}


class ReasonCodeTests(unittest.TestCase):
    RESULTS = [
        {"conditions": [
            _cond("bureau_score", ">=", 700, 540, False),   # missed by 160/700 ≈ 0.23
            _cond("dti_ratio", "<=", 40, 42, False),        # missed by 2/40 = 0.05
            _cond("kyc_status", "==", "verified", "verified", True),  # passed -> ignored
        ]},
        {"conditions": [
            _cond("age", ">=", 21, 19, False),              # missed by 2/21 ≈ 0.095
        ]},
    ]

    def test_only_failing_conditions_become_codes(self):
        codes = adverse_reason_codes(self.RESULTS, limit=10)
        names = {c["variable_id"] for c in codes}
        self.assertEqual(names, {"bureau_score", "dti_ratio", "age"})
        self.assertNotIn("kyc_status", names)  # a passing condition is never a reason

    def test_ranked_by_materiality_and_limited(self):
        codes = adverse_reason_codes(self.RESULTS, limit=2)
        self.assertEqual(len(codes), 2)
        # bureau_score missed most (relative), age next, dti least -> top 2 are bureau_score, age.
        self.assertEqual(codes[0]["variable_id"], "bureau_score")
        self.assertEqual(codes[1]["variable_id"], "age")

    def test_codes_are_stable_and_deterministic(self):
        a = adverse_reason_codes(self.RESULTS, limit=3)
        b = adverse_reason_codes(self.RESULTS, limit=3)
        self.assertEqual([c["code"] for c in a], [c["code"] for c in b])
        self.assertEqual(a[0]["code"], "RC-BUREAU-SCORE-LT")

    def test_reason_is_human_readable(self):
        codes = adverse_reason_codes(self.RESULTS, limit=1)
        self.assertIn("required 700", codes[0]["reason"])
        self.assertIn("actual 540", codes[0]["reason"])

    def test_empty_when_nothing_failed(self):
        passed_only = [{"conditions": [_cond("bureau_score", ">=", 700, 800, True)]}]
        self.assertEqual(adverse_reason_codes(passed_only), [])


if __name__ == "__main__":
    unittest.main()
