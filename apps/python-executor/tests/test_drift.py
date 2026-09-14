"""Population Stability Index (parity P20's drift half).

PSI rather than KL divergence or a KS test because this is a credit decisioning product, and PSI
is the metric a credit risk team already reads -- its bands (0.10 / 0.25) are conventional, so a
number here means the same thing in a model risk committee as in any other monitoring pack.

THE TESTS THAT MATTER MOST ARE THE ZERO-BIN ONES. PSI divides by the expected proportion and logs
the ratio, so an empty bin is a division by zero, and the obvious fix -- skip empty bins -- is
wrong in the dangerous direction: a bin that was 20% of the baseline and is now 0% is the
strongest drift signal there is, and skipping it reports STABLE. Everything else here is
arithmetic; that one is the difference between a monitoring feature and a monitoring feature that
lies.
"""
from __future__ import annotations

import math
import os
import random
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import drift  # noqa: E402


def normal(mean, sd, n, seed):
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


class TheMetricBehavesTest(unittest.TestCase):

    def test_a_population_compared_with_itself_has_essentially_no_drift(self):
        values = normal(700, 50, 2000, 1)
        result = drift.psi(values, values)
        self.assertLess(result["psi"], 0.001)
        self.assertEqual(result["classification"], drift.STABLE)

    def test_a_fresh_sample_of_the_same_distribution_reads_stable(self):
        result = drift.psi(normal(700, 50, 2000, 1), normal(700, 50, 2000, 2))
        self.assertEqual(result["classification"], drift.STABLE,
                         "sampling noise alone was reported as drift: {0}".format(result["psi"]))

    def test_a_shifted_population_reads_significant(self):
        result = drift.psi(normal(700, 50, 2000, 1), normal(620, 50, 2000, 2))
        self.assertEqual(result["classification"], drift.SIGNIFICANT)
        self.assertGreater(result["psi"], 0.25)

    def test_psi_grows_monotonically_with_the_size_of_the_shift(self):
        """A metric that is merely 'large when things change' is not enough -- an operator
        watches the trend, so more shift has to mean a bigger number."""
        baseline = normal(700, 50, 3000, 1)
        scores = [drift.psi(baseline, normal(700 - shift, 50, 3000, 2))["psi"]
                  for shift in (0, 10, 30, 60, 100)]
        self.assertEqual(scores, sorted(scores), "PSI did not increase with the shift: {0}".format(scores))

    def test_a_widening_distribution_is_detected_even_with_the_same_mean(self):
        """Variance drift with a stable mean is the case a mean-comparison would miss entirely."""
        result = drift.psi(normal(700, 20, 3000, 1), normal(700, 90, 3000, 2))
        self.assertNotEqual(result["classification"], drift.STABLE)


class TheZeroBinCasesTest(unittest.TestCase):
    """The reason this module is not three lines."""

    def test_a_bin_that_emptied_completely_still_contributes(self):
        """A fifth of the baseline population disappearing is the strongest signal available.
        Skipping empty bins -- the obvious way to avoid the div-by-zero -- reports it as stable."""
        baseline = list(range(0, 1000))
        # Everything that was in the bottom fifth is gone.
        current = [v for v in baseline if v >= 200]
        result = drift.psi(baseline, current)
        self.assertGreater(result["psi"], 0.10,
                           "a vanished bin was reported as stable: {0}".format(result["psi"]))
        vanished = [b for b in result["bins"] if b["current_pct"] == 0 and b["baseline_pct"] > 0]
        self.assertTrue(vanished, "no bin was recorded as emptied")
        self.assertGreater(vanished[0]["contribution"], 0)

    def test_a_bin_that_appeared_from_nothing_contributes(self):
        baseline = [500.0] * 500 + [510.0] * 500
        current = baseline + [50.0] * 400
        self.assertGreater(drift.psi(baseline, current)["psi"], 0)

    def test_no_nan_or_infinity_ever_escapes(self):
        """A NaN in a monitoring number is worse than no number: it sorts oddly, compares false
        against every threshold, and renders as blank."""
        cases = [
            ([1.0] * 100, [2.0] * 100),
            (list(range(100)), [0.0] * 100),
            ([0.0] * 100, list(range(100))),
            (list(range(100)), list(range(1000, 1100))),
        ]
        for baseline, current in cases:
            with self.subTest(baseline=baseline[:2], current=current[:2]):
                value = drift.psi(baseline, current)["psi"]
                self.assertIsNotNone(value)
                self.assertFalse(math.isnan(value), "PSI was NaN")
                self.assertFalse(math.isinf(value), "PSI was infinite")


class DegenerateInputTest(unittest.TestCase):

    def test_too_little_baseline_is_undecidable_not_stable(self):
        """Zero would read as "no drift", and there is no evidence for that."""
        result = drift.psi([1.0], [1.0, 2.0, 3.0])
        self.assertIsNone(result["psi"])
        self.assertEqual(result["classification"], "insufficient_data")

    def test_an_empty_current_period_is_undecidable(self):
        result = drift.psi(list(range(100)), [])
        self.assertIsNone(result["psi"])
        self.assertEqual(result["classification"], "insufficient_data")

    def test_nones_are_ignored_rather_than_counted_as_zero(self):
        """A failed variable is None, and counting it as 0.0 would invent a spike at the bottom
        of the distribution -- drift that is really a computation failure."""
        baseline = normal(700, 50, 500, 1)
        with_nones = normal(700, 50, 500, 2) + [None] * 100
        self.assertEqual(drift.psi(baseline, with_nones)["current_count"], 500)

    def test_a_constant_baseline_does_not_divide_by_zero(self):
        """60% of a population sharing one value (a floor, a cap, a default) produces repeated
        quantiles, and a zero-width bin would take every one of them."""
        result = drift.psi([500.0] * 1000, [500.0] * 900 + [700.0] * 100)
        self.assertIsNotNone(result["psi"])
        self.assertFalse(math.isnan(result["psi"]))


class TheBinsAreUsableTest(unittest.TestCase):
    """"PSI is 0.31" says the population moved. The per-bin table is what says WHERE, which is
    the difference between a dashboard number and something someone can act on."""

    def test_the_contributions_sum_to_the_total(self):
        result = drift.psi(normal(700, 50, 1000, 1), normal(650, 60, 1000, 2))
        self.assertAlmostEqual(sum(b["contribution"] for b in result["bins"]), result["psi"], places=4)

    def test_the_bins_carry_their_boundaries_and_the_tails_are_open(self):
        result = drift.psi(normal(700, 50, 1000, 1), normal(700, 50, 1000, 2))
        self.assertIsNone(result["bins"][0]["lower"], "the bottom bin is not open")
        self.assertIsNone(result["bins"][-1]["upper"], "the top bin is not open")

    def test_edges_come_from_the_baseline_so_periods_stay_comparable(self):
        """Recomputing edges from the combined data would make each period's PSI incomparable
        with the last, which defeats tracking it over time."""
        baseline = normal(700, 50, 1000, 1)
        edges = drift.quantile_edges(baseline)
        moved = drift.psi(baseline, normal(500, 50, 1000, 2))
        self.assertEqual([b["upper"] for b in moved["bins"][:-1]], edges)

    def test_bins_are_quantiles_so_a_skewed_population_is_spread_evenly(self):
        """Equal-width bins would put almost every observation in two or three of them for a
        skewed distribution -- which is what a credit score IS -- leaving PSI dominated by
        rounding in the near-empty tails. Quantile bins mean the BASELINE is spread roughly
        evenly, and that is the property that distinguishes the two schemes.
        """
        rng = random.Random(11)
        # Heavily right-skewed, like a score with a long thin upper tail.
        skewed = [rng.expovariate(1 / 50.0) for _ in range(4000)]
        result = drift.psi(skewed, skewed, bins=10)
        occupancy = [b["baseline_pct"] for b in result["bins"]]
        self.assertGreater(min(occupancy), 3.0,
                           "a baseline bin holds under 3% of the population, so the bins are not "
                           "quantiles: {0}".format([round(o, 1) for o in occupancy]))
        self.assertLess(max(occupancy), 25.0,
                        "a baseline bin holds over a quarter of the population: {0}".format(
                            [round(o, 1) for o in occupancy]))

    def test_a_heavily_duplicated_baseline_produces_no_zero_width_bins(self):
        """60% of a population sharing one value -- a floor, a cap, a default -- produces repeated
        quantiles. A zero-width bin would take every one of those observations into whichever
        side a boundary comparison happened to fall, so the edges have to be strictly
        increasing."""
        baseline = [500.0] * 600 + list(range(501, 901))
        edges = drift.quantile_edges(baseline, bins=10)
        self.assertEqual(edges, sorted(set(edges)),
                         "duplicate or unordered bin edges: {0}".format(edges))
        result = drift.psi(baseline, baseline)
        self.assertEqual(len(result["bins"]), len(edges) + 1)
        self.assertLess(result["psi"], 0.001)

    def test_the_bands_are_the_conventional_ones(self):
        self.assertEqual(drift.classify(0.05), drift.STABLE)
        self.assertEqual(drift.classify(0.10), drift.MODERATE)
        self.assertEqual(drift.classify(0.24), drift.MODERATE)
        self.assertEqual(drift.classify(0.25), drift.SIGNIFICANT)


if __name__ == "__main__":
    unittest.main()
