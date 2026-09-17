"""Model-evaluation ENDPOINTS against datasets with hand-computable metrics (#127).

#127 records the gap precisely: "The CSV parser feeding them is unit-tested; the endpoints
themselves are not." test_model_evaluation.py covers app/model_evaluation.py directly, so the
maths is checked -- but nothing asserts that POSTing a dataset returns those numbers. Column
mapping, task dispatch, persistence and the response shape all sit between the two, and a
mistake in any of them (a swapped score/label column, a dropped segment) produces a
plausible-looking number that no existing test contradicts.

Every expected value here is derived by hand rather than by running the code and recording
what it printed. A fixture whose expectation came from the implementation cannot detect the
implementation being wrong -- it only detects change. The derivations are in the docstrings so
the next reader can check them without trusting me.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


def rows(pairs, score_key="score", label_key="label"):
    return [{score_key: s, label_key: y} for s, y in pairs]


class EvaluationEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "eval.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

        # Restore the global, and release this test's engine. Placed at the END of tearDown so
        # the teardown above still runs against THIS test's storage -- an earlier version put it
        # first and silently redirected those statements at the restored global.
        #
        # The restore is the load-bearing half. Leaving app_main.storage pointed at a deleted
        # tempdir made an UNRELATED module later fail with
        # `sqlite3.OperationalError: unable to open database file`: the failure lands on whoever
        # reads the global next, never on the test that leaked it, which is why it looked like a
        # random flake for so long.
        try:
            app_main.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        try:
            app_main.storage = self._orig_storage
        except Exception:  # pragma: no cover
            pass

    def _evaluate(self, payload):
        res = self.client.post("/api/v1/evaluations", headers=self.headers, json=payload)
        self.assertLess(res.status_code, 300, res.text[:400])
        return res.json()

    def _metrics(self, body):
        return body.get("metrics") or body

    # ── binary metrics ──────────────────────────────────────────────────────

    def test_perfect_separation_scores_auc_1(self):
        """Every positive scores above every negative, so every (pos, neg) pair is concordant:
        AUC = 1, Gini = 2*1 - 1 = 1, KS = 1."""
        body = self._evaluate({
            "name": "perfect", "task": "binary",
            "rows": rows([(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)]),
        })
        m = self._metrics(body)
        self.assertAlmostEqual(m["auc"], 1.0, places=6)
        self.assertAlmostEqual(m["gini"], 1.0, places=6)
        self.assertAlmostEqual(m["ks"], 1.0, places=6)

    def test_inverted_scores_auc_0(self):
        """The same data with the score sign flipped. This is the assertion that catches a
        swapped score/label mapping: a symmetric metric would give 1.0 both times."""
        m = self._metrics(self._evaluate({
            "name": "inverted", "task": "binary",
            "rows": rows([(0.1, 1), (0.2, 1), (0.8, 0), (0.9, 0)]),
        }))
        self.assertAlmostEqual(m["auc"], 0.0, places=6)
        self.assertAlmostEqual(m["gini"], -1.0, places=6)

    def test_a_known_intermediate_auc(self):
        """Not 0 or 1 -- those can both fall out of a broken implementation by accident.

        Positives score {0.8, 0.4}; negatives {0.6, 0.2}. The four (pos, neg) pairs:
          0.8>0.6 concordant, 0.8>0.2 concordant, 0.4<0.6 discordant, 0.4>0.2 concordant.
        AUC = 3/4 = 0.75, so Gini = 2(0.75) - 1 = 0.5.
        """
        m = self._metrics(self._evaluate({
            "name": "intermediate", "task": "binary",
            "rows": rows([(0.8, 1), (0.4, 1), (0.6, 0), (0.2, 0)]),
        }))
        self.assertAlmostEqual(m["auc"], 0.75, places=6)
        self.assertAlmostEqual(m["gini"], 0.5, places=6)

    def test_ties_count_as_half_a_concordant_pair(self):
        """The convention every correct ROC implementation follows, and a common bug.

        One positive at 0.5, one negative at 0.5: the single pair is a tie, worth 0.5, so
        AUC = 0.5. An implementation using a strict > and ignoring ties returns 0.0 here.
        """
        m = self._metrics(self._evaluate({
            "name": "ties", "task": "binary", "rows": rows([(0.5, 1), (0.5, 0)]),
        }))
        self.assertAlmostEqual(m["auc"], 0.5, places=6)

    def test_custom_column_names_are_honoured(self):
        """config.score_col / label_col are the mapping layer that only an endpoint test
        exercises -- calling the metric functions directly bypasses it entirely."""
        m = self._metrics(self._evaluate({
            "name": "custom-cols", "task": "binary",
            "config": {"score_col": "pd", "label_col": "defaulted"},
            "rows": rows([(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)], "pd", "defaulted"),
        }))
        self.assertAlmostEqual(m["auc"], 1.0, places=6)

    def test_wrong_column_names_do_not_silently_score_perfectly(self):
        """If a missing column defaulted to zeros, every dataset would score identically and
        the endpoint would report confident nonsense. Either an error or a degenerate metric is
        acceptable; a confident 1.0 is not."""
        res = self.client.post("/api/v1/evaluations", headers=self.headers, json={
            "name": "bad-cols", "task": "binary",
            "config": {"score_col": "nope", "label_col": "also_nope"},
            "rows": rows([(0.9, 1), (0.1, 0)]),
        })
        if res.status_code < 300:
            auc = self._metrics(res.json()).get("auc")
            self.assertNotEqual(auc, 1.0, "a missing score column must not yield a perfect AUC")

    # ── persistence and shape ───────────────────────────────────────────────

    def test_raw_rows_are_not_persisted(self):
        """The module docstring promises "only metrics + a dataset summary are persisted --
        never the raw scored rows". That is a data-protection claim, so it gets an assertion:
        scored rows are customer data and an evaluation is not a place to accumulate it."""
        created = self._evaluate({
            "name": "no-rows-kept", "task": "binary",
            "rows": rows([(0.9, 1), (0.1, 0)]),
        })
        fetched = self.client.get(f"/api/v1/evaluations/{created['id']}", headers=self.headers).json()
        self.assertNotIn("rows", fetched)
        self.assertIn("metrics", fetched)
        summary = fetched.get("dataset_summary") or {}
        self.assertTrue(summary, "a summary must survive even though the rows do not")

    def test_an_evaluation_is_listed_after_creation(self):
        created = self._evaluate({"name": "listed", "task": "binary",
                                  "rows": rows([(0.9, 1), (0.1, 0)])})
        listing = self.client.get("/api/v1/evaluations", headers=self.headers).json()
        self.assertIn(created["id"], [e["id"] for e in listing])

    def test_an_empty_dataset_is_refused_rather_than_scored(self):
        """An AUC over zero rows is undefined; returning one would be a fabricated number."""
        res = self.client.post("/api/v1/evaluations", headers=self.headers,
                               json={"name": "empty", "task": "binary", "rows": []})
        self.assertGreaterEqual(res.status_code, 400)
        self.assertLess(res.status_code, 500)

    def test_a_single_class_dataset_does_not_report_a_confident_auc(self):
        """AUC needs both classes -- with positives only there are no (pos, neg) pairs. The
        honest answers are null or an error; 1.0 would be a fabricated success."""
        res = self.client.post("/api/v1/evaluations", headers=self.headers, json={
            "name": "one-class", "task": "binary", "rows": rows([(0.9, 1), (0.8, 1)]),
        })
        if res.status_code < 300:
            self.assertIn(self._metrics(res.json()).get("auc"), (None, 0.5),
                          "a single-class dataset cannot yield a meaningful AUC")

    def test_evaluations_are_tenant_scoped(self):
        created = self._evaluate({"name": "mine", "task": "binary",
                                  "rows": rows([(0.9, 1), (0.1, 0)])})
        other = app_main.storage.create_tenant("Other Co")
        other_key = app_main.storage.generate_api_key_for_tenant(other["id"])["plaintext"]
        listing = self.client.get("/api/v1/evaluations", headers={"x-api-key": other_key}).json()
        self.assertNotIn(created["id"], [e["id"] for e in listing])


if __name__ == "__main__":
    unittest.main()
