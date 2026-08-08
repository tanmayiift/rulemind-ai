"""CI gate for the full executable test matrix (simulation/full_matrix.py).

Runs the 2,000+ real generated assertions across all 12 domains and fails if ANY assertion
fails or if the executed count regresses below the 1,620-scenario target.
"""
import unittest

from simulation.full_matrix import run_all


class FullMatrixTest(unittest.TestCase):
    def test_full_matrix_all_pass_and_meets_scale(self) -> None:
        domains, total, failed = run_all()
        detail = "\n".join(
            "  [{0}] {1}: {2}/{3}{4}".format(
                "PASS" if d.failed == 0 else "FAIL", d.name, d.passed, d.total,
                "" if not d.fails else "  first-fail={0}".format(d.fails[0]))
            for d in domains
        )
        self.assertEqual(failed, 0, "matrix had {0} failing assertion(s):\n{1}".format(failed, detail))
        self.assertGreaterEqual(total, 1620, "matrix executed only {0} assertions (< 1,620 target)".format(total))
        # Every one of the 12 domains must contribute real executed cases.
        self.assertEqual(len(domains), 12)
        for d in domains:
            self.assertGreater(d.total, 0, "domain {0} executed nothing".format(d.name))


if __name__ == "__main__":
    unittest.main()
