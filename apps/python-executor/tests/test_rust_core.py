"""Rust eval-core conformance — the 5th engine against the shared operator spec.

Skips cleanly if the Rust extension isn't built (so CI without a Rust toolchain
still passes). Build it with:  maturin develop --release  in packages/rulemind-core-rs.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

SPEC_PATH = APP_ROOT.parents[1] / "packages" / "shared" / "operators.spec.json"

try:
    import rulemind_core_rs  # type: ignore

    HAVE_RUST = True
except ImportError:
    HAVE_RUST = False


@unittest.skipUnless(HAVE_RUST, "rulemind_core_rs (Rust) not built")
class RustCoreConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = json.loads(SPEC_PATH.read_text())

    def test_all_operators_have_fixtures(self) -> None:
        covered = {case["operator"] for case in self.spec["cases"]}
        for entry in self.spec["operators"]:
            self.assertIn(entry["op"], covered)

    def test_every_fixture_matches_rust(self) -> None:
        for case in self.spec["cases"]:
            with self.subTest(case=case["name"]):
                result = rulemind_core_rs.compare(
                    case.get("actual"),
                    case["operator"],
                    case.get("value"),
                    case.get("value2"),
                    case.get("fieldType"),
                )
                self.assertEqual(result, case["expected"], case["name"])

    def test_tree_evaluation(self) -> None:
        tree = json.dumps(
            {
                "type": "group",
                "logic": "AND",
                "children": [
                    {"type": "condition", "variable": "score", "operator": ">=", "value": 700},
                    {"type": "condition", "variable": "state", "operator": "in", "value": ["KA", "MH"]},
                ],
                "onPass": "approve",
                "onFail": "reject",
            }
        )
        self.assertEqual(rulemind_core_rs.evaluate_tree(tree, json.dumps({"score": 750, "state": "KA"})), "approve")
        self.assertEqual(rulemind_core_rs.evaluate_tree(tree, json.dumps({"score": 650, "state": "KA"})), "reject")


if __name__ == "__main__":
    unittest.main()
