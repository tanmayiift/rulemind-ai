"""The two /decide payload forms have DISTINCT, intentional semantics — document + lock them in.

A reviewer flagged that flat and nested payloads can decide differently for the "same" fields. That
is by design, not a bug (the seeded SDK-manifest scenarios rely on it):

  * FLAT   {"bureau_score": 800}           -> per-field OVERRIDE: set bureau_score on each source's
                                             sample, keep every other sample field.
  * NESTED {"loan": {"bureau_score": 800}} -> the loan source payload IS exactly {"bureau_score": 800}
                                             — every other loan field is absent (an authoritative,
                                             complete source payload).

So a PARTIAL flat and a PARTIAL nested of the same field can differ; a COMPLETE nested payload matches
the equivalent flat. These tests pin that contract so neither form silently drifts.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402

POLICY = "policy_instant_personal_loan"


class DecidePayloadFormsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "pf.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}
        # The full loan sample (the source's declared field set) — used to build a COMPLETE nested form.
        schema = self.client.get(f"/api/v1/policies/{POLICY}/input-schema", headers=self.headers).json()
        loan = next(s for s in schema["sources"] if s["source_id"] == "loan")
        self.loan_sample = {f["name"]: f["sample"] for f in loan["fields"]}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _decide(self, payload):
        r = self.client.post("/api/v1/decide", json={"policy_id": POLICY, "payload": payload}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        return d["outcome"], d.get("score")

    def test_flat_override_keeps_sample_defaults(self):
        # A single strong flat field; the other loan fields keep their (approving) sample -> approve.
        outcome, _ = self._decide({"bureau_score": 800, "dti_ratio": 0.12})
        self.assertEqual(outcome, "approve")

    def test_complete_nested_matches_equivalent_flat(self):
        # When the nested payload carries the WHOLE source (all fields), it is equivalent to flat.
        fields = {**self.loan_sample, "bureau_score": 800, "dti_ratio": 0.12}
        flat = self._decide(dict(fields))
        nested = self._decide({"loan": dict(fields)})
        self.assertEqual(flat, nested, "a complete nested payload must equal the equivalent flat input")

    def test_partial_nested_is_authoritative_not_an_override(self):
        # A PARTIAL nested payload replaces the source: unspecified fields are absent, not defaulted —
        # the documented contract. A complete strong payload approves; dropping fields must change the
        # decision (i.e. absent != silently reuse the sample).
        strong = {**self.loan_sample, "bureau_score": 820, "dti_ratio": 0.1}
        complete = self._decide({"loan": dict(strong)})
        self.assertEqual(complete[0], "approve")
        partial = self._decide({"loan": {"bureau_score": 820, "dti_ratio": 0.1}})
        self.assertNotEqual(partial, complete, "partial nested must be authoritative (absent != sample)")


if __name__ == "__main__":
    unittest.main()
