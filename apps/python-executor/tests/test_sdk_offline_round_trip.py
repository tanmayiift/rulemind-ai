"""The offline -> online round trip for on-device decisions.

A decision taken on a device with no connectivity must survive, reach the
backend once connectivity returns, and appear in the audit log under the id the
device generated -- exactly once, however many times the device retries.

This was the one piece of the SDK story that had never been proven end to end.
The pieces were individually tested (batch insert in test_sdk_decision_sync,
execution sync in test_sdk_runtime) but nothing joined "the device could not
reach the server" to "the decision is visible in /api/v1/audit/decisions".

Driven from Python rather than Dart deliberately. `RuleMind._httpClient`
(rulemind.dart:33) is private with no injection seam, and `resetForTest`
(:355-366) never clears the outbox or the syncer, so Dart cannot exercise the
wire path without an SDK change. What Python *can* reach is the observable
contract: the exact record the device puts on the wire, the server's ack list,
dedup, and the audit read.

The records below mirror `RuleMind._toPending` (rulemind.dart:164-187) key for
key, including snake_case and `source: "on_device"`. If that shape changes, this
test should fail -- that is the point of duplicating it rather than importing.
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
# Not optional: middleware.py:93 early-returns on AUTH_MODE=none, which would
# skip the API-key check AND the RBAC capability check. The round trip should
# traverse real auth, since every /sdk/v1 write maps to the `decide` capability.
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402

POLICY = "policy_instant_personal_loan"


def _on_device_record(exec_id: str, outcome: str = "approve", *, score=810):
    """One record shaped exactly as RuleMind._toPending puts it on the wire."""
    return {
        "id": exec_id,                       # == Decision.executionId
        "policy_id": POLICY,
        "outcome": outcome,
        "payload": {"bureau_score": score},
        "computed_variables": {"bureau_score": score},
        "rule_results": [],
        "scorecard_result": None,
        "latency_ms": 2,
        "source": "on_device",
        "sdk_version": "4.1.0",
        "experiment_variant": None,
        "created_at": "2026-08-28T09:15:00Z",
    }


class SdkOfflineRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "offline.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
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

    # ── helpers ─────────────────────────────────────────────────────────────

    def _drain(self, records):
        """What DecisionSyncer.sync does: POST the queued batch."""
        return self.client.post(
            "/sdk/v1/decisions", headers=self.headers, json={"decisions": records}
        )

    def _audit(self, client_id):
        """Read the decision back the way an operator would.

        The device's executionId is stored as `client_id`, NOT `id` -- `id` is a
        server-generated uuid (storage.py:1385) and `client_id` carries the
        device's own (storage.py:782). Filtering on the wrong one silently
        returns everything.
        """
        r = self.client.get(
            "/api/v1/audit/decisions",
            headers=self.headers,
            params={"client_id": client_id, "limit": 50},
        )
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        return body if isinstance(body, list) else body.get("items", body.get("decisions", []))

    # ── the round trip ──────────────────────────────────────────────────────

    def test_offline_decision_reaches_the_audit_log_when_connectivity_returns(self):
        exec_id = "exec_1756377300000_offline1"
        record = _on_device_record(exec_id)

        # OFFLINE: the device decided, the outbox holds it, nothing was sent.
        self.assertEqual(self._audit(exec_id), [], "nothing should exist before the flush")

        # ONLINE: the syncer drains the outbox.
        resp = self._drain([record])
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()
        self.assertEqual(body["received"], 1)
        self.assertEqual(body["inserted"], 1)
        self.assertEqual(body["duplicates"], 0)
        self.assertIn(exec_id, body["acked"], "the device clears its outbox on the ack")

        rows = self._audit(exec_id)
        self.assertEqual(len(rows), 1, f"expected exactly one decision, got {len(rows)}")
        row = rows[0]
        self.assertEqual(row["client_id"], exec_id)
        self.assertEqual(row["policy_id"], POLICY)
        self.assertEqual(row["outcome"], "approve")
        # The device's own clock survives the trip -- an offline decision must not
        # be re-dated to whenever the network happened to come back.
        self.assertIn("2026-08-28", str(row.get("created_at", "")))

    def test_replaying_the_same_batch_does_not_double_log(self):
        """The lost-ack case. The device retries when it did not see the ack."""
        exec_id = "exec_1756377300001_retry"
        record = _on_device_record(exec_id, outcome="review")

        first = self._drain([record]).json()
        self.assertEqual(first["inserted"], 1)

        second = self._drain([record]).json()
        self.assertEqual(second["received"], 1)
        self.assertEqual(second["inserted"], 0, "a replay must not insert again")
        self.assertEqual(second["duplicates"], 1)
        self.assertIn(
            exec_id, second["acked"],
            "a duplicate must still be acked, or the device retries forever",
        )

        self.assertEqual(len(self._audit(exec_id)), 1, "still exactly one row")

    def test_a_whole_outbox_drains_in_one_batch(self):
        """DecisionOutbox.pending yields oldest-first; every id must be acked or
        the device cannot clear the ones the server did accept."""
        ids = [f"exec_1756377300{i:03d}_drain" for i in range(5)]
        records = [_on_device_record(i, outcome="reject" if n % 2 else "approve")
                   for n, i in enumerate(ids)]

        body = self._drain(records).json()
        self.assertEqual(body["received"], 5)
        self.assertEqual(body["inserted"], 5)
        self.assertEqual(set(body["acked"]), set(ids))

        for i in ids:
            self.assertEqual(len(self._audit(i)), 1, f"{i} missing or duplicated")

    def test_a_partial_replay_inserts_only_what_is_new(self):
        """Realistic: the ack was lost after some rows landed, so the device
        resends a batch that overlaps what the server already has."""
        old = _on_device_record("exec_1756377300100_overlap_a")
        self._drain([old])

        new = _on_device_record("exec_1756377300101_overlap_b", outcome="review")
        body = self._drain([old, new]).json()
        self.assertEqual(body["received"], 2)
        self.assertEqual(body["inserted"], 1)
        self.assertEqual(body["duplicates"], 1)
        self.assertEqual(set(body["acked"]), {old["id"], new["id"]})

    def test_an_unauthenticated_drain_is_rejected(self):
        """The outbox must not be a way around auth."""
        r = self.client.post(
            "/sdk/v1/decisions", json={"decisions": [_on_device_record("exec_noauth")]}
        )
        self.assertIn(r.status_code, (401, 403), f"expected auth failure, got {r.status_code}")
        self.assertEqual(self._audit("exec_noauth"), [])
