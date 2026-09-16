"""Sweep: the evaluations endpoint's behaviour on ORDINARY BAD DATA.

`test_sweep_eval_surface.py` covers the metric maths by importing `model_evaluation` directly.
That leaves the router's error contract untested, and the router is where the difference between
"your file has a blank cell" and "the server is broken" gets decided.

The defect this file was written for: a null score produced **HTTP 500**. `run_evaluation` does
`float(r[score_col])`, and `float(None)` raises `TypeError` -- which the handler did not catch,
because it caught only `(ValueError, KeyError)`. `float("abc")` raises ValueError and correctly
returned 422; a MISSING column raises KeyError and correctly returned 422. Only `null` -- by far
the most ordinary of the three, since it is what a spreadsheet export writes for an unscored row
-- fell through as a server fault, with no indication of which column was at fault.

Found by the evaluations sweep. The fix was one name in a tuple, but nothing was asserting the
contract, so a mutation reverting it survived.
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
from tests._storage_global import preserve_storage_global  # noqa: E402


class EvaluationBadDataTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "eval.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _post(self, rows):
        return self.client.post(
            "/api/v1/evaluations",
            headers=self.headers,
            json={"name": "sweep", "task": "binary", "rows": rows, "config": {}},
        )

    def _good_rows(self):
        return [{"score": 0.1 * (i % 10), "label": i % 2} for i in range(40)]

    def test_a_control_evaluation_succeeds(self):
        """The positive control. Without it every assertion below could pass because the
        endpoint is broken for all input, which is the shape of a security test that refuses
        everything and reports itself green."""
        response = self._post(self._good_rows())
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_null_score_is_rejected_as_bad_data_not_a_server_error(self):
        rows = self._good_rows()
        rows[3]["score"] = None
        response = self._post(rows)
        self.assertNotEqual(
            response.status_code, 500,
            "an unscored row is ORDINARY input -- a 500 tells the caller the server is broken "
            "and invites a retry that cannot succeed")
        self.assertEqual(response.status_code, 422, response.text)

    def test_a_null_label_is_rejected_the_same_way(self):
        rows = self._good_rows()
        rows[5]["label"] = None
        response = self._post(rows)
        self.assertEqual(response.status_code, 422, response.text)

    def test_a_non_numeric_score_is_still_a_422(self):
        """Guards the fix against over-correction: widening the handler must not change the
        two cases that were already right."""
        rows = self._good_rows()
        rows[2]["score"] = "abc"
        response = self._post(rows)
        self.assertEqual(response.status_code, 422, response.text)

    def test_a_missing_column_is_still_a_422(self):
        rows = self._good_rows()
        del rows[1]["score"]
        response = self._post(rows)
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
