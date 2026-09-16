"""Audit tests for the EVALUATIONS / MODELS / A-B surface.

Each test here pins a defect that was reproduced against a running stack on 2026-09-16.
They are written to FAIL if the defect is fixed in a way that changes the observable
contract, so they double as regression markers for whoever fixes them.

Covers:
  * app/model_evaluation.py  -- null-cell error class, absent PSI, unrendered confusion,
                                WoE sentinels, promotion gate with no sample floor
  * app/analytics.py         -- the response key the web dashboard fails to read
  * app/champion_challenger.py -- recommendation is a string, not an object
  * app/storage.py + app/models.py -- promotion provenance silently dropped on write
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import numpy as np  # noqa: E402

from app import model_evaluation as me  # noqa: E402


def _clean_rows(n: int, seed: int = 0):
    """A perfectly calibrated dataset: the score IS the true event probability."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.02, 0.98, size=n)
    y = (rng.random(n) < p).astype(int)
    return [{"score": float(p[i]), "label": int(y[i])} for i in range(n)]


BINARY_CFG = {"task": "binary", "score_col": "score", "label_col": "label"}


class NullCellErrorClassTests(unittest.TestCase):
    """A null cell is caller error but surfaces as HTTP 500.

    routers/evaluations.py catches only (ValueError, KeyError). float(None) raises
    TypeError, which escapes and becomes a 500 Internal Server Error.
    """

    def test_null_score_cell_raises_typeerror(self):
        rows = [{"score": None, "label": 1}, {"score": 0.2, "label": 0}]
        with self.assertRaises(TypeError):
            me.run_evaluation(rows, BINARY_CFG)

    def test_null_label_cell_raises_typeerror(self):
        rows = [{"score": 0.9, "label": None}, {"score": 0.2, "label": 0}]
        with self.assertRaises(TypeError):
            me.run_evaluation(rows, BINARY_CFG)

    def test_typeerror_is_not_covered_by_the_routers_except_clause(self):
        # The exact tuple routers/evaluations.py uses on every run_evaluation call site.
        self.assertFalse(issubclass(TypeError, (ValueError, KeyError)))

    def test_non_numeric_string_is_a_valueerror_and_so_is_handled(self):
        # Contrast case: this one the router *does* translate to 422.
        rows = [{"score": "abc", "label": 1}, {"score": 0.2, "label": 0}]
        with self.assertRaises(ValueError):
            me.run_evaluation(rows, BINARY_CFG)

    def test_missing_column_is_a_keyerror_and_so_is_handled(self):
        rows = [{"score": 0.5, "label": 1}]
        with self.assertRaises(KeyError):
            me.run_evaluation(rows, {**BINARY_CFG, "score_col": "nope"})


class AdvertisedButAbsentMetricTests(unittest.TestCase):
    """PSI is advertised in the router docstring and the page subtitle but never computed."""

    def test_psi_function_exists(self):
        self.assertIsNotNone(me.psi([0.1, 0.5, 0.9] * 20, [0.2, 0.5, 0.8] * 20))

    def test_binary_evaluation_result_contains_no_psi(self):
        result = me.run_evaluation(_clean_rows(500), BINARY_CFG)
        keys = set(result["metrics"])
        self.assertFalse(
            any("psi" in k for k in keys),
            "PSI is advertised to the user but absent from metrics: {0}".format(sorted(keys)),
        )

    def test_psi_is_absent_from_every_task_result(self):
        for task, cfg, rows in [
            ("binary", BINARY_CFG, _clean_rows(200)),
            (
                "uplift",
                {"task": "uplift", "score_col": "score", "label_col": "label", "treatment_col": "t"},
                [{"score": 0.5, "label": 1, "t": 1}, {"score": 0.2, "label": 0, "t": 0}],
            ),
        ]:
            with self.subTest(task=task):
                result = me.run_evaluation(rows, cfg)
                self.assertNotIn("psi", set(result["metrics"]))


class ComputedButUnrenderedTests(unittest.TestCase):
    """The backend computes confusion + calibration; the dashboard renders neither.

    These assert the DATA is present, so the omission is provably a UI gap rather than a
    missing backend capability.
    """

    def test_confusion_matrix_is_present_with_all_four_cells(self):
        metrics = me.run_evaluation(_clean_rows(400), BINARY_CFG)["metrics"]
        self.assertIn("confusion", metrics)
        for cell in ("tp", "fp", "tn", "fn"):
            self.assertIn(cell, metrics["confusion"])

    def test_accuracy_is_present_in_the_payload(self):
        metrics = me.run_evaluation(_clean_rows(400), BINARY_CFG)["metrics"]
        self.assertIn("accuracy", metrics["confusion"])

    def test_confusion_cells_sum_to_the_row_count(self):
        rows = _clean_rows(400)
        c = me.run_evaluation(rows, BINARY_CFG)["metrics"]["confusion"]
        self.assertEqual(c["tp"] + c["fp"] + c["tn"] + c["fn"], len(rows))

    def test_calibration_curve_is_present(self):
        metrics = me.run_evaluation(_clean_rows(400), BINARY_CFG)["metrics"]
        self.assertTrue(metrics["calibration_curve"])


class DecileWoeUndefinedBandTests(unittest.TestCase):
    """A band with no events (or no non-events) has UNDEFINED weight of evidence, not a number.

    This class originally recorded the defect: `max(dist, 1e-12)` clamped the division, so an
    infinity came back as a finite +-26 and the UI printed it straight into the "WoE" column.
    Real weight-of-evidence lives in roughly [-3, 3], so a displayed 25.79 was indistinguishable
    from a genuine (absurd) value, and a client had no way to detect the sentinel -- it was
    finite, so `isfinite` said it was fine.

    FIXED in model_evaluation.decile_table: an undefined band now returns None, which the
    dashboard's `fmt` helper already renders as a dash. The assertions below are inverted from
    what they were, deliberately -- pinning a sentinel as correct is how it becomes permanent.
    """

    def _bands(self):
        # Top half all events, bottom half none -> bands at both extremes are undefined.
        y = [1] * 50 + [0] * 50
        s = [0.9 - i * 0.001 for i in range(100)]
        return me.decile_table(y, s)["bands"]

    def test_a_zero_event_band_reports_undefined_rather_than_a_sentinel(self):
        empty = [b for b in self._bands() if b["events"] == 0]
        self.assertTrue(empty, "expected at least one zero-event band")
        for band in empty:
            self.assertIsNone(
                band["woe"],
                "a band with no events has no weight of evidence; a finite number here is an "
                "infinity in disguise")

    def test_an_all_event_band_reports_undefined_rather_than_a_sentinel(self):
        bands = self._bands()
        full = [b for b in bands if b["events"] == b["count"]]
        self.assertTrue(full, "expected at least one all-event band")
        for band in full:
            self.assertIsNone(band["woe"])

    def test_every_defined_woe_is_a_plausible_finite_value(self):
        """The other half of the fix: defined bands must still produce real numbers.

        Without this, returning None unconditionally would pass the two tests above -- which is
        the failure mode where a fix for a display bug quietly deletes the metric.

        NOTE the different fixture. `_bands()` is PERFECTLY separated, so every one of its bands
        is all-events or no-events and every WoE is legitimately undefined -- this control
        written against that data asserted on an empty list and failed, which is the control
        working. A realistic model has overlap, so its middle bands carry both classes.
        """
        # An imperfect ranker: the label is mostly-but-not-always ordered by score.
        y = [(i * 7) % 10 < 4 for i in range(200)]
        y = [int(v) for v in y]
        s = [1.0 - i * 0.004 for i in range(200)]
        bands = me.decile_table(y, s)["bands"]
        defined = [b["woe"] for b in bands if b["woe"] is not None]
        self.assertTrue(
            defined,
            "a model with class overlap must still produce real WoE values; blanking them all "
            "would mean the fix deleted the metric rather than guarding it")
        for woe in defined:
            self.assertTrue(np.isfinite(woe))
            self.assertLess(abs(woe), 20.0,
                            "a defined WoE of +-26 means the clamp is back")


class PromotionGateSampleFloorTests(unittest.TestCase):
    """The gate emits a confident pass/fail with no minimum-N guard and no monotonic tolerance."""

    def test_gate_returns_a_verdict_on_three_rows(self):
        rows = [{"score": 0.9, "label": 1}, {"score": 0.1, "label": 0}, {"score": 0.8, "label": 1}]
        result = me.run_evaluation(rows, BINARY_CFG)
        self.assertIn(result["gate_status"], {"pass", "fail"})
        self.assertNotEqual(result["gate_status"], "unknown")

    def test_gate_result_carries_no_sample_size_warning(self):
        rows = [{"score": 0.9, "label": 1}, {"score": 0.1, "label": 0}, {"score": 0.8, "label": 1}]
        gate_result = me.run_evaluation(rows, BINARY_CFG)["gate_result"]
        self.assertEqual(set(gate_result), {"passed", "status", "checks"})
        checks = {c["check"] for c in gate_result["checks"]}
        self.assertNotIn("min_sample", checks)
        self.assertNotIn("n>=min", checks)

    def test_monotonic_check_has_no_tolerance_for_one_adjacent_inversion(self):
        # Ten bands, perfectly ordered except one adjacent pair swapped by a hair.
        rates = [1.0, 0.9, 0.8, 0.7, 0.6, 0.45, 0.50, 0.3, 0.2, 0.1]
        y, s = [], []
        for band, rate in enumerate(rates):
            events = int(round(rate * 20))
            y.extend([1] * events + [0] * (20 - events))
            s.extend([1.0 - band * 0.1 - j * 0.0001 for j in range(20)])
        table = me.decile_table(y, s)
        self.assertFalse(table["monotonic"], "a single 0.05 inversion must be what trips this")
        verdict = me.gate({"gini": 0.8, "ece": 0.01, "decile_table": table})
        self.assertEqual(verdict["status"], "fail")

    def test_small_clean_samples_fail_the_monotonic_check_most_of_the_time(self):
        """Quantifies the false-fail rate on a PERFECTLY calibrated model at N=200."""
        fails = 0
        trials = 20
        for seed in range(trials):
            result = me.run_evaluation(_clean_rows(200, seed=seed), BINARY_CFG)
            checks = {c["check"]: c["passed"] for c in result["gate_result"]["checks"]}
            if not checks.get("decile_monotonic", True):
                fails += 1
        self.assertGreater(
            fails, trials // 2,
            "expected the un-toleranced monotonic check to false-fail a majority of clean N=200 samples",
        )

    def test_large_clean_samples_do_pass(self):
        """The counterpart: the gate is NOT broken in general, only sample-size sensitive."""
        result = me.run_evaluation(_clean_rows(4000, seed=1), BINARY_CFG)
        self.assertEqual(result["gate_status"], "pass")


class AnalyticsContractTests(unittest.TestCase):
    """The web dashboard reads keys the backend does not emit."""

    @classmethod
    def setUpClass(cls):
        import app.main as app_main
        from app.analytics import experiment_analytics

        cls.experiment_analytics = staticmethod(experiment_analytics)
        cls.storage = app_main.storage
        cls.tenant_id = cls.storage.default_tenant_id
        cls.exp_id = "sweep_contract_" + uuid.uuid4().hex[:8]
        cls.storage.create_or_update_experiment(
            {
                "id": cls.exp_id,
                "name": "sweep contract probe",
                "status": "draft",
                "hash_key": "user_id",
                "variants": [
                    {"id": "champion", "role": "champion", "weight": 50},
                    {"id": "challenger", "role": "challenger", "weight": 50},
                ],
            },
            tenant_id=cls.tenant_id,
        )

    def _analytics(self):
        # Bypass the 300s result cache so each assertion sees a freshly computed payload.
        from app import analytics as analytics_mod

        original = analytics_mod.cache_get_json
        analytics_mod.cache_get_json = lambda *a, **k: None
        try:
            return self.experiment_analytics(self.storage, self.tenant_id, self.exp_id)
        finally:
            analytics_mod.cache_get_json = original

    def test_champion_challenger_key_is_camelcase(self):
        """apps/web/app/ab-tests/page.tsx reads `champion_challenger`; the API emits camelCase."""
        result = self._analytics()
        self.assertIn("championChallenger", result)
        self.assertNotIn("champion_challenger", result)

    def test_challenger_recommendation_is_a_plain_string(self):
        """The page reads `recommendation.action`; a string has no `.action`."""
        challengers = self._analytics()["championChallenger"]["challengers"]
        self.assertTrue(challengers)
        for row in challengers:
            self.assertIsInstance(row["recommendation"], str)
            self.assertIn(row["recommendation"], {"promote", "hold", "rollback"})

    def test_significance_compares_only_the_first_two_variants(self):
        """With 3+ variants the headline p-value silently describes variants[0] vs [1]."""
        import inspect

        from app import analytics as analytics_mod

        source = inspect.getsource(analytics_mod.experiment_analytics)
        self.assertIn("variant_rows[0], variant_rows[1]", source)

    def test_experiment_results_are_cached_without_decision_invalidation(self):
        """A 300s TTL with no bust on new decisions: fresh traffic reads as zero for 5 minutes."""
        import inspect

        from app import analytics as analytics_mod

        source = inspect.getsource(analytics_mod.experiment_analytics)
        self.assertIn("ttl_seconds=300", source)


class PromotionProvenanceTests(unittest.TestCase):
    """promote_experiment writes promotion provenance that storage silently discards."""

    @classmethod
    def setUpClass(cls):
        import app.main as app_main

        cls.storage = app_main.storage
        cls.tenant_id = cls.storage.default_tenant_id

    def test_promotion_fields_are_dropped_on_write(self):
        exp_id = "sweep_promo_" + uuid.uuid4().hex[:8]
        self.storage.create_or_update_experiment(
            {
                "id": exp_id,
                "name": "sweep promo probe",
                "status": "draft",
                "variants": [{"id": "champion", "role": "champion", "weight": 100}],
            },
            tenant_id=self.tenant_id,
        )
        # Exactly the payload routers/experiments.py::promote_experiment persists.
        written = self.storage.create_or_update_experiment(
            {
                "id": exp_id,
                "name": "sweep promo probe",
                "status": "completed",
                "variants": [{"id": "champion", "role": "champion", "weight": 100}],
                "promoted_variant_id": "challenger",
                "promoted_by": "audit@example.com",
                "promoted_at": "2026-09-16T00:00:00Z",
            },
            tenant_id=self.tenant_id,
        )
        # The status change survives...
        self.assertEqual(written["status"], "completed")
        # ...but every promotion field is gone, and the write reported success.
        read_back = self.storage.get_experiment(exp_id, tenant_id=self.tenant_id)
        for field in ("promoted_variant_id", "promoted_by", "promoted_at"):
            with self.subTest(field=field):
                self.assertIsNone(written.get(field))
                self.assertIsNone(read_back.get(field))

    def test_experiment_orm_has_no_promotion_columns(self):
        from app.models import Experiment

        columns = set(Experiment.__table__.columns.keys())
        for field in ("promoted_variant_id", "promoted_by", "promoted_at"):
            self.assertNotIn(field, columns)


if __name__ == "__main__":
    unittest.main()
