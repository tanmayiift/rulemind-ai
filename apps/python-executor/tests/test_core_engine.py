"""Tests for the stateless, DB-free decision core (app/core).

Also asserts the core imports and runs with NO database configured — the proof
that it is hostable anywhere (K8s pod, serverless, edge, SDK host).
"""
from __future__ import annotations

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

    def test_control_flow_steps_fail_closed_instead_of_being_skipped(self):
        """`loop`/`branch`/`workflow`/`monitor` were in neither step-type set.

        They fell through to `else`, wrote "unknown step type" into the trace, and the loop
        CONTINUED -- so the core returned whatever the remaining steps decided, at full
        confidence. Because a branch decides WHICH steps run, an approve-by-default policy
        whose reject lives inside one came back as approve. what-if, backtest and governance
        replay are exactly the surfaces used to justify a decision after the fact.
        """
        bundle = sample_bundle()
        # An approving payload, with the gate that would reject buried in a branch the core
        # cannot evaluate. Before the fix this returned "approve" and nothing above the trace
        # said otherwise.
        bundle["policy"]["steps"] = [
            {"type": "outcome", "ref_id": "approve"},
            {"type": "branch", "ref_id": "risk_split"},
        ]
        out = decide(bundle, {"annual_income": 120000, "risk_flag": False}, {})

        self.assertEqual(out["outcome"], "review", "an unevaluable branch must not approve")
        self.assertFalse(out["input_valid"])
        self.assertTrue(any("branch" in e for e in out["validation_errors"]), out["validation_errors"])
        entry = next(t for t in out["trace"] if t.get("step", {}).get("type") == "branch")
        self.assertTrue(entry.get("undecidable"))
        # Not "deferred": that means a host will resolve it, and for this core there is no host.
        self.assertNotIn("deferred", entry)

    def test_a_genuinely_unknown_step_type_also_fails_closed(self):
        bundle = sample_bundle()
        bundle["policy"]["steps"] = [
            {"type": "outcome", "ref_id": "approve"},
            {"type": "teleport", "ref_id": "x"},
        ]
        out = decide(bundle, {"annual_income": 120000, "risk_flag": False}, {})
        self.assertEqual(out["outcome"], "review")
        self.assertFalse(out["input_valid"])

    def test_empty_variables_does_not_fall_back_to_payload(self):
        """An explicit empty variables map must NOT make the payload the feature map.

        `dict(context.get("variables") or payload or {})` treated `{}` as absent,
        so a caller that computed zero variables silently decided on raw payload
        keys. That is a fail-open: it promotes unvalidated caller input into the
        position of authored, typed variables. Under COMPUTE_ONLY_USED_VARS a
        policy that reads no variables produces exactly that empty map, so this
        is reachable, not theoretical.
        """
        payload = {"annual_income": 80000, "risk_flag": False}
        result = decide(
            sample_bundle(),
            payload,
            {"variables": {}, "strict_validation": False},
        )
        # With the payload leaking in, rule_income sees annual_income=80000 and
        # the policy approves. With no variables it cannot, and rejects.
        self.assertEqual(result["outcome"], "reject")
        self.assertEqual(result["variables"], {})

    def test_absent_variables_key_still_uses_payload(self):
        """The legacy contract stays: no "variables" key at all means payload-as-features."""
        payload = {"annual_income": 80000, "risk_flag": False}
        self.assertEqual(decide(sample_bundle(), payload)["outcome"], "approve")
        self.assertEqual(
            decide(sample_bundle(), payload, {"strict_validation": False})["outcome"],
            "approve",
        )

    def test_validate_input_helper(self):
        schema = {"fields": {"pan": {"type": "string", "pattern": r"^[A-Z]{5}[0-9]{4}[A-Z]$"}}}
        self.assertEqual(validate_input({"pan": "ABCDE1234F"}, schema), [])
        self.assertTrue(validate_input({"pan": "bad"}, schema))


if __name__ == "__main__":
    unittest.main()
