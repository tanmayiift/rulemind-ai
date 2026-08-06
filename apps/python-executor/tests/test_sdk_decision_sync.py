"""Batch ingest of on-device decisions — idempotent, retry-safe (POST /sdk/v1/decisions).

Decisions made on-device are queued locally and drained in batches. Ingestion must be
idempotent by the client-stable id so a device retrying with exponential backoff never
double-counts (the same guarantee as the /decide single-log fix).
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
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app.storage import Storage  # noqa: E402


def _decision(did: str, outcome: str = "approve", score_payload=None):
    return {
        "id": did, "policy_id": "policy_instant_personal_loan", "outcome": outcome,
        "payload": score_payload or {"bureau_score": 810}, "computed_variables": {"bureau_score": 810},
        "rule_results": [], "latency_ms": 3, "created_at": "2026-07-31T10:00:00Z",
    }


class SdkDecisionBatchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "sync.db"))
        self.client = TestClient(app_main.app)
        self.tenant = app_main.storage.default_tenant_id
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _count(self):
        return app_main.storage.count_decisions(tenant_id=self.tenant)

    def test_batch_ingest_inserts_and_acks(self):
        batch = [_decision(f"dec-{i}") for i in range(5)]
        before = self._count()
        r = self.client.post("/sdk/v1/decisions", headers=self.headers, json={"decisions": batch})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["inserted"], 5)
        self.assertEqual(body["duplicates"], 0)
        self.assertEqual(set(body["acked"]), {f"dec-{i}" for i in range(5)})
        self.assertEqual(self._count(), before + 5)

    def test_resending_the_same_batch_never_double_counts(self):
        batch = [_decision(f"dec-{i}") for i in range(5)]
        self.client.post("/sdk/v1/decisions", headers=self.headers, json={"decisions": batch})
        after_first = self._count()
        # Retry the identical batch (e.g. the ack was lost on the wire).
        r = self.client.post("/sdk/v1/decisions", headers=self.headers, json={"decisions": batch})
        body = r.json()
        self.assertEqual(body["inserted"], 0)
        self.assertEqual(body["duplicates"], 5)
        self.assertEqual(set(body["acked"]), {f"dec-{i}" for i in range(5)})  # still fully acked -> safe to clear
        self.assertEqual(self._count(), after_first)  # no new rows

    def test_partial_overlap_inserts_only_new(self):
        self.client.post("/sdk/v1/decisions", headers=self.headers, json={"decisions": [_decision("a"), _decision("b")]})
        before = self._count()
        r = self.client.post("/sdk/v1/decisions", headers=self.headers,
                             json={"decisions": [_decision("b"), _decision("c"), _decision("d")]})
        body = r.json()
        self.assertEqual(body["inserted"], 2)   # c, d
        self.assertEqual(body["duplicates"], 1)  # b
        self.assertEqual(self._count(), before + 2)

    def test_duplicate_ids_within_one_batch_are_deduped(self):
        r = self.client.post("/sdk/v1/decisions", headers=self.headers,
                             json={"decisions": [_decision("x"), _decision("x"), _decision("y")]})
        body = r.json()
        self.assertEqual(body["inserted"], 2)
        self.assertEqual(body["duplicates"], 1)

    def test_source_and_device_time_preserved(self):
        self.client.post("/sdk/v1/decisions", headers=self.headers, json={"decisions": [_decision("z")]})
        rows = app_main.storage.list_decisions(tenant_id=self.tenant, limit=10)
        # The device-supplied id is now the client_id (the PK is server-generated).
        row = next(r for r in rows if r["client_id"] == "z")
        self.assertEqual(row["source"], "on_device")
        self.assertTrue(str(row["created_at"]).startswith("2026-07-31"))  # device decision time kept

    def test_oversize_batch_rejected(self):
        big = [_decision(f"d{i}") for i in range(app_main.SDK_DECISIONS_BATCH_MAX + 1)]
        r = self.client.post("/sdk/v1/decisions", headers=self.headers, json={"decisions": big})
        self.assertEqual(r.status_code, 413)

    def test_requires_auth(self):
        self.assertEqual(self.client.post("/sdk/v1/decisions", json={"decisions": []}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
