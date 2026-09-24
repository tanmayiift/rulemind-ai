"""An integration written against the old `rule_results` wrapper keeps working until its sunset.

Plan 5.1 made `rule_results` one shape on every decision path -- the rule result itself. Before
that the executor path (the default) wrapped each item in its trace entry, so an integrator may
be reading `rule_results[i].result.passed` or `rule_results[i].step.id`. Those fields stay on
/decide responses, on BOTH paths, until `LEGACY_RULE_FIELDS_SUNSET`, and this file fails the day
that passes with them still present -- a deprecation without an enforced end is permanent.
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app.routers import runtime  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402

LEGACY = {"step", "result", "duration_ms", "error"}


class LegacyRuleFieldTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "legacy.db"))
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def _decide(self, fast="0"):
        with mock.patch.dict(os.environ, {"FAST_DECIDE": fast}):
            body = self.client.post("/api/v1/decide", headers=self.headers,
                                    json={"policy_id": "policy_pl_underwriting", "payload": {}}).json()
        self.assertTrue(body["rule_results"], "no rules ran, so this would check nothing")
        return body

    def test_an_old_reader_still_works_on_both_paths(self):
        for flag in ("0", "1"):
            with self.subTest(fast_decide=flag):
                for item in self._decide(flag)["rule_results"]:
                    self.assertEqual(item["result"]["passed"], item["passed"])
                    self.assertEqual(item["result"]["conditions"], item["conditions"])
                    self.assertEqual(item["step"]["type"], "rule")
                    self.assertEqual(item["step"]["ref_id"], item["rule_id"])

    def test_the_stored_decision_stays_canonical(self):
        body = self._decide()
        stored = app_main.storage.get_decision(body["decision_id"], tenant_id=app_main.storage.default_tenant_id)
        self.assertEqual(LEGACY & set().union(*(set(item) for item in stored["rule_results"])), set())

    def test_a_rule_run_by_two_steps_maps_each_result_to_its_own_step(self):
        trace = [{"step": {"id": "s1", "type": "rule", "ref_id": "r"}, "duration_ms": 1, "error": None},
                 {"step": {"id": "s2", "type": "rule", "ref_id": "r"}, "duration_ms": 2, "error": None}]
        results = [{"rule_id": "r", "passed": True}, {"rule_id": "r", "passed": False}]
        legacy = runtime._with_legacy_rule_fields(results, trace)
        self.assertEqual([item["step"]["id"] for item in legacy], ["s1", "s2"])
        self.assertEqual([item["duration_ms"] for item in legacy], [1, 2])
        self.assertNotIn("step", results[0], "the originals were mutated; the decision log may still hold them")

    def test_the_spec_marks_them_deprecated_with_the_date(self):
        properties = app_main.app.openapi()["components"]["schemas"]["RuleResult"]["properties"]
        for name in LEGACY:
            with self.subTest(field=name):
                self.assertTrue(properties[name].get("deprecated"))
                self.assertIn(runtime.LEGACY_RULE_FIELDS_SUNSET, properties[name]["description"])

    def test_the_fields_are_gone_by_their_sunset(self):
        if datetime.date.today() >= datetime.date.fromisoformat(runtime.LEGACY_RULE_FIELDS_SUNSET):
            self.fail("LEGACY_RULE_FIELDS_SUNSET has passed: remove _with_legacy_rule_fields, the four "
                      "deprecated RuleResult fields, this file, and the API.md note")


if __name__ == "__main__":
    unittest.main()
