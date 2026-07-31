"""P1-D: decision tables on the scalable fast path + concurrent-map loops.

* Fast path — the pure core (`app.core.engine`) and the cached-bundle server
  (`app.fast_decide`) now evaluate `decision_table` steps, so a table-driven policy
  is served on the hot path instead of silently skipped.
* Parallel loops — a loop with `mode: "parallel"` runs its iterations concurrently on
  isolated cloned contexts (the latency lever for I/O-bound loops) and produces the
  same per-item results as the sequential loop.
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
os.environ.setdefault("RULEMIND_SEED_DEMO", "1")
os.environ.pop("AUTH_MODE", None)

import app.fast_decide as fast_decide  # noqa: E402
import app.main as app_main  # noqa: E402
from app.core.engine import decide as core_decide  # noqa: E402
from app.executor import PolicyExecutor  # noqa: E402
from app.storage import Storage  # noqa: E402


def _table():
    return {
        "hit_policy": "first",
        "inputs": [{"id": "in_score", "variable_id": "score", "name": "Score", "field_type": "number"}],
        "outputs": [{"id": "out_decision", "name": "Decision", "type": "outcome"}],
        "rows": [
            {"id": "r1", "cells": {"in_score": {"operator": ">=", "value": 750}}, "outputs": {"out_decision": "approve"}},
            {"id": "r2", "cells": {"in_score": {"operator": "between", "value": 600, "value2": 749}}, "outputs": {"out_decision": "review"}},
            {"id": "r3", "cells": {"in_score": {"operator": "<", "value": 600}}, "outputs": {"out_decision": "reject"}},
        ],
    }


class DecisionTableFastPathTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "fp.db"))
        self.storage = app_main.storage
        self.tenant = self.storage.default_tenant_id
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": self.storage.default_api_key or ""}
        self.storage.create_variable({
            "id": "score", "name": "Score", "category": "Custom", "source_id": "custom",
            "code": "def run(payload, context):\n    return payload.get('score', 0)\n", "status": "dev", "version": 1,
        })
        tid = self.client.post("/api/v1/decision-tables", json={"name": "Risk", **_table()}, headers=self.headers).json()["id"]
        self.tid = tid
        self.policy = self.client.post("/api/v1/policies", json={
            "name": "DT Policy", "steps": [{"type": "decision_table", "ref_id": tid, "label": "Risk"}],
        }, headers=self.headers).json()
        fast_decide.invalidate()

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def test_policy_is_fast_servable(self):
        # A pure table policy has no I/O steps, so it belongs on the fast path.
        self.assertTrue(fast_decide.is_fast_servable(self.policy))

    def test_core_evaluates_decision_table(self):
        bundle = {"policy": self.policy, "rules": {}, "scorecards": {},
                  "decision_tables": {self.tid: {"id": self.tid, **_table()}},
                  "variables": [{"id": "score", "source_id": "custom"}]}
        out = core_decide(bundle, {"score": 800}, {"variables": {"score": 800}})
        self.assertEqual(out["outcome"], "approve")
        out2 = core_decide(bundle, {"score": 400}, {"variables": {"score": 400}})
        self.assertEqual(out2["outcome"], "reject")

    def test_fast_decide_serves_the_table(self):
        approve = fast_decide.fast_decide(self.storage, self.policy, {"score": 800}, self.tenant, log=False)
        self.assertEqual(approve["outcome"], "approve")
        review = fast_decide.fast_decide(self.storage, self.policy, {"score": 700}, self.tenant, log=False)
        self.assertEqual(review["outcome"], "review")
        reject = fast_decide.fast_decide(self.storage, self.policy, {"score": 400}, self.tenant, log=False)
        self.assertEqual(reject["outcome"], "reject")

    def test_fast_and_heavy_paths_agree(self):
        # Parity: the fast path must produce the same outcome the heavy executor does
        # (previously the fast path silently skipped the table -> defaulted to review).
        for score, expected in [(800, "approve"), (700, "review"), (400, "reject")]:
            heavy = self.client.post("/api/v1/decide",
                                     json={"policy_id": self.policy["id"], "payload": {"score": score}},
                                     headers=self.headers).json()
            fast = fast_decide.fast_decide(self.storage, self.policy, {"score": score}, self.tenant, log=False)
            self.assertEqual(heavy["outcome"], expected)
            self.assertEqual(fast["outcome"], expected, f"score={score}")


def _score_branch(item_name="app"):
    return {"type": "branch", "config": {
        "branches": [{"condition": f"{item_name}['score'] < 600", "steps": [{"type": "outcome", "ref_id": "reject"}]}],
        "default": [{"type": "outcome", "ref_id": "approve"}],
    }}


class ParallelLoopTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "loop.db"))
        self.tenant = app_main.storage.default_tenant_id
        self.executor = PolicyExecutor(app_main.storage)

    def tearDown(self):
        self.tempdir.cleanup()

    def _debug(self, loop_step, variables):
        return asyncio.run(self.executor.debug_loop(loop_step, {}, self.tenant, variable_values=variables))

    def test_parallel_matches_sequential(self):
        applicants = [{"score": 800}, {"score": 500}, {"score": 650}, {"score": 400}, {"score": 900}]
        seq = self._debug({"type": "loop", "config": {"over": "variables.applicants", "as": "app", "steps": [_score_branch()]}},
                          {"applicants": applicants})
        par = self._debug({"type": "loop", "config": {"over": "variables.applicants", "as": "app", "mode": "parallel",
                          "concurrency": 3, "steps": [_score_branch()]}}, {"applicants": applicants})
        self.assertEqual(par["mode"], "parallel")
        self.assertEqual(par["iterations_run"], len(applicants))
        # Same per-item outcomes, in the same index order.
        self.assertEqual([it["outcome_after"] for it in par["iterations"]],
                         [it["outcome_after"] for it in seq["iterations"]])
        # Cumulative outcome merge (parity with sequential): once reject appears its
        # precedence sticks, so later approves don't lower the running outcome.
        self.assertEqual([it["outcome_after"] for it in par["iterations"]],
                         ["approve", "reject", "reject", "reject", "reject"])

    def test_parallel_preserves_index_order(self):
        applicants = [{"score": s} for s in range(700, 500, -20)]  # 10 items
        par = self._debug({"type": "loop", "config": {"over": "variables.applicants", "as": "app", "mode": "parallel",
                          "concurrency": 5, "steps": [_score_branch()]}}, {"applicants": applicants})
        self.assertEqual([it["index"] for it in par["iterations"]], list(range(len(applicants))))

    def test_parallel_runs_concurrently(self):
        # A sub-step that awaits ~40ms each: N items across a wide pool must finish in
        # far less than N*40ms if they truly overlap.
        async def _timed():
            async def sleep_step(step, ctx, *a, **k):
                await asyncio.sleep(0.04)
                ctx.outcome = "approve"
                return {"slept": True}, None
            self.executor._execute_step_body = sleep_step  # type: ignore[assignment]
            items = [{"i": i} for i in range(10)]
            loop = {"type": "loop", "config": {"over": "variables.items", "as": "it", "mode": "parallel",
                    "concurrency": 10, "steps": [{"type": "action", "id": "s"}]}}
            import time as _t
            start = _t.perf_counter()
            await self.executor.debug_loop(loop, {}, self.tenant, variable_values={"items": items})
            return _t.perf_counter() - start
        elapsed = asyncio.run(_timed())
        self.assertLess(elapsed, 0.25)  # 10 * 0.04 = 0.4s sequential; concurrent is well under


if __name__ == "__main__":
    unittest.main()
