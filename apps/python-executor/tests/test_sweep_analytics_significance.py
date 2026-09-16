"""The headline p-value must name the pair it measured, and pick that pair by ROLE.

`experiment_results` reported one `significance.pValue` for the whole experiment while computing
it from `variant_rows[0]` and `variant_rows[1]` -- positional. Two consequences, both silent:

  * with three or more variants it described one arbitrary pair while appearing to describe the
    experiment;
  * if the champion was not listed first, "control" was not the champion, so the sign of the
    effect flipped.

Found by the evaluations sweep.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from app import analytics  # noqa: E402


class _FakeStorage:
    """Just enough surface for experiment_results: an experiment and a rollup."""

    def __init__(self, variants, rollup):
        self._variants = variants
        self._rollup = rollup

    def get_experiment(self, experiment_id, tenant_id=None):
        return {"id": experiment_id, "name": experiment_id, "status": "running",
                "variants": self._variants}

    def experiment_variant_rollup(self, tenant_id, experiment_id):
        return self._rollup


def _stats(users, approved):
    return {"users": users, "approved": approved, "rejected": users - approved,
            "reviewed": 0, "latency_sum": users * 10}


class SignificancePairTests(unittest.TestCase):
    def setUp(self):
        # The cache would hand a later test the earlier one's answer.
        patcher = mock.patch.object(analytics, "cache_get_json", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher2 = mock.patch.object(analytics, "cache_set_json", return_value=None)
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def _run(self, variants, rollup):
        return analytics.experiment_analytics(_FakeStorage(variants, rollup), "t1", "exp")

    def test_the_champion_is_the_control_even_when_it_is_not_listed_first(self):
        variants = [
            {"id": "b", "role": "challenger", "weight": 50},
            {"id": "a", "role": "champion", "weight": 50},
        ]
        rollup = {"a": _stats(1000, 500), "b": _stats(1000, 700)}
        significance = self._run(variants, rollup)["significance"]
        self.assertEqual(
            significance["control"], "a",
            "the champion is the baseline; taking position 0 makes the challenger the control "
            "and flips the sign of the effect")
        self.assertEqual(significance["treatment"], "b")

    def test_the_response_says_how_many_variants_the_p_value_covers(self):
        variants = [
            {"id": "a", "role": "champion", "weight": 25},
            {"id": "b", "role": "challenger", "weight": 25},
            {"id": "c", "role": "challenger", "weight": 25},
            {"id": "d", "role": "challenger", "weight": 25},
        ]
        rollup = {k: _stats(500, 250) for k in "abcd"}
        significance = self._run(variants, rollup)["significance"]
        self.assertEqual(significance["totalVariants"], 4)
        self.assertEqual(
            significance["comparedVariants"], 2,
            "one p-value cannot describe four arms; the response must say it covers two")
        self.assertEqual(significance["control"], "a")

    def test_a_real_difference_is_still_reported_as_significant(self):
        """The positive control: the pair selection must not break the statistic itself."""
        variants = [
            {"id": "a", "role": "champion", "weight": 50},
            {"id": "b", "role": "challenger", "weight": 50},
        ]
        rollup = {"a": _stats(2000, 600), "b": _stats(2000, 1000)}
        significance = self._run(variants, rollup)["significance"]
        self.assertTrue(
            significance["significant"],
            "30% vs 50% over 2000 users each must be significant; if this fails, the pair "
            "selection has broken the statistic rather than fixed the labelling")
        self.assertLess(significance["pValue"], 0.05)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
