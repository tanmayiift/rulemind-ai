"""Unit tests for the predictive-model evaluation metrics (app/model_evaluation.py).

Pure-numpy metrics checked against known-answer fixtures: perfect separation, random,
calibration, PSI stability, decile monotonicity, multi-label, and uplift (Qini)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import numpy as np  # noqa: E402

from app import model_evaluation as me  # noqa: E402


class TestDiscrimination(unittest.TestCase):
    def test_perfect_separation(self):
        y = [0, 0, 0, 1, 1, 1]
        s = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        self.assertAlmostEqual(me.roc_auc(y, s), 1.0, places=6)
        self.assertAlmostEqual(me.gini_from_auc(me.roc_auc(y, s)), 1.0, places=6)
        self.assertAlmostEqual(me.ks_statistic(y, s), 1.0, places=6)

    def test_inverted_separation(self):
        y = [1, 1, 1, 0, 0, 0]
        s = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9]
        self.assertAlmostEqual(me.roc_auc(y, s), 0.0, places=6)

    def test_random_is_half(self):
        rng = np.random.default_rng(0)
        y = rng.integers(0, 2, 5000)
        s = rng.random(5000)
        self.assertAlmostEqual(me.roc_auc(y, s), 0.5, delta=0.03)

    def test_single_class_returns_none(self):
        self.assertIsNone(me.roc_auc([1, 1, 1], [0.1, 0.2, 0.3]))
        self.assertIsNone(me.ks_statistic([0, 0], [0.1, 0.2]))

    def test_auc_matches_ties(self):
        # all-tied scores → AUC 0.5 exactly (tie-aware ranks)
        self.assertAlmostEqual(me.roc_auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]), 0.5, places=6)


class TestCalibration(unittest.TestCase):
    def test_brier_bounds(self):
        self.assertAlmostEqual(me.brier_score([1, 0], [1.0, 0.0]), 0.0, places=6)
        self.assertAlmostEqual(me.brier_score([1, 0], [0.0, 1.0]), 1.0, places=6)

    def test_ece_well_calibrated_low(self):
        rng = np.random.default_rng(3)
        p = rng.random(20000)
        y = (rng.random(20000) < p).astype(int)  # perfectly calibrated by construction
        self.assertLess(me.expected_calibration_error(y, p), 0.03)

    def test_ece_miscalibrated_high(self):
        rng = np.random.default_rng(3)
        p = rng.random(20000)
        y = (rng.random(20000) < np.clip(p - 0.3, 0, 1)).astype(int)  # over-predicts
        self.assertGreater(me.expected_calibration_error(y, p), 0.1)


class TestStability(unittest.TestCase):
    def test_psi_identical_zero(self):
        rng = np.random.default_rng(5)
        a = rng.random(5000)
        self.assertAlmostEqual(me.psi(a, a), 0.0, places=6)

    def test_psi_shift_positive(self):
        rng = np.random.default_rng(5)
        a = rng.random(5000)
        b = rng.random(5000) + 0.5
        self.assertGreater(me.psi(a, b), 0.25)


class TestDecile(unittest.TestCase):
    def test_monotonic_on_signal(self):
        rng = np.random.default_rng(7)
        p = rng.random(6000)
        y = (rng.random(6000) < p).astype(int)
        table = me.decile_table(y, p, n_bands=10)
        self.assertEqual(len(table["bands"]), 10)
        self.assertTrue(table["monotonic"])
        # highest-score band should have a higher event rate than the lowest
        self.assertGreater(table["bands"][0]["event_rate"], table["bands"][-1]["event_rate"])


class TestConfusionAndBootstrap(unittest.TestCase):
    def test_confusion_counts(self):
        y = [1, 1, 0, 0]
        s = [0.9, 0.4, 0.6, 0.1]
        c = me.confusion_at(y, s, threshold=0.5)
        self.assertEqual((c["tp"], c["fp"], c["tn"], c["fn"]), (1.0, 1.0, 1.0, 1.0))

    def test_bootstrap_ci_brackets_point(self):
        rng = np.random.default_rng(9)
        p = rng.random(3000)
        y = (rng.random(3000) < p).astype(int)
        point = me.gini_from_auc(me.roc_auc(y, p))
        ci = me.bootstrap_ci(y, p, lambda a, b: me.gini_from_auc(me.roc_auc(a, b)), n_resamples=300, seed=0)
        self.assertIsNotNone(ci)
        self.assertLessEqual(ci["lo"], point)
        self.assertGreaterEqual(ci["hi"], point)


class TestMultiLabelAndUplift(unittest.TestCase):
    def test_per_label_auc_signal(self):
        rng = np.random.default_rng(11)
        n, L = 2000, 3
        Ys = rng.random((n, L))
        Yt = (rng.random((n, L)) < Ys).astype(int)  # scores predict labels
        out = me.per_label_auc(Yt, Ys, ["a", "b", "c"])
        self.assertGreater(out["macro_auc"], 0.7)
        self.assertGreater(me.precision_at_k(Yt, Ys, 1), 0.5)

    def test_qini_positive_for_real_uplift(self):
        rng = np.random.default_rng(13)
        n = 8000
        t = rng.integers(0, 2, n)
        sc = rng.random(n)
        # treated respond more, and more so when score is high → positive uplift signal
        prob = np.clip(0.1 + 0.6 * sc * t, 0, 1)
        y = (rng.random(n) < prob).astype(int)
        q = me.qini(t, y, sc)
        self.assertGreater(q["qini_coefficient"], 0.0)


class TestGateAndDispatch(unittest.TestCase):
    def test_gate_fails_on_low_gini(self):
        metrics = {"gini": 0.2, "gini_ci": {"lo": 0.15}, "ece": 0.01, "decile_table": {"monotonic": True}}
        verdict = me.gate(metrics)
        self.assertEqual(verdict["status"], "fail")

    def test_gate_flags_leakage(self):
        metrics = {"gini": 0.95, "gini_ci": {"lo": 0.93}, "ece": 0.01, "decile_table": {"monotonic": True}}
        verdict = me.gate(metrics)
        self.assertFalse(verdict["passed"])  # leakage guard trips at gini>0.90

    def test_run_evaluation_binary_with_segments_and_temporal(self):
        rng = np.random.default_rng(15)
        p = rng.random(2000)
        y = (rng.random(2000) < p).astype(int)
        rows = [
            {"score": float(p[i]), "label": int(y[i]), "seg": "A" if i % 2 else "B", "d": f"2026-0{1 + i % 6}-01"}
            for i in range(len(p))
        ]
        res = me.run_evaluation(rows, {"task": "binary", "segment_col": "seg", "date_col": "d", "date_freq": "month"})
        self.assertIn("gini", res["metrics"])
        self.assertEqual(len(res["segments"]["slices"]), 2)
        self.assertEqual(len(res["temporal"]["buckets"]), 6)
        self.assertIn(res["gate_status"], {"pass", "fail"})

    def test_run_evaluation_rejects_empty(self):
        with self.assertRaises(ValueError):
            me.run_evaluation([], {"task": "binary"})


if __name__ == "__main__":
    unittest.main()
