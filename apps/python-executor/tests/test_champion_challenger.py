"""Tests for champion/challenger analysis (pure, no DB)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.champion_challenger import (  # noqa: E402
    analyze_champion_challenger,
    current_ramp_weight,
    evaluate_guardrails,
    identify_roles,
    recommend_action,
)


class RoleTests(unittest.TestCase):
    def test_explicit_role(self):
        champ, challengers = identify_roles(
            [{"id": "b", "role": "challenger"}, {"id": "a", "role": "champion"}]
        )
        self.assertEqual(champ["id"], "a")
        self.assertEqual([c["id"] for c in challengers], ["b"])

    def test_fallback_to_first(self):
        champ, challengers = identify_roles([{"id": "x"}, {"id": "y"}])
        self.assertEqual(champ["id"], "x")
        self.assertEqual([c["id"] for c in challengers], ["y"])


class RampTests(unittest.TestCase):
    def test_ramp_progression(self):
        variant = {"id": "c", "ramp": [{"day": 0, "weight": 10}, {"day": 3, "weight": 25}, {"day": 7, "weight": 50}]}
        self.assertEqual(current_ramp_weight(variant, 0), 10)
        self.assertEqual(current_ramp_weight(variant, 2), 10)
        self.assertEqual(current_ramp_weight(variant, 3), 25)
        self.assertEqual(current_ramp_weight(variant, 10), 50)

    def test_no_ramp(self):
        self.assertIsNone(current_ramp_weight({"id": "c", "weight": 10}, 5))


class GuardrailTests(unittest.TestCase):
    def test_latency_breach(self):
        result = evaluate_guardrails(
            {"approvalRate": 65, "rejectRate": 30, "avgLatencyMs": 200},
            {"approvalRate": 66},
            {"maxAvgLatencyMs": 150},
        )
        self.assertTrue(result["breached"])

    def test_approval_drop_breach(self):
        result = evaluate_guardrails(
            {"approvalRate": 55, "rejectRate": 40, "avgLatencyMs": 40},
            {"approvalRate": 66},
            {"maxApprovalRateDropPct": 5},
        )
        self.assertTrue(result["breached"])

    def test_within_guardrails(self):
        result = evaluate_guardrails(
            {"approvalRate": 68, "rejectRate": 30, "avgLatencyMs": 40},
            {"approvalRate": 66},
            {"minApprovalRate": 60, "maxAvgLatencyMs": 150, "maxApprovalRateDropPct": 5},
        )
        self.assertFalse(result["breached"])


class RecommendationTests(unittest.TestCase):
    def test_rollback_on_breach(self):
        self.assertEqual(recommend_action(True, 5, {"breached": True, "breaches": ["x"]}, 100, 1000), "rollback")

    def test_hold_on_low_sample(self):
        self.assertEqual(recommend_action(True, 5, {"breached": False}, 100, 50), "hold")

    def test_promote_when_significant_positive(self):
        self.assertEqual(recommend_action(True, 2.4, {"breached": False}, 100, 5000), "promote")

    def test_rollback_on_negative_significant(self):
        self.assertEqual(recommend_action(True, -3.0, {"breached": False}, 100, 5000), "rollback")


class AnalysisTests(unittest.TestCase):
    def test_end_to_end_promote(self):
        variants = [
            {"id": "champion", "role": "champion"},
            {"id": "challenger", "role": "challenger", "guardrails": {"maxAvgLatencyMs": 150, "maxApprovalRateDropPct": 5}},
        ]
        stats = {
            "champion": {"users": 9000, "approved": 5967, "approvalRate": 66.3, "rejectRate": 30.0, "avgLatencyMs": 38},
            "challenger": {"users": 9000, "approved": 6156, "approvalRate": 68.4, "rejectRate": 28.0, "avgLatencyMs": 42},
        }
        analysis = analyze_champion_challenger(variants, stats)
        self.assertEqual(analysis["champion"]["id"], "champion")
        challenger = analysis["challengers"][0]
        self.assertGreater(challenger["liftPct"], 0)
        self.assertTrue(challenger["significant"])
        self.assertFalse(challenger["guardrails"]["breached"])
        self.assertEqual(challenger["recommendation"], "promote")


if __name__ == "__main__":
    unittest.main()
