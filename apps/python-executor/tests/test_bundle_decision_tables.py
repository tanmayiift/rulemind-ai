"""The offline SDK bundle must carry decision tables (so on-device engines can run
them), and a decision_table policy step must be an EDGE step, not server-only."""
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.compiler import compile_bundle  # noqa: E402
from app.storage import Storage  # noqa: E402


class BundleDecisionTableTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "b.db"))
        self.storage = app_main.storage
        self.tenant = self.storage.default_tenant_id
        self.storage.create_variable({
            "id": "score", "name": "Score", "category": "Custom", "source_id": "custom",
            "code": "def run(payload, context):\n    return payload.get('score', 0)\n", "status": "prod", "version": 1,
        })
        self.storage.create_decision_table({
            "id": "dt_risk", "name": "Risk", "hit_policy": "first",
            "inputs": [{"id": "in_score", "variable_id": "score", "name": "Score", "field_type": "number"}],
            "outputs": [{"id": "out_decision", "name": "Decision", "type": "outcome"}],
            "rows": [{"id": "r1", "cells": {"in_score": {"operator": ">=", "value": 700}}, "outputs": {"out_decision": "approve"}}],
            "status": "prod",
        })
        self.storage.create_policy({
            "id": "pol_dt", "name": "DT Policy", "status": "prod",
            "steps": [{"type": "decision_table", "ref_id": "dt_risk", "id": "s1", "label": "Risk"}],
        })

    def tearDown(self):
        self.tempdir.cleanup()

    def test_bundle_includes_decision_tables(self):
        bundle = compile_bundle(self.storage, self.tenant, force=True)
        tables = bundle["content"]["decisionTables"]
        self.assertTrue(any(t["id"] == "dt_risk" for t in tables))
        table = next(t for t in tables if t["id"] == "dt_risk")
        self.assertEqual(table["hit_policy"], "first")
        self.assertTrue(table["rows"])

    def test_decision_table_step_is_an_edge_step(self):
        bundle = compile_bundle(self.storage, self.tenant, force=True)
        policy = next(p for p in bundle["content"]["policies"] if p["id"] == "pol_dt")
        self.assertTrue(any(s.get("type") == "decision_table" for s in policy["steps"]))
        self.assertNotIn("s1", policy["serverOnlySteps"])  # runnable on-device, not skipped


if __name__ == "__main__":
    unittest.main()
