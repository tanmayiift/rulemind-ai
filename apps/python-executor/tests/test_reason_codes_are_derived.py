"""Adverse-action reason codes must come from the conditions that actually failed.

`apps/web/app/decision-explorer/page.tsx` used to contain:

    function reasonCodes(decision) {
      if (decision.outcome === "reject")
        return ["AA05 · Debt-to-income too high", "AA12 · Insufficient credit history"];
      if (decision.outcome === "review")
        return ["MR02 · Manual verification required"];
    }

-- the same two codes for every rejected decision in the product, regardless of which rules
fired, rendered under a heading reading "Adverse-action reason codes". These are the codes a
lender puts in an FCRA-style decline notice. Inventing them is not a placeholder; it is a
compliance fabrication, and a risk lead who opens two rejected decisions in an evaluation finds
it immediately.

The real implementation had existed the whole time and was called by nothing:
`GET /api/v1/decisions/{id}/reason-codes` -> `logic.adverse_reason_codes()`, which ranks failing
conditions by relative miss magnitude.

These tests pin the property that makes the fabrication impossible to reintroduce: **two
decisions that failed different conditions must produce different codes.** A test that only
asserted "some codes are returned" would have passed against the hardcoded version.
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


def _cond(variable_id, name, operator, threshold, value, passed):
    return {"variable_id": variable_id, "variable_name": name, "operator": operator,
            "threshold": threshold, "value": value, "passed": passed}


class ReasonCodesAreDerivedTest(unittest.TestCase):
    def test_two_decisions_failing_different_conditions_get_different_codes(self):
        """The property the hardcoded version violated, and the only one that matters."""
        loan = adverse_reason_codes([_cond("bureau_score", "Bureau Score", ">=", 700, 540, False)])
        travel = adverse_reason_codes([_cond("visa_ready", "Visa Ready", ">=", 1, 0, False)])
        self.assertTrue(loan and travel)
        self.assertNotEqual({c["code"] for c in loan}, {c["code"] for c in travel})

    def test_a_passing_decision_has_no_adverse_codes(self):
        """An approval carries no adverse action. The old version keyed off `outcome` and would
        happily have emitted codes for anything it was told to."""
        self.assertEqual(adverse_reason_codes([_cond("bureau_score", "Bureau Score", ">=", 700, 810, True)]), [])

    def test_codes_name_the_variable_that_failed(self):
        codes = adverse_reason_codes([_cond("dti_ratio", "Debt to Income", "<=", 0.4, 0.62, False)])
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0]["variable_id"], "dti_ratio")
        self.assertEqual(codes[0]["threshold"], 0.4)
        self.assertEqual(codes[0]["value"], 0.62)
        self.assertIn("Debt to Income", codes[0]["reason"])

    def test_ranking_is_by_materiality_not_by_order(self):
        """A notice should lead with the condition that missed by the most."""
        codes = adverse_reason_codes([
            _cond("a", "Narrow miss", ">=", 700, 695, False),     # 0.7% short
            _cond("b", "Wide miss", ">=", 700, 200, False),       # 71% short
        ], limit=2)
        self.assertEqual([c["variable_id"] for c in codes], ["b", "a"])

    def test_it_is_deterministic(self):
        conds = [_cond("x", "X", ">=", 10, 3, False), _cond("y", "Y", ">=", 5, 1, False)]
        self.assertEqual(adverse_reason_codes(conds), adverse_reason_codes(conds))

    def test_nested_rule_results_are_walked(self):
        """rule_results is a tree of groups; a failing condition three levels down still counts."""
        nested = {"groups": [{"conditions": [_cond("deep", "Deep", ">=", 1, 0, False)]}]}
        codes = adverse_reason_codes(nested)
        self.assertEqual([c["variable_id"] for c in codes], ["deep"])


class TheUiMustNotFabricateTest(unittest.TestCase):
    """A source-level guard, because this defect lived in the frontend and no Python test could
    have caught it. It is crude on purpose: the failure it prevents is someone re-adding a
    literal code table to make a screen look finished."""

    WEB = APP_ROOT.parent / "web" / "app" / "decision-explorer" / "page.tsx"

    def test_the_decision_explorer_calls_the_server(self):
        src = self.WEB.read_text(encoding="utf-8")
        self.assertIn("/reason-codes", src,
                      "the Decision Explorer must fetch reason codes from the API, not derive them")

    def test_no_hardcoded_reason_code_table(self):
        import re

        src = self.WEB.read_text(encoding="utf-8")
        # Strip comments: the file deliberately quotes the old fabricated codes to explain them.
        body = re.sub(r"//.*", "", src)
        offenders = re.findall(r'"(?:AA|MR)\d{2}\s*·', body)
        self.assertEqual(offenders, [],
                         "hardcoded adverse-action codes are back in the Decision Explorer")


if __name__ == "__main__":
    unittest.main()
