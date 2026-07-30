"""Guards the simulation harness: correctness at scale + A/B recommendations.

Runs a smaller batch than the CLI (fast in CI) but exercises the same code so a
regression in the core, operators, or champion/challenger logic is caught.
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

from simulation.harness import (  # noqa: E402
    build_credit_bundle,
    generate_customers,
    run_ab_experiment,
    run_and_validate,
)


class SimulationTests(unittest.TestCase):
    def test_correctness_matches_oracle_at_scale(self):
        customers = generate_customers(2000, seed=42)
        report = run_and_validate(customers, build_credit_bundle())
        self.assertEqual(report["total"], 2000)
        self.assertTrue(report["valid"], f"oracle mismatches: {report['mismatches']}")
        # sanity: all three outcomes should appear in a 2k random sample
        self.assertGreater(report["outcomes"].get("approve", 0), 0)
        self.assertGreater(report["outcomes"].get("reject", 0), 0)

    def test_throughput_well_above_target(self):
        customers = generate_customers(2000, seed=1)
        report = run_and_validate(customers, build_credit_bundle())
        # even a slow CI core clears 200/sec single-threaded by a wide margin
        self.assertGreater(report["throughputPerSec"], 200)

    def test_ab_variants_split_and_recommend(self):
        customers = generate_customers(4000, seed=5)
        ab = run_ab_experiment(customers, "credit_sme", challenger_threshold=700)
        stats = ab["variantStats"]
        # deterministic 50/50 assignment gives both variants substantial traffic
        self.assertGreater(stats["champion"]["users"], 1000)
        self.assertGreater(stats["challenger"]["users"], 1000)
        challenger = ab["analysis"]["challengers"][0]
        # a stricter score floor lowers approvals -> negative lift, rollback call
        self.assertLess(challenger["liftPct"], 0)
        self.assertIn(challenger["recommendation"], {"rollback", "hold"})


if __name__ == "__main__":
    unittest.main()
