"""The two /decide payload forms have DISTINCT, intentional semantics — document + lock them in.

A reviewer flagged that flat and nested payloads can decide differently for the "same" fields. That
is by design, not a bug (the seeded SDK-manifest scenarios rely on it):

  * FLAT   {"bureau_score": 800}           -> per-field OVERRIDE: set bureau_score on each source
                                             that declares it. Whether the source's OTHER fields
                                             keep their sample values depends on the surface --
                                             see below; on /decide they do not.
  * NESTED {"loan": {"bureau_score": 800}} -> the loan source payload IS exactly {"bureau_score": 800}
                                             — every other loan field is absent (an authoritative,
                                             complete source payload).

So a PARTIAL flat and a PARTIAL nested of the same field can differ; a COMPLETE nested payload matches
the equivalent flat. These tests pin that contract so neither form silently drifts.

ONE PART OF THAT CONTRACT WAS A PRODUCTION HAZARD AND HAS CHANGED. Sample pre-fill used to apply on
every surface, so `POST /decide {"payload": {}}` returned **approve** -- on the demo connector's
bureau_score of 756, with a complete and legitimate-looking audit trail. An integration that
silently sent nothing got a confident approval. Pre-fill is an authoring affordance and now runs
only on the test console, simulation and loop-debug (executor._SAMPLE_SEEDING_SOURCES); on
/decide an unsupplied field is unsupplied and the decision fails closed.

This file previously asserted the opposite as intentional, which is why the hazard survived.
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

    def _test_console(self, payload):
        r = self.client.post("/api/v1/test/policy/" + POLICY, json={"payload": payload},
                             headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        return body.get("outcome") or (body.get("result") or {}).get("outcome")

    def test_decide_does_not_inherit_sample_defaults(self):
        """Production traffic must not be scored against a connector's demo sample.

        The old assertion here was `assertEqual(outcome, "approve")` on a two-field payload,
        which passed only because the seventeen fields the caller never sent were quietly
        filled from the demo connector. An empty payload approved for exactly the same reason.
        """
        empty, _ = self._decide({})
        self.assertNotEqual(empty, "approve",
                            "an empty payload approved -- /decide is scoring demo sample data")
        # And a partial payload is genuinely partial: the unsent fields do not arrive from the
        # sample, so it cannot land on the same decision as the complete one.
        partial, _ = self._decide({"bureau_score": 800, "dti_ratio": 0.12})
        complete, _ = self._decide({**self.loan_sample, "bureau_score": 800, "dti_ratio": 0.12})
        self.assertEqual(complete, "approve", "the complete strong payload must still approve")
        self.assertNotEqual(partial, complete)

    def test_test_console_still_pre_fills_from_the_sample(self):
        """The authoring half of the same change: the console is where pre-fill belongs.

        An author types one field and expects the policy to run end to end. Removing pre-fill
        everywhere would have made the console useless, so the fix is scoped by source rather
        than deleted outright -- and that scoping needs a test on both sides of the line.
        """
        self.assertEqual(self._test_console({"bureau_score": 800, "dti_ratio": 0.12}), "approve")

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
