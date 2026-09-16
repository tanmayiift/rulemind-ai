"""The decile rank-ordering gate must not fail good models for being small.

`decile_monotonic` demanded STRICT whole-sequence monotonicity of the per-band event rate. On a
PERFECTLY CALIBRATED model the measured false-fail rate was:

    N=200 -> 90%      N=500 -> 70%      N=1000 -> 30%      N=4000 -> 2%

That curve is binomial noise in each band shrinking as N grows, not a modelling defect. The gate
also emitted a confident pass/fail on 3 rows, and the dashboard ships a 2-row placeholder
dataset. A gate that fails 90% of good models at N=200 does not protect anyone -- it trains
people to override it, and this sweep separately found a UI that hardcoded `force: true`, which
is what a gate that cries wolf produces.

Two changes, and the second is the one that needs guarding hardest: an inversion now counts only
when it exceeds 2 standard errors of the difference between the two bands, and below
MONOTONIC_MIN_SAMPLES the check reports None ("not applicable") instead of a verdict.

**The risk in that is over-correction** -- a tolerance wide enough to pass anything is the same
as deleting the check. Hence `test_a_genuinely_bad_ranker_still_fails`, which is the most
important test in this file.
"""
from __future__ import annotations

import os
import random
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import model_evaluation as me  # noqa: E402


def _calibrated(n: int, seed: int):
    """A perfectly calibrated ranker: P(event) equals the score."""
    rng = random.Random(seed)
    scores = [i / n for i in range(n)]
    labels = [1 if rng.random() < p else 0 for p in scores]
    return labels, scores


def _humped(n: int, seed: int):
    """A genuinely broken ranker: events concentrate in the MIDDLE of the score range, so the
    band rates rise then fall. This is a real rank-ordering failure, not noise."""
    rng = random.Random(seed)
    scores = [i / n for i in range(n)]
    labels = [1 if rng.random() < (1.0 - abs(2 * p - 1.0)) else 0 for p in scores]
    return labels, scores


class MonotonicSampleFloorTests(unittest.TestCase):
    def test_a_small_sample_reports_not_applicable_rather_than_a_verdict(self):
        labels, scores = _calibrated(200, seed=7)
        table = me.decile_table(labels, scores)
        self.assertIsNone(
            table["monotonic"],
            "200 rows cannot answer this question; a False here is a false accusation")
        self.assertEqual(table["monotonic_samples"], 200)

    def test_the_old_strict_answer_is_still_reported(self):
        """The change must be visible, not silent -- a caller can still see what strict said."""
        labels, scores = _calibrated(200, seed=7)
        table = me.decile_table(labels, scores)
        self.assertIn("monotonic_strict", table)
        self.assertIsInstance(table["monotonic_strict"], bool)

    def test_a_calibrated_model_above_the_floor_passes(self):
        labels, scores = _calibrated(4000, seed=3)
        self.assertTrue(me.decile_table(labels, scores)["monotonic"])

    def test_a_genuinely_bad_ranker_still_fails(self):
        """THE control. A tolerance wide enough to pass this would have deleted the check."""
        labels, scores = _humped(4000, seed=11)
        table = me.decile_table(labels, scores)
        self.assertFalse(
            table["monotonic"],
            "a ranker whose event rate peaks in the middle is not rank-ordered; if the noise "
            "tolerance passes this, it is too wide and the gate no longer gates")

    def test_the_false_fail_rate_on_good_models_is_now_low(self):
        """The defect stated as a measurement, so a regression is visible as a number.

        Strict monotonicity failed ~70% of calibrated N=500 models. Repeats across seeds rather
        than a single sample, because a single clean sample is not evidence about a rate.
        """
        strict_failures = 0
        reported_failures = 0
        trials = 30
        for seed in range(trials):
            labels, scores = _calibrated(500, seed=seed)
            table = me.decile_table(labels, scores)
            if not table["monotonic_strict"]:
                strict_failures += 1
            if table["monotonic"] is False:
                reported_failures += 1
        self.assertGreater(
            strict_failures, trials // 3,
            "if strict monotonicity no longer fails calibrated N=500 models often, this fixture "
            "has drifted and the rest of this test proves nothing")
        self.assertEqual(
            reported_failures, 0,
            "a calibrated model must never be REPORTED as non-monotonic; strict flagged "
            f"{strict_failures}/{trials} of these")


class GateNotApplicableTests(unittest.TestCase):
    def _gate_check(self, table):
        verdict = me.gate({"decile_table": table, "gini": 0.7, "ece": 0.01})
        return next(c for c in verdict["checks"] if c["check"] == "decile_monotonic")

    def test_a_not_applicable_check_does_not_sink_the_gate(self):
        labels, scores = _calibrated(200, seed=7)
        check = self._gate_check(me.decile_table(labels, scores))
        self.assertTrue(check["passed"])
        self.assertTrue(check["not_applicable"])
        self.assertIn("1000", check["reason"], "the reason must name the floor it fell short of")

    def test_a_real_rank_ordering_failure_still_fails_the_gate(self):
        labels, scores = _humped(4000, seed=11)
        check = self._gate_check(me.decile_table(labels, scores))
        self.assertFalse(check["passed"])
        self.assertNotIn("not_applicable", check)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
