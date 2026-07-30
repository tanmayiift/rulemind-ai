"""Tests for the stateless, DB-free decision core (app/core).

Also asserts the core imports and runs with NO database configured — the proof
that it is hostable anywhere (K8s pod, serverless, edge, SDK host).
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import os as _os
_os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory

# Deliberately do NOT set DATABASE_URL / RULEMIND_CONFIG_KEY — the core must not
# need them. If importing app.core triggered a DB connection this would fail.
from app.core import decide, validate_input  # noqa: E402


def sample_bundle():
    return {
        "policy": {
            "id": "credit_v1",
            "steps": [
                {"type": "scorecard", "ref_id": "sc1"},
                {"type": "rule", "ref_id": "rule_income"},
                {"type": "rule", "ref_id": "rule_risk"},
            ],
            "input_schema": {
                "fields": {
                    "annual_income": {"required": True, "type": "number", "min": 0},
                    "risk_flag": {"type": "boolean"},
                }
            },
        },
        "rules": {
            "rule_income": {
                "id": "rule_income",
                "rule_format": "v2",
                "tree": {
                    "type": "group",
                    "logic": "AND",
                    "children": [
                        {"type": "condition", "variable": "annual_income", "operator": ">=", "value": 50000},
                    ],
                    "onPass": "approve",
                    "onFail": "reject",
                },
            },
            "rule_risk": {
                "id": "rule_risk",
                "rule_format": "v2",
                "tree": {
                    "type": "group",
                    "logic": "AND",
                    "children": [
                        {"type": "condition", "variable": "risk_flag", "operator": "==", "value": False, "fieldType": "boolean"},
                    ],
                    "onPass": "approve",
                    "onFail": "reject",
                },
            },
        },
        "scorecards": {
            "sc1": {
                "id": "sc1",
                "base_score": 300,
                "max_score": 900,
                "bins": [
                    {
                        "variable_id": "annual_income",
                        "weight": 1.0,
                        "ranges": [{"min": 0, "max": 1000000, "points": 40}],
                    }
                ],
            }
        },
    }


class CoreEngineTests(unittest.TestCase):
    def test_approve_path(self):
        result = decide(sample_bundle(), {"annual_income": 80000, "risk_flag": False})
        self.assertTrue(result["input_valid"])
        self.assertEqual(result["outcome"], "approve")
        self.assertEqual(len(result["rule_results"]), 2)
        self.assertIsNotNone(result["scorecard_result"])

    def test_reject_path_precedence(self):
        # income passes but risk_flag is True -> rule_risk rejects; reject wins.
        result = decide(sample_bundle(), {"annual_income": 80000, "risk_flag": True})
        self.assertEqual(result["outcome"], "reject")

    def test_input_validation_blocks(self):
        result = decide(sample_bundle(), {"risk_flag": False})  # missing required income
        self.assertFalse(result["input_valid"])
        self.assertEqual(result["outcome"], "reject")
        self.assertTrue(any("annual_income" in e for e in result["validation_errors"]))

    def test_input_validation_non_strict_continues(self):
        result = decide(
            sample_bundle(),
            {"risk_flag": False},
            {"strict_validation": False},
        )
        self.assertFalse(result["input_valid"])
        # Without strict validation it still evaluates (income missing -> rule fails).
        self.assertEqual(result["outcome"], "reject")

    def test_new_operators_flow_through_core(self):
        bundle = {
            "policy": {"id": "p", "steps": [{"type": "rule", "ref_id": "r"}]},
            "rules": {
                "r": {
                    "id": "r",
                    "rule_format": "v2",
                    "tree": {
                        "type": "group",
                        "logic": "AND",
                        "children": [
                            {"type": "condition", "variable": "state", "operator": "in", "value": ["KA", "MH"]},
                            {"type": "condition", "variable": "income", "operator": "between", "value": 40000, "value2": 60000},
                        ],
                        "onPass": "approve",
                        "onFail": "reject",
                    },
                }
            },
        }
        approved = decide(bundle, {"state": "KA", "income": 50000})
        rejected = decide(bundle, {"state": "TN", "income": 50000})
        self.assertEqual(approved["outcome"], "approve")
        self.assertEqual(rejected["outcome"], "reject")

    def test_experiment_override_changes_threshold(self):
        bundle = sample_bundle()
        bundle["experiments"] = [
            {
                "id": "exp1",
                "status": "running",
                "target_policy_id": "credit_v1",
                "variants": [
                    {"id": "control", "weight": 0, "overrides": {}},
                    {
                        "id": "challenger",
                        "weight": 100,
                        "overrides": {"rule_income.conditions.0.value": 90000},
                    },
                ],
            }
        ]
        # subject deterministically lands in challenger (weight 100) -> threshold 90k.
        result = decide(bundle, {"annual_income": 80000, "risk_flag": False}, {"subject_id": "user-123"})
        self.assertEqual(result["experiment_variant"], "challenger")
        self.assertEqual(result["outcome"], "reject")  # 80k < 90k now

    def test_validate_input_helper(self):
        schema = {"fields": {"pan": {"type": "string", "pattern": r"^[A-Z]{5}[0-9]{4}[A-Z]$"}}}
        self.assertEqual(validate_input({"pan": "ABCDE1234F"}, schema), [])
        self.assertTrue(validate_input({"pan": "bad"}, schema))


if __name__ == "__main__":
    unittest.main()
