"""Workflow loops + loop-debug — unit, smoke, regression.

* unit        — the loop executor: list iteration, map-object iteration, cap,
                nested-scope save/restore, non-iterable no-op, literal items
* smoke       — a loop step inside a policy via /decide, and the /loop-debug endpoint
* regression  — empty collection, cumulative outcome merge across iterations

Loops iterate over a computed variable that returns a collection
(`over: "variables.<id>"`) — the intended abstraction over the raw payload.
"""
from __future__ import annotations

import asyncio
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
os.environ.setdefault("RULEMIND_ADMIN_JWT_SECRET", "rulemind-test-admin-secret")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402
from app.storage import Storage  # noqa: E402


# A branch that rejects low scores, approves otherwise — keyed on the loop item.
def _score_branch(item_name="app"):
    return {"type": "branch", "config": {
        "branches": [{"condition": f"{item_name}['score'] < 600", "steps": [{"type": "outcome", "ref_id": "reject"}]}],
        "default": [{"type": "outcome", "ref_id": "approve"}],
    }}


class LoopExecutorUnitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "loop.db"))
        self.executor = PolicyExecutor(app_main.storage)
        self.tenant = app_main.storage.default_tenant_id or "default"

    def tearDown(self):
        self.tempdir.cleanup()

    def _debug(self, loop_step, variables):
        return asyncio.run(self.executor.debug_loop(loop_step, {}, self.tenant, variable_values=variables))

    def test_iterates_over_a_list(self):
        step = {"type": "loop", "config": {"over": "variables.applicants", "as": "app", "steps": [_score_branch()]}}
        res = self._debug(step, {"applicants": [{"score": 800}, {"score": 500}, {"score": 700}]})
        self.assertEqual(res["count"], 3)
        self.assertEqual(res["iterations_run"], 3)
        # cumulative merge: approve -> reject -> reject (reject dominates)
        self.assertEqual([it["outcome_after"] for it in res["iterations"]], ["approve", "reject", "reject"])

    def test_map_object_iterates_as_key_value(self):
        step = {"type": "loop", "config": {"over": "variables.limits", "as": "row",
                "steps": [{"type": "outcome", "ref_id": "approve"}]}}
        res = self._debug(step, {"limits": {"a": 1, "b": 2}})
        self.assertEqual(res["count"], 2)
        items = [it["item"] for it in res["iterations"]]
        self.assertIn({"key": "a", "value": 1}, items)

    def test_max_iterations_caps_and_flags_truncation(self):
        step = {"type": "loop", "config": {"over": "variables.xs", "as": "x", "maxIterations": 5,
                "steps": [{"type": "outcome", "ref_id": "approve"}]}}
        res = self._debug(step, {"xs": list(range(20))})
        self.assertEqual(res["iterations_run"], 5)
        self.assertTrue(res["truncated"])

    def test_non_iterable_is_a_noop(self):
        step = {"type": "loop", "config": {"over": "variables.missing", "as": "x", "steps": [{"type": "outcome", "ref_id": "approve"}]}}
        res = self._debug(step, {"applicants": []})
        self.assertEqual(res["iterations_run"], 0)

    def test_nested_loop_scope_is_restored(self):
        inner = {"type": "loop", "config": {"over": "outer.vals", "as": "inner", "indexAs": "j",
                 "steps": [{"type": "outcome", "ref_id": "approve"}]}}
        outer = {"type": "loop", "config": {"over": "variables.groups", "as": "outer", "indexAs": "i", "steps": [inner]}}
        res = self._debug(outer, {"groups": [{"vals": [1, 2]}, {"vals": [3]}]})
        self.assertEqual(res["count"], 2)
        # each outer iteration ran an inner loop step (captured in its steps trace)
        self.assertTrue(all(any(s.get("step", {}).get("type") == "loop" for s in it["steps"]) for it in res["iterations"]))

    def test_literal_items_list(self):
        step = {"type": "loop", "config": {"items": [{"score": 900}, {"score": 100}], "as": "app", "steps": [_score_branch()]}}
        res = self._debug(step, {})
        self.assertEqual([it["outcome_after"] for it in res["iterations"]], ["approve", "reject"])


class LoopApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "loopapi.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _make_applicants_variable(self):
        app_main.storage.create_variable({
            "id": "applicants", "name": "Applicants", "category": "Custom", "source_id": "custom",
            "code": "def run(payload, context):\n    return payload.get('applicants', [])\n",
            "status": "dev", "version": 1,
        })

    # ---- smoke: /loop-debug endpoint (variables provided verbatim) ----
    def test_loop_debug_endpoint(self):
        body = {
            "over": "variables.applicants", "as": "app",
            "steps": [_score_branch()],
            "variable_values": {"applicants": [{"score": 800}, {"score": 500}]},
        }
        r = self.client.post("/api/v1/workflows/loop-debug", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["iterations_run"], 2)
        self.assertEqual(data["outcome"], "reject")

    # ---- smoke: loop runs inside a policy via /decide ----
    def test_loop_inside_policy(self):
        self._make_applicants_variable()
        pol = self.client.post("/api/v1/policies", json={
            "name": "Batch applicants",
            "steps": [{"type": "loop", "id": "lp", "config": {"over": "variables.applicants", "as": "app", "steps": [_score_branch()]}}],
        }, headers=self.headers)
        self.assertEqual(pol.status_code, 200, pol.text)
        pid = pol.json()["id"]
        res = self.client.post("/api/v1/decide", json={"policy_id": pid, "payload": {"applicants": [{"score": 900}, {"score": 400}]}}, headers=self.headers).json()
        self.assertEqual(res["outcome"], "reject")
        self.assertTrue(any(e.get("step", {}).get("type") == "loop" for e in res["trace"]))

    # ---- regression ----
    def test_empty_collection_is_safe(self):
        body = {"over": "variables.applicants", "as": "app", "steps": [_score_branch()], "variable_values": {"applicants": []}}
        r = self.client.post("/api/v1/workflows/loop-debug", json=body, headers=self.headers).json()
        self.assertEqual(r["iterations_run"], 0)
        self.assertEqual(r["outcome"], "pending")

    def test_loop_step_accepted_by_policy_validation(self):
        # a loop step must not be rejected by the step-type whitelist
        r = self.client.post("/api/v1/policies", json={
            "name": "Loopy", "steps": [{"type": "loop", "config": {"over": "variables.xs", "steps": []}}],
        }, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
