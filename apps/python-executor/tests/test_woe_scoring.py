"""
Detailed tests for WoE / weighted-bin scoring in evaluate_scorecard.

Tests the logic in app.logic.evaluate_scorecard directly without HTTP
overhead, covering points, WoE, formula, and metric scoring modes.
"""
from __future__ import annotations

import math
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory

from app.logic import evaluate_scorecard


def _var_lookup(*variable_ids: str) -> Dict[str, Dict[str, Any]]:
    """Build a minimal variable lookup dict."""
    return {vid: {"name": vid, "source_id": "test"} for vid in variable_ids}


class WoEScoringTests(unittest.TestCase):
    """~15 tests covering points, WoE, formula, and metric scoring."""

    # ------------------------------------------------------------------
    # Points-based scoring
    # ------------------------------------------------------------------

    def test_points_weight_1_same_as_unweighted(self) -> None:
        """weight=1.0 should not alter points."""
        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "age",
                    "weight": 1.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 18, "max": 65, "points": 50}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {"age": 30}, _var_lookup("age"))
        self.assertEqual(result["score"], 350)
        self.assertEqual(result["breakdown"][0]["weighted_points"], 50)

    def test_points_weight_2_doubles_points(self) -> None:
        """weight=2.0 should double the points contribution."""
        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "age",
                    "weight": 2.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 18, "max": 65, "points": 50}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {"age": 30}, _var_lookup("age"))
        self.assertEqual(result["score"], 400)
        self.assertEqual(result["breakdown"][0]["weighted_points"], 100)

    # ------------------------------------------------------------------
    # WoE-based scoring
    # ------------------------------------------------------------------

    def test_woe_scoring_with_coefficients(self) -> None:
        """WoE scoring: offset + factor * (intercept + sum(coeff * woe))."""
        intercept = -1.5
        pdo = 20.0
        target_score = 600.0
        target_odds = 50.0
        woe_value = 0.8
        coefficient = 0.5

        factor = pdo / math.log(2)
        offset = target_score - factor * math.log(target_odds)
        expected_raw = offset + factor * (intercept + coefficient * woe_value)
        expected_score = max(0, min(900, round(expected_raw)))

        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "woe",
            "intercept": intercept,
            "pdo": pdo,
            "target_score": target_score,
            "target_odds": target_odds,
            "bins": [
                {
                    "variable_id": "income",
                    "weight": 1.0,
                    "coefficient": coefficient,
                    "ranges": [{"min": 0, "max": 100000, "points": 0}],
                    "woe_values": [{"min": 0, "max": 100000, "woe": woe_value, "iv": 0.03}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {"income": 50000}, _var_lookup("income"))
        self.assertEqual(result["score"], expected_score)
        self.assertEqual(result["scoring_method"], "woe")

    def test_woe_scoring_mathematically_correct(self) -> None:
        """Verify the full WoE formula with two bins."""
        intercept = -2.0
        pdo = 25.0
        target_score = 650.0
        target_odds = 30.0

        factor = pdo / math.log(2)
        offset = target_score - factor * math.log(target_odds)

        coeff_a, woe_a = 0.3, 1.2
        coeff_b, woe_b = 0.7, -0.5

        expected_raw = offset + factor * (intercept + coeff_a * woe_a + coeff_b * woe_b)
        expected_score = max(0, min(850, round(expected_raw)))

        scorecard = {
            "base_score": 300,
            "max_score": 850,
            "scoring_method": "woe",
            "intercept": intercept,
            "pdo": pdo,
            "target_score": target_score,
            "target_odds": target_odds,
            "bins": [
                {
                    "variable_id": "var_a",
                    "weight": 1.0,
                    "coefficient": coeff_a,
                    "ranges": [{"min": 0, "max": 100, "points": 0}],
                    "woe_values": [{"min": 0, "max": 100, "woe": woe_a, "iv": 0.04}],
                },
                {
                    "variable_id": "var_b",
                    "weight": 1.0,
                    "coefficient": coeff_b,
                    "ranges": [{"min": 0, "max": 100, "points": 0}],
                    "woe_values": [{"min": 0, "max": 100, "woe": woe_b, "iv": 0.02}],
                },
            ],
        }
        result = evaluate_scorecard(
            scorecard,
            {"var_a": 50, "var_b": 50},
            _var_lookup("var_a", "var_b"),
        )
        self.assertEqual(result["score"], expected_score)

    def test_woe_details_populated(self) -> None:
        """WoE scoring should populate woe_details in the result."""
        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "woe",
            "intercept": 0,
            "pdo": 20,
            "target_score": 600,
            "target_odds": 50,
            "bins": [
                {
                    "variable_id": "x",
                    "weight": 1.0,
                    "coefficient": 1.0,
                    "ranges": [{"min": 0, "max": 100, "points": 0}],
                    "woe_values": [{"min": 0, "max": 100, "woe": 0.5, "iv": 0.1, "event_rate": 0.2, "non_event_rate": 0.8}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {"x": 50}, _var_lookup("x"))
        self.assertIn("woe_details", result)
        self.assertEqual(len(result["woe_details"]), 1)
        self.assertAlmostEqual(result["woe_details"][0]["woe"], 0.5)
        self.assertAlmostEqual(result["woe_details"][0]["contribution"], 0.5)

    # ------------------------------------------------------------------
    # Formula-based scoring
    # ------------------------------------------------------------------

    def test_formula_scoring_custom_expression(self) -> None:
        """Formula scoring with a custom expression."""
        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "formula",
            "formula": "base_score + total_points * 2",
            "bins": [
                {
                    "variable_id": "age",
                    "weight": 1.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 18, "max": 65, "points": 50}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {"age": 30}, _var_lookup("age"))
        # total_points = 50*1 = 50; formula = 300 + 50*2 = 400
        self.assertEqual(result["score"], 400)
        self.assertEqual(result["scoring_method"], "formula")

    # ------------------------------------------------------------------
    # Metric formulas
    # ------------------------------------------------------------------

    def test_metric_formula_if_score_high(self) -> None:
        """IF(score >= 700, 0.15, 0.05) with score=750 should yield 0.15."""
        scorecard = {
            "base_score": 700,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "age",
                    "weight": 1.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 18, "max": 65, "points": 50}],
                },
            ],
            "metrics": [
                {"name": "risk_rate", "formula": "IF(score >= 700, 0.15, 0.05)", "unit": "%", "category": "financial"},
            ],
        }
        result = evaluate_scorecard(scorecard, {"age": 30}, _var_lookup("age"))
        # score = 700 + 50 = 750
        self.assertEqual(result["score"], 750)
        self.assertIn("metrics", result)
        self.assertAlmostEqual(result["metrics"]["risk_rate"]["value"], 0.15)

    def test_metric_formula_if_score_low(self) -> None:
        """IF(score >= 700, 0.15, 0.05) with score=350 should yield 0.05."""
        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "age",
                    "weight": 1.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 18, "max": 65, "points": 50}],
                },
            ],
            "metrics": [
                {"name": "risk_rate", "formula": "IF(score >= 700, 0.15, 0.05)", "unit": "%", "category": "financial"},
            ],
        }
        result = evaluate_scorecard(scorecard, {"age": 30}, _var_lookup("age"))
        # score = 300 + 50 = 350
        self.assertEqual(result["score"], 350)
        self.assertAlmostEqual(result["metrics"]["risk_rate"]["value"], 0.05)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_missing_variable_defaults_to_zero(self) -> None:
        """If a variable is missing from the payload, it defaults to 0."""
        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "missing_var",
                    "weight": 1.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 0, "max": 100, "points": 80}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {}, _var_lookup("missing_var"))
        # value=0, 0 is within [0,100] range -> 80 points; 300 + 80 = 380
        self.assertEqual(result["score"], 380)

    def test_empty_bins_returns_base_score(self) -> None:
        """A scorecard with no bins should return base_score."""
        scorecard = {
            "base_score": 500,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [],
        }
        result = evaluate_scorecard(scorecard, {"x": 10})
        self.assertEqual(result["score"], 500)
        self.assertEqual(len(result["breakdown"]), 0)

    def test_information_value_calculated(self) -> None:
        """Total information_value should be the sum of IV from WoE bins."""
        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "woe",
            "intercept": 0,
            "pdo": 20,
            "target_score": 600,
            "target_odds": 50,
            "bins": [
                {
                    "variable_id": "a",
                    "weight": 1.0,
                    "coefficient": 0.5,
                    "ranges": [{"min": 0, "max": 100, "points": 0}],
                    "woe_values": [{"min": 0, "max": 100, "woe": 0.3, "iv": 0.07}],
                },
                {
                    "variable_id": "b",
                    "weight": 1.0,
                    "coefficient": 0.5,
                    "ranges": [{"min": 0, "max": 100, "points": 0}],
                    "woe_values": [{"min": 0, "max": 100, "woe": -0.2, "iv": 0.03}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {"a": 50, "b": 50}, _var_lookup("a", "b"))
        self.assertIn("information_value", result)
        self.assertAlmostEqual(result["information_value"], 0.10)

    def test_mixed_bins_woe_and_non_woe(self) -> None:
        """Bins where only some have WoE values should still work."""
        scorecard = {
            "base_score": 300,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "var_with_woe",
                    "weight": 1.0,
                    "coefficient": 0.5,
                    "ranges": [{"min": 0, "max": 100, "points": 40}],
                    "woe_values": [{"min": 0, "max": 100, "woe": 0.6, "iv": 0.05}],
                },
                {
                    "variable_id": "var_without_woe",
                    "weight": 1.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 0, "max": 100, "points": 60}],
                },
            ],
        }
        result = evaluate_scorecard(
            scorecard,
            {"var_with_woe": 50, "var_without_woe": 50},
            _var_lookup("var_with_woe", "var_without_woe"),
        )
        # Points: 40 + 60 = 100; score = 300 + 100 = 400
        self.assertEqual(result["score"], 400)
        self.assertEqual(len(result["breakdown"]), 2)
        # WoE details should only contain the one bin that has WoE
        self.assertIn("woe_details", result)
        self.assertEqual(len(result["woe_details"]), 1)

    def test_score_clamped_to_max(self) -> None:
        """Score should not exceed max_score."""
        scorecard = {
            "base_score": 800,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "x",
                    "weight": 1.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 0, "max": 100, "points": 200}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {"x": 50}, _var_lookup("x"))
        # 800 + 200 = 1000, but clamped to 900
        self.assertEqual(result["score"], 900)

    def test_score_clamped_to_zero(self) -> None:
        """Score should not go below 0."""
        scorecard = {
            "base_score": 50,
            "max_score": 900,
            "scoring_method": "points",
            "bins": [
                {
                    "variable_id": "x",
                    "weight": 1.0,
                    "coefficient": 0.0,
                    "ranges": [{"min": 0, "max": 100, "points": -200}],
                },
            ],
        }
        result = evaluate_scorecard(scorecard, {"x": 50}, _var_lookup("x"))
        # 50 + (-200) = -150 -> clamped to 0
        self.assertEqual(result["score"], 0)


if __name__ == "__main__":
    unittest.main()
