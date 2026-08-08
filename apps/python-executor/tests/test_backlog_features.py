"""Backlog features: shadow/dark-launch (#81), what-if KPI sim (#82), release snapshots +
rollback (#83), CRDT collab + time-travel (#87). Exercises the real product code paths."""
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

from app import crdt, release, shadow, whatif  # noqa: E402
from app.storage import Storage  # noqa: E402


def _outcome_policy(pid: str, outcome: str) -> dict:
    # The engine reads an outcome step from config.outcome / ref_id / label (not a top-level key).
    return {"id": pid, "name": pid, "defaultOutcome": "review",
            "steps": [{"id": "s", "type": "outcome", "ref_id": outcome,
                       "config": {"outcome": outcome}, "label": outcome}]}


def _save_bundle(st, tn, policy):
    st.save_bundle({"version": 1, "content": {"policy": policy, "rules": {}},
                    "encrypted_content": "x", "signature": "x", "checksum": "x"}, tenant_id=tn)


# --------------------------------------------------------------------------- #
class CrdtTests(unittest.TestCase):
    """#87 — conflict-free merge + time-travel. Pure logic, no DB."""

    def test_concurrent_edits_to_different_fields_both_survive(self):
        base = {"name": "Loan Policy", "threshold": 700, "outcome": "approve"}
        merged, conflicts = crdt.three_way_merge(
            base, {"name": "Loan Policy v2"}, {"threshold": 750}, ts_a=1, actor_a="alice", ts_b=1, actor_b="bob")
        self.assertEqual(merged["name"], "Loan Policy v2")   # alice's edit
        self.assertEqual(merged["threshold"], 750)           # bob's edit
        self.assertEqual(merged["outcome"], "approve")       # untouched
        self.assertEqual(conflicts, [])

    def test_same_field_conflict_resolves_by_lww_and_is_reported(self):
        base = {"threshold": 700}
        # Both edit threshold; bob has the higher timestamp -> bob wins, conflict reported.
        merged, conflicts = crdt.three_way_merge(
            base, {"threshold": 720}, {"threshold": 750}, ts_a=1, actor_a="alice", ts_b=2, actor_b="bob")
        self.assertEqual(merged["threshold"], 750)
        self.assertEqual(conflicts, ["threshold"])

    def test_merge_is_commutative_and_idempotent(self):
        a = crdt.to_crdt({"x": 1, "y": 2}, ts=1, actor="a")
        b = crdt.apply_edit(a, {"y": 9}, ts=2, actor="b")
        self.assertEqual(crdt.merge(a, b), crdt.merge(b, a))          # commutative
        self.assertEqual(crdt.merge(b, b), b)                          # idempotent
        self.assertEqual(crdt.to_plain(crdt.merge(a, b))["y"], 9)

    def test_time_travel_reads_and_diffs_past_versions(self):
        history = [
            {"version": 1, "doc": {"threshold": 700}, "actor": "a", "ts": 1},
            {"version": 2, "doc": {"threshold": 720}, "actor": "b", "ts": 2},
            {"version": 3, "doc": {"threshold": 750}, "actor": "a", "ts": 3},
        ]
        self.assertEqual(crdt.doc_as_of(history, 1)["threshold"], 700)   # travel back
        self.assertEqual(crdt.doc_as_of(history, 2)["threshold"], 720)
        self.assertEqual(crdt.doc_as_of(history, 3)["threshold"], 750)
        self.assertIsNone(crdt.doc_as_of(history, 0))
        self.assertEqual(crdt.diff_versions(history, 1, 3), {"threshold": {"from": 700, "to": 750}})


# --------------------------------------------------------------------------- #
class WhatIfTests(unittest.TestCase):
    """#82 — custom KPIs over baseline vs candidate, chunked replay."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.st = Storage(path=os.path.join(self.td.name, "wi.db"))
        self.tn = self.st.default_tenant_id
        # Candidate bundle always approves; historical decisions were mixed.
        self.policy = _outcome_policy("POL-WI", "approve")
        self.st.create_policy(self.policy, tenant_id=self.tn)
        _save_bundle(self.st, self.tn, self.policy)
        for i in range(300):
            self.st.add_decision({"id": f"wi-{i}", "policy_id": "POL-WI",
                                  "outcome": "approve" if i % 2 == 0 else "reject",
                                  "payload": {}, "computed_variables": {"score": 600 + i}},
                                 tenant_id=self.tn)

    def tearDown(self):
        self.td.cleanup()

    def test_kpis_baseline_vs_candidate(self):
        kpis = [
            {"name": "approval_rate", "type": "outcome_rate", "outcome": "approve"},
            {"name": "avg_score", "type": "avg", "field": "score"},
            {"name": "high_score", "type": "count_where", "field": "score", "op": ">=", "value": 800},
        ]
        res = whatif.simulate_kpis(self.st, "POL-WI", kpis, tenant_id=self.tn, full=True)
        self.assertEqual(res["scanned"], 300)
        by = {k["name"]: k for k in res["kpis"]}
        # baseline approval = 150/300 = 0.5; candidate always approves = 1.0
        self.assertAlmostEqual(by["approval_rate"]["baseline"], 0.5)
        self.assertAlmostEqual(by["approval_rate"]["candidate"], 1.0)
        self.assertAlmostEqual(by["approval_rate"]["delta"], 0.5)
        # avg_score is a variable metric — identical on both sides (variables unchanged by replay)
        self.assertEqual(by["avg_score"]["baseline"], by["avg_score"]["candidate"])

    def test_missing_bundle_raises(self):
        st2 = Storage(path=os.path.join(self.td.name, "empty.db"))
        with self.assertRaises(ValueError):
            whatif.simulate_kpis(st2, "nope", [{"name": "x", "type": "count"}], tenant_id=st2.default_tenant_id)


# --------------------------------------------------------------------------- #
class ShadowTests(unittest.TestCase):
    """#81 — dark launch: candidate runs on live traffic, never affects the returned decision."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.st = Storage(path=os.path.join(self.td.name, "sh.db"))
        self.tn = self.st.default_tenant_id
        self.live = _outcome_policy("POL-LIVE", "approve")
        self.cand = _outcome_policy("POL-CAND", "reject")   # candidate would flip every decision
        self.st.create_policy(self.live, tenant_id=self.tn)
        self.st.create_policy(self.cand, tenant_id=self.tn)
        _save_bundle(self.st, self.tn, self.cand)

    def tearDown(self):
        self.td.cleanup()

    def test_register_run_and_report(self):
        shadow.register_shadow(self.st, "POL-LIVE", "POL-CAND", tenant_id=self.tn)
        self.assertEqual(shadow.shadow_target(self.st, "POL-LIVE", tenant_id=self.tn), "POL-CAND")
        # Live decided "approve"; shadow candidate would "reject" -> diverged.
        out = shadow.run_shadow(self.st, "POL-LIVE", {"score": 700}, "approve", tenant_id=self.tn)
        self.assertIsNotNone(out)
        self.assertEqual(out["shadow_outcome"], "reject")
        self.assertTrue(out["diverged"])
        rep = shadow.shadow_report(self.st, "POL-LIVE", tenant_id=self.tn)
        self.assertEqual(rep["shadow_count"], 1)
        self.assertEqual(rep["divergence_rate_pct"], 100.0)

    def test_no_shadow_registered_is_noop(self):
        self.assertIsNone(shadow.run_shadow(self.st, "POL-LIVE", {}, "approve", tenant_id=self.tn))

    def test_unregister(self):
        shadow.register_shadow(self.st, "POL-LIVE", "POL-CAND", tenant_id=self.tn)
        shadow.unregister_shadow(self.st, "POL-LIVE", tenant_id=self.tn)
        self.assertIsNone(shadow.shadow_target(self.st, "POL-LIVE", tenant_id=self.tn))


# --------------------------------------------------------------------------- #
class ReleaseRollbackTests(unittest.TestCase):
    """#83 — release snapshots + one-click rollback restores the prior definition."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.st = Storage(path=os.path.join(self.td.name, "rel.db"))
        self.tn = self.st.default_tenant_id

    def tearDown(self):
        self.td.cleanup()

    def test_rollback_restores_snapshot_and_records_promotion(self):
        # v1: defaultOutcome=approve. Capture a promotion snapshot of v1.
        policy = {"id": "POL-R", "name": "Rel v1", "status": "prod", "defaultOutcome": "approve",
                  "steps": [{"id": "s", "type": "outcome", "outcome": "approve"}]}
        self.st.create_policy(policy, tenant_id=self.tn)
        from app.policy_diff import policy_snapshot
        snap = policy_snapshot(self.st, self.tn, self.st.get_policy("POL-R", tenant_id=self.tn))
        self.st.add_promotion("policy", "POL-R", "dev", "prod", "alice", "ship v1",
                              tenant_id=self.tn, snapshot=snap)
        releases = release.list_releases(self.st, "POL-R", tenant_id=self.tn)
        self.assertEqual(len(releases), 1)
        promo_id = releases[0]["promotion_id"]

        # v2: change the policy (defaultOutcome -> reject, rename).
        self.st.update_policy("POL-R", {"name": "Rel v2", "defaultOutcome": "reject"}, tenant_id=self.tn)
        self.assertEqual(self.st.get_policy("POL-R", tenant_id=self.tn)["defaultOutcome"], "reject")

        # One-click rollback to v1's snapshot.
        result = release.rollback_policy(self.st, "POL-R", promo_id, "bob", tenant_id=self.tn)
        self.assertEqual(result["rolled_back_to"], promo_id)
        restored = self.st.get_policy("POL-R", tenant_id=self.tn)
        self.assertEqual(restored["defaultOutcome"], "approve")   # v1 restored
        self.assertEqual(restored["name"], "Rel v1")
        # The rollback itself is recorded as a forward promotion (append-only history).
        rollbacks = [p for p in self.st.list_promotions(tenant_id=self.tn)
                     if p.get("entity_id") == "POL-R" and "Rollback" in (p.get("reason") or "")]
        self.assertEqual(len(rollbacks), 1)

    def test_rollback_missing_snapshot_raises(self):
        self.st.create_policy({"id": "POL-X", "name": "x", "steps": []}, tenant_id=self.tn)
        with self.assertRaises(ValueError):
            release.rollback_policy(self.st, "POL-X", 999, "a", tenant_id=self.tn)


if __name__ == "__main__":
    unittest.main()
