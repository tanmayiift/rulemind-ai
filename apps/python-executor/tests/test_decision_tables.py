"""Decision tables + optimiser — unit, smoke, and regression coverage.

* unit         — the pure evaluator + optimiser (`app.decision_tables`)
* smoke        — full CRUD + analyze + evaluate through the HTTP API, and a
                 decision-table step running inside a policy via /decide
* regression   — version bump on update, tenant isolation, hit-policy semantics
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")  # tests use the sample lending inventory
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.decision_tables import analyze_decision_table, evaluate_decision_table  # noqa: E402
from app.storage import Storage  # noqa: E402


# A small risk-tiering table: score band -> outcome. Two inputs to exercise AND.
def _table(hit_policy="first", rows=None, default_row=None):
    return {
        "hit_policy": hit_policy,
        "inputs": [
            {"id": "in_score", "variable_id": "score", "name": "Score", "field_type": "number"},
            {"id": "in_flag", "variable_id": "fraud_flag", "name": "Fraud", "field_type": "boolean"},
        ],
        "outputs": [{"id": "out_decision", "name": "Decision", "type": "outcome"}],
        "rows": rows if rows is not None else [
            {"id": "r1", "cells": {"in_score": {"operator": ">=", "value": 750}, "in_flag": {"operator": "==", "value": "false"}}, "outputs": {"out_decision": "approve"}},
            {"id": "r2", "cells": {"in_score": {"operator": "between", "value": 600, "value2": 749}, "in_flag": {"operator": "==", "value": "false"}}, "outputs": {"out_decision": "review"}},
            {"id": "r3", "cells": {"in_score": {"operator": "<", "value": 600}}, "outputs": {"out_decision": "reject"}},
        ],
        "default_row": default_row,
    }


class DecisionTableUnitTests(unittest.TestCase):
    # ---- evaluation ----
    def test_first_match_wins(self):
        res = evaluate_decision_table(_table(), {"score": 800, "fraud_flag": False})
        self.assertEqual(res["outcome"], "approve")
        self.assertEqual(res["winning_row_id"], "r1")

    def test_between_is_inclusive(self):
        self.assertEqual(evaluate_decision_table(_table(), {"score": 600, "fraud_flag": False})["outcome"], "review")
        self.assertEqual(evaluate_decision_table(_table(), {"score": 749, "fraud_flag": False})["outcome"], "review")

    def test_wildcard_cell_always_matches(self):
        # r3 has no fraud cell -> wildcard; a low score rejects regardless of the flag
        self.assertEqual(evaluate_decision_table(_table(), {"score": 500, "fraud_flag": True})["outcome"], "reject")

    def test_no_match_uses_default(self):
        t = _table(rows=[{"id": "r1", "cells": {"in_score": {"operator": ">=", "value": 900}}, "outputs": {"out_decision": "approve"}}],
                   default_row={"outputs": {"out_decision": "review"}})
        res = evaluate_decision_table(t, {"score": 100})
        self.assertEqual(res["outcome"], "review")
        self.assertEqual(res["winning_row_id"], "default")

    def test_collect_hit_policy_returns_lists(self):
        t = _table(hit_policy="collect", rows=[
            {"id": "a", "cells": {"in_score": {"operator": ">=", "value": 100}}, "outputs": {"out_decision": "approve"}},
            {"id": "b", "cells": {"in_score": {"operator": ">=", "value": 200}}, "outputs": {"out_decision": "review"}},
        ])
        res = evaluate_decision_table(t, {"score": 500})
        self.assertEqual(res["outputs"]["out_decision"], ["approve", "review"])

    def test_priority_hit_policy_picks_highest(self):
        t = _table(hit_policy="priority", rows=[
            {"id": "a", "priority": 1, "cells": {"in_score": {"operator": ">=", "value": 100}}, "outputs": {"out_decision": "approve"}},
            {"id": "b", "priority": 5, "cells": {"in_score": {"operator": ">=", "value": 100}}, "outputs": {"out_decision": "reject"}},
        ])
        res = evaluate_decision_table(t, {"score": 500})
        self.assertEqual(res["winning_row_id"], "b")

    def test_unique_flags_ambiguous(self):
        t = _table(hit_policy="unique", rows=[
            {"id": "a", "cells": {"in_score": {"operator": ">=", "value": 100}}, "outputs": {"out_decision": "approve"}},
            {"id": "b", "cells": {"in_score": {"operator": ">=", "value": 100}}, "outputs": {"out_decision": "reject"}},
        ])
        self.assertTrue(evaluate_decision_table(t, {"score": 500})["ambiguous"])

    # ---- optimiser ----
    def test_invalid_between_without_upper_bound(self):
        t = _table(rows=[{"id": "r", "cells": {"in_score": {"operator": "between", "value": 600}}, "outputs": {"out_decision": "review"}}])
        a = analyze_decision_table(t)
        self.assertTrue(a["hasInvalidValues"])
        self.assertTrue(any(d["type"] == "invalid" and "upper bound" in d["description"] for d in a["diagnostics"]))

    def test_invalid_non_numeric_on_number_field(self):
        t = _table(rows=[{"id": "r", "cells": {"in_score": {"operator": ">=", "value": "high"}}, "outputs": {"out_decision": "approve"}}])
        self.assertTrue(analyze_decision_table(t)["hasInvalidValues"])

    def test_invalid_outcome_value_is_flagged(self):
        t = _table(rows=[{"id": "r", "cells": {"in_score": {"operator": ">=", "value": 1}}, "outputs": {"out_decision": "maybe"}}])
        a = analyze_decision_table(t)
        self.assertTrue(any(d["type"] == "invalid" and "maybe" in d["description"] for d in a["diagnostics"]))

    def test_conflict_error_under_unique_but_info_under_first(self):
        overlap = [
            {"id": "a", "cells": {"in_score": {"operator": ">=", "value": 700}}, "outputs": {"out_decision": "approve"}},
            {"id": "b", "cells": {"in_score": {"operator": ">=", "value": 650}}, "outputs": {"out_decision": "review"}},
        ]
        self.assertTrue(analyze_decision_table(_table(hit_policy="unique", rows=overlap))["hasConflicts"])
        self.assertFalse(analyze_decision_table(_table(hit_policy="first", rows=overlap))["hasConflicts"])

    def test_unreachable_row_detected(self):
        # r_specific is fully inside r_broad under first-match -> unreachable
        t = _table(rows=[
            {"id": "r_broad", "cells": {"in_score": {"operator": ">=", "value": 600}}, "outputs": {"out_decision": "approve"}},
            {"id": "r_specific", "cells": {"in_score": {"operator": ">=", "value": 700}}, "outputs": {"out_decision": "review"}},
        ])
        a = analyze_decision_table(t)
        self.assertTrue(a["hasUnreachableRows"])
        self.assertTrue(any(d.get("shadowedBy") == "r_broad" for d in a["diagnostics"] if d["type"] == "unreachable"))

    def test_clean_table_is_ok(self):
        # score bands cover (-inf,600),[600,749],[750,inf) with a default -> no errors
        self.assertTrue(analyze_decision_table(_table(default_row={"outputs": {"out_decision": "review"}}))["ok"])

    def test_opaque_row_flagged_as_not_analyzable(self):
        # A regex cell can't be reasoned about for reachability — surfaced (info), not
        # silently treated as safe, and never a blocking error.
        t = _table(rows=[
            {"id": "r_regex", "cells": {"in_flag": {"operator": "regex", "value": "^A.*"}}, "outputs": {"out_decision": "review"}},
            {"id": "r_num", "cells": {"in_score": {"operator": ">=", "value": 700}}, "outputs": {"out_decision": "approve"}},
        ], default_row={"outputs": {"out_decision": "review"}})
        a = analyze_decision_table(t)
        self.assertTrue(a["hasUnanalyzableRows"])
        note = next(d for d in a["diagnostics"] if d["type"] == "not_analyzable")
        self.assertEqual(note["severity"], "info")
        self.assertIn("r_regex", note["rows"])
        self.assertTrue(a["ok"])  # info-only, not a blocking error

    def test_plain_numeric_table_has_no_unanalyzable_note(self):
        self.assertFalse(analyze_decision_table(_table())["hasUnanalyzableRows"])


class DecisionTableApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "dt.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _create(self, **overrides):
        body = {"name": "Risk Tiers", **_table()}
        body.pop("default_row", None)
        body.update(overrides)
        r = self.client.post("/api/v1/decision-tables", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ---- smoke: CRUD + analyze + evaluate ----
    def test_crud_lifecycle(self):
        created = self._create()
        tid = created["id"]
        self.assertIn("analysis", created)
        self.assertEqual(self.client.get("/api/v1/decision-tables", headers=self.headers).json()[0]["id"], tid)
        got = self.client.get(f"/api/v1/decision-tables/{tid}", headers=self.headers)
        self.assertEqual(got.status_code, 200)
        deleted = self.client.delete(f"/api/v1/decision-tables/{tid}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/decision-tables/{tid}", headers=self.headers).status_code, 404)

    def test_evaluate_endpoint(self):
        tid = self._create()["id"]
        r = self.client.post(f"/api/v1/decision-tables/{tid}/evaluate",
                             json={"variable_values": {"score": 800, "fraud_flag": False}}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["outcome"], "approve")

    def test_analyze_draft_endpoint(self):
        draft = _table(hit_policy="unique", rows=[
            {"id": "a", "cells": {"in_score": {"operator": ">=", "value": 700}}, "outputs": {"out_decision": "approve"}},
            {"id": "b", "cells": {"in_score": {"operator": ">=", "value": 650}}, "outputs": {"out_decision": "review"}},
        ])
        r = self.client.post("/api/v1/decision-tables/analyze", json=draft, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["hasConflicts"])

    # ---- smoke: decision table runs as a policy step (/decide) ----
    def test_decision_table_runs_inside_a_policy(self):
        app_main.storage.create_variable({
            "id": "score", "name": "Score", "category": "Custom", "source_id": "custom",
            "code": "def run(payload, context):\n    return payload.get('score', 0)\n",
            "status": "dev", "version": 1,
        })
        app_main.storage.create_variable({
            "id": "fraud_flag", "name": "Fraud", "category": "Custom", "source_id": "custom",
            "code": "def run(payload, context):\n    return payload.get('fraud_flag', False)\n",
            "status": "dev", "version": 1,
        })
        tid = self._create()["id"]
        pol = self.client.post("/api/v1/policies", json={
            "name": "DT Policy",
            "steps": [{"type": "decision_table", "ref_id": tid, "label": "Risk"}],
        }, headers=self.headers)
        self.assertEqual(pol.status_code, 200, pol.text)
        pid = pol.json()["id"]
        res = self.client.post("/api/v1/decide", json={"policy_id": pid, "payload": {"score": 800, "fraud_flag": False}}, headers=self.headers).json()
        self.assertEqual(res["outcome"], "approve")
        res2 = self.client.post("/api/v1/decide", json={"policy_id": pid, "payload": {"score": 400}}, headers=self.headers).json()
        self.assertEqual(res2["outcome"], "reject")

    # ---- regression ----
    def test_update_bumps_version(self):
        tid = self._create()["id"]
        v1 = self.client.get(f"/api/v1/decision-tables/{tid}", headers=self.headers).json()["version"]
        body = {"name": "Risk Tiers", **_table()}
        body.pop("default_row", None)
        body["description"] = "changed"
        updated = self.client.put(f"/api/v1/decision-tables/{tid}", json=body, headers=self.headers).json()
        self.assertGreater(updated["version"], v1)
        self.assertEqual(updated["description"], "changed")

    def test_tenant_isolation(self):
        tid = self._create()["id"]
        other = app_main.storage.create_tenant("Other Co", plan="starter")
        other_key = app_main.storage.generate_api_key_for_tenant(other["id"])["plaintext"]
        listing = self.client.get("/api/v1/decision-tables", headers={"x-api-key": other_key}).json()
        self.assertEqual(listing, [])
        self.assertEqual(self.client.get(f"/api/v1/decision-tables/{tid}", headers={"x-api-key": other_key}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
