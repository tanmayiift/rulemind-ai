"""Full outcome-spectrum coverage for the decision analytics.

The rejection-driver + decision analytics must partition the WHOLE outcome
spectrum correctly — approve, reject AND review — not just declines. In
particular: `focus_count` counts decline+review (the "bad" outcomes) but NOT
approve; a driver's `fail_count` is attributed only to focused decisions; and
`outcome_mix` reflects every outcome seen. A condition that only ever fails on
APPROVED decisions must contribute zero to the drivers.

These are pure-compute unit tests (no LLM, no DB) over synthetic traces, so every
branch of the spectrum is exercised deterministically.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.analytics import rejection_drivers  # noqa: E402


def _cond(variable_id, passed, operator=">=", threshold=700, value=0):
    return {
        "variable_id": variable_id,
        "variable_name": variable_id,
        "operator": operator,
        "threshold": threshold,
        "passed": passed,
        "value": value,
    }


def _decision(outcome, conditions, policy_id="p1"):
    return {
        "outcome": outcome,
        "policy_id": policy_id,
        "trace": [{"step": {"type": "rule"}, "result": {"conditions": conditions}}],
    }


class OutcomeSpectrumTests(unittest.TestCase):
    def _spectrum(self):
        # 3 approves (everything passed), 2 rejects (bureau failed), 1 review (dti failed).
        approves = [_decision("approve", [_cond("bureau_score", True), _cond("dti_ratio", True, operator="<=", threshold=0.4)])
                    for _ in range(3)]
        rejects = [_decision("reject", [_cond("bureau_score", False, value=520), _cond("dti_ratio", True, operator="<=", threshold=0.4)])
                   for _ in range(2)]
        review = [_decision("review", [_cond("bureau_score", True), _cond("dti_ratio", False, operator="<=", threshold=0.4, value=0.9)])]
        return approves + rejects + review

    def test_focus_count_excludes_approvals(self) -> None:
        result = rejection_drivers(self._spectrum())
        self.assertEqual(result["analyzed"], 6)
        # reject + review are the focus outcomes; the 3 approvals are NOT counted.
        self.assertEqual(set(result["focus_outcomes"]), {"reject", "review"})
        self.assertEqual(result["focus_count"], 3)
        self.assertEqual(result["outcome_mix"], {"approve": 3, "reject": 2, "review": 1})

    def test_drivers_attribute_failures_only_to_focused_outcomes(self) -> None:
        drivers = {d["variable_id"]: d for d in rejection_drivers(self._spectrum())["drivers"]}
        # bureau_score failed only on the 2 rejects (approvals passed it) -> fail_count 2.
        self.assertEqual(drivers["bureau_score"]["fail_count"], 2)
        self.assertEqual(drivers["bureau_score"]["seen"], 6)
        self.assertAlmostEqual(drivers["bureau_score"]["share_of_focus"], round(2 / 3, 4))
        # dti_ratio failed only on the 1 review -> fail_count 1.
        self.assertEqual(drivers["dti_ratio"]["fail_count"], 1)
        self.assertAlmostEqual(drivers["dti_ratio"]["share_of_focus"], round(1 / 3, 4))

    def test_failure_on_an_approved_decision_never_counts(self) -> None:
        # A condition that "fails" but only on APPROVED decisions must contribute 0 —
        # approvals are out of focus, so a fail there is not a rejection driver.
        decisions = [
            _decision("approve", [_cond("odd_flag", False)]),
            _decision("approve", [_cond("odd_flag", False)]),
        ]
        result = rejection_drivers(decisions)
        self.assertEqual(result["focus_count"], 0)
        self.assertEqual(result["drivers"][0]["fail_count"], 0)
        self.assertEqual(result["outcome_mix"], {"approve": 2})

    def test_review_only_is_still_analyzed(self) -> None:
        # "review" is a focus outcome on its own, even with no hard rejects.
        result = rejection_drivers([_decision("review", [_cond("dti_ratio", False, operator="<=", threshold=0.4, value=0.9)])])
        self.assertEqual(result["focus_count"], 1)
        self.assertEqual(result["drivers"][0]["variable_id"], "dti_ratio")
        self.assertEqual(result["drivers"][0]["fail_count"], 1)

    def test_drivers_ranked_by_fail_count(self) -> None:
        # bureau fails on 3 declines, dti on 1 -> bureau ranks first.
        decisions = (
            [_decision("reject", [_cond("bureau_score", False), _cond("dti_ratio", True, operator="<=", threshold=0.4)]) for _ in range(3)]
            + [_decision("review", [_cond("bureau_score", True), _cond("dti_ratio", False, operator="<=", threshold=0.4, value=0.9)])]
        )
        drivers = rejection_drivers(decisions)["drivers"]
        self.assertEqual(drivers[0]["variable_id"], "bureau_score")
        self.assertEqual(drivers[0]["fail_count"], 3)


if __name__ == "__main__":
    unittest.main()
