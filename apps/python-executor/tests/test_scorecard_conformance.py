"""Cross-engine SCORECARD conformance — the Python arm (source of truth).

packages/shared/scorecards.spec.json records, for each of 166 scorecards, the score and result
shape that app/logic.py's evaluate_scorecard actually produces — points, weighted points, WoE
(coefficient/woe_values/intercept/pdo/target_score/target_odds), custom formulas evaluated through
the allowlisting AST evaluator in app/safe_formula.py, derived metrics, and the boundary cases
(missing variable, explicit null, non-numeric value, value below/above every bin, max_score and
base_score clamping, empty factor list).

This arm asserts the fixture still matches the live Python engine — it catches drift in
app/logic.py. The on-device arms (Kotlin ScorecardEvaluator, Dart scorecard_evaluator) implement
POINTS ONLY today and will fail this corpus until they implement the rest; that is the point.

Regenerate the fixture with:  python -m simulation.gen_scorecards_spec
Self-check it in place with:  python -m simulation.gen_scorecards_spec --verify
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.logic import evaluate_scorecard  # noqa: E402

SPEC = json.loads((APP_ROOT.parents[1] / "packages" / "shared" / "scorecards.spec.json").read_text())
TOLERANCE = SPEC["meta"]["floatTolerance"]


def _close(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=TOLERANCE, abs_tol=TOLERANCE)
    return a == b


class ScorecardConformanceTests(unittest.TestCase):
    def test_every_case_reproduces_score_and_shape(self):
        for case in SPEC["cases"]:
            with self.subTest(case=case["name"]):
                result = evaluate_scorecard(case["scorecard"], case["variables"], case.get("variableLookup"))
                expected = case["expected"]
                # score is always an int, so this is an exact assertion — the one every engine owes.
                self.assertEqual(result["score"], expected["score"], "score")
                self.assertEqual(result["scoring_method"], expected["scoringMethod"])
                self.assertEqual(result["base_score"], expected["baseScore"])
                self.assertEqual(result["max_score"], expected["maxScore"])
                self.assertEqual(sorted(result.keys()), expected["resultKeys"], "result keys")
                self.assertEqual(len(result["breakdown"]), expected["breakdownLength"])

    def test_breakdown_matches_per_factor(self):
        """points/weighted_points/weight/woe/coefficient and the matched range, factor by factor."""
        for case in SPEC["cases"]:
            with self.subTest(case=case["name"]):
                result = evaluate_scorecard(case["scorecard"], case["variables"], case.get("variableLookup"))
                for entry, want in zip(result["breakdown"], case["expected"]["breakdown"], strict=True):
                    self.assertEqual(entry["variable_id"], want["variableId"])
                    self.assertEqual(entry["variable_name"], want["variableName"])
                    self.assertEqual(entry["source_id"], want["sourceId"])
                    self.assertEqual(entry["active_range"], want["activeRange"], "matched range")
                    self.assertEqual(entry["points"], want["points"])
                    # int(points * weight), truncated PER FACTOR — never summed then rounded.
                    self.assertEqual(entry["weighted_points"], want["weightedPoints"])
                    self.assertTrue(_close(entry["weight"], want["weight"]))
                    self.assertTrue(_close(entry["woe"], want["woe"]))
                    self.assertTrue(_close(entry["coefficient"], want["coefficient"]))

    def test_woe_details_and_information_value(self):
        for case in SPEC["cases"]:
            expected = case["expected"]
            with self.subTest(case=case["name"]):
                result = evaluate_scorecard(case["scorecard"], case["variables"], case.get("variableLookup"))
                self.assertEqual("woe_details" in result, expected["hasWoeDetails"])
                if not expected["hasWoeDetails"]:
                    self.assertNotIn("information_value", result)
                    continue
                self.assertTrue(_close(result["information_value"], expected["informationValue"]))
                self.assertEqual(len(result["woe_details"]), len(expected["woeDetails"]))
                for detail, want in zip(result["woe_details"], expected["woeDetails"], strict=True):
                    self.assertEqual(detail["variable_id"], want["variable_id"])
                    self.assertTrue(_close(detail["woe"], want["woe"]))
                    self.assertTrue(_close(detail["iv"], want["iv"]))
                    self.assertTrue(_close(detail["coefficient"], want["coefficient"]))
                    self.assertTrue(_close(detail["contribution"], want["contribution"]))
                    self.assertEqual(detail["event_rate"], want["event_rate"])
                    self.assertEqual(detail["non_event_rate"], want["non_event_rate"])

    def test_metrics(self):
        for case in SPEC["cases"]:
            expected = case["expected"]
            with self.subTest(case=case["name"]):
                result = evaluate_scorecard(case["scorecard"], case["variables"], case.get("variableLookup"))
                self.assertEqual("metrics" in result, expected["hasMetrics"])
                if not expected["hasMetrics"]:
                    continue
                self.assertEqual(sorted(result["metrics"]), sorted(expected["metrics"]))
                for name, want in expected["metrics"].items():
                    got = result["metrics"][name]
                    self.assertEqual(got["unit"], want["unit"])
                    self.assertEqual(got["category"], want["category"])
                    if isinstance(want["value"], dict):
                        # A failed metric formula. The corpus records {"error": true} because the
                        # real message embeds CPython's SyntaxError text, which is not a contract.
                        self.assertIsInstance(got["value"], dict)
                        self.assertIn("error", got["value"])
                    else:
                        self.assertTrue(_close(got["value"], want["value"]), name)

    def test_fixture_covers_every_scoring_method_and_boundary(self):
        by_method = SPEC["meta"]["casesByScoringMethod"]
        for method in ("points", "woe", "formula"):
            self.assertGreaterEqual(by_method.get(method, 0), 10, f"{method} coverage")
        names = {case["name"] for case in SPEC["cases"]}
        for required in (
            "points-missing-variable-defaults-to-zero",
            "points-null-value-matches-nothing",
            "points-below-every-bin",
            "points-above-every-bin",
            "points-clamped-to-max-score",
            "points-clamped-to-zero",
            "points-empty-bins",
            "weighted-half-truncates",
            "weighted-per-factor-truncation-not-sum-then-truncate",
            "woe-defaults-for-every-parameter",
            "woe-pdo-zero-uses-factor-one",
            "woe-target-odds-zero-drops-log-term",
            "woe-no-range-matches-omits-woe-keys",
            "formula-omitted-uses-default",
            "formula-unknown-name-falls-back-to-base-score",
            "formula-subscript-rejected",
            "metrics-error-value",
        ):
            self.assertIn(required, names, f"fixture lost boundary case {required}")

    def test_woe_scorecards_actually_diverge_from_points(self):
        """The bug this corpus exists to catch: a `woe` scorecard must NOT score like a points one.

        If these ever coincide the corpus has stopped discriminating and an on-device engine that
        implements points only would pass it by accident.
        """
        diverged = 0
        for case in SPEC["cases"]:
            card = case["scorecard"]
            if card.get("scoring_method") != "woe":
                continue
            points_score = max(0, min(int(card.get("max_score", 900)),
                                      int(card.get("base_score", 300)) + case["expected"]["totalWeightedPoints"]))
            if points_score != case["expected"]["score"]:
                diverged += 1
        self.assertGreaterEqual(diverged, 25, "woe cases no longer distinguishable from points scoring")


if __name__ == "__main__":
    unittest.main()
