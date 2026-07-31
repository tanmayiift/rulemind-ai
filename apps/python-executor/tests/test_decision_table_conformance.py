"""Cross-engine decision-table conformance — the Python arm.

Every RuleMind decision-table evaluator (this Python core, and the Kotlin + Dart
on-device SDKs) is validated against the SAME fixture: packages/shared/decision-
tables.spec.json. The Python core is the source of truth; the Kotlin arm lives at
packages/sdk-android/.../DecisionTableConformanceTest.kt and the Dart arm at
packages/sdk-flutter/test/decision_table_conformance_test.dart.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.decision_tables import evaluate_decision_table  # noqa: E402

SPEC = json.loads((APP_ROOT.parents[1] / "packages" / "shared" / "decision-tables.spec.json").read_text())


class DecisionTableConformanceTests(unittest.TestCase):
    def test_every_case_matches_expected_outcome(self):
        tables = SPEC["tables"]
        for case in SPEC["cases"]:
            table = tables[case["table"]]
            result = evaluate_decision_table(table, case["variables"])
            self.assertEqual(result.get("outcome"), case["expectedOutcome"], case["name"])

    def test_spec_exercises_every_hit_policy(self):
        policies = {t.get("hit_policy") for t in SPEC["tables"].values()}
        self.assertTrue({"first", "priority", "collect"}.issubset(policies))


if __name__ == "__main__":
    unittest.main()
