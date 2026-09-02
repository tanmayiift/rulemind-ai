"""Idempotency-Key on public writes (Track B6).

A client that retries a POST after a timeout cannot know whether the first attempt landed.
Without a key the retry silently duplicates the write -- for a decisioning platform that means
a duplicate decision, a duplicate policy, a double-counted anything.

The cases that matter are the ones where a plausible implementation gets it wrong:

  * a retry must not create a SECOND object (the whole point);
  * the same key with a DIFFERENT body must be rejected, not served the first response, or a
    client that reuses one key across operations silently loses writes;
  * a 5xx must NOT be remembered, or a transient failure pins the key forever;
  * keys must be per tenant, or one customer's uuid collides with another's;
  * no header must mean no behaviour change, or the upgrade breaks every integration.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import idempotency  # noqa: E402
from app.models import IdempotencyRecord  # noqa: E402
from app.storage import Storage  # noqa: E402


class IdempotencyUnitTest(unittest.TestCase):
    def test_fingerprint_covers_method_path_and_body(self):
        base = idempotency.request_fingerprint("POST", "/api/v1/rules", b'{"a":1}')
        self.assertNotEqual(base, idempotency.request_fingerprint("PUT", "/api/v1/rules", b'{"a":1}'))
        self.assertNotEqual(base, idempotency.request_fingerprint("POST", "/api/v1/policies", b'{"a":1}'),
                            "the same body posted to a different endpoint is a different operation")
        self.assertNotEqual(base, idempotency.request_fingerprint("POST", "/api/v1/rules", b'{"a":2}'))
        self.assertEqual(base, idempotency.request_fingerprint("post", "/api/v1/rules", b'{"a":1}'),
                         "method comparison is case-insensitive")

    def test_only_5xx_is_forgotten(self):
        self.assertTrue(idempotency.should_remember(201))
        self.assertTrue(idempotency.should_remember(422), "a validation failure is deterministic")
        self.assertFalse(idempotency.should_remember(500), "a transient failure must not pin the key")
        self.assertFalse(idempotency.should_remember(503))

    def test_expiry(self):
        now = datetime(2026, 9, 3, 12, 0, 0)
        self.assertFalse(idempotency.is_expired(now - timedelta(hours=23), now=now))
        self.assertTrue(idempotency.is_expired(now - timedelta(hours=25), now=now))
        self.assertTrue(idempotency.is_expired(None))


class IdempotencyStorageTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(path=os.path.join(self.tempdir.name, "idem.db"))
        self.tenant = self.storage.default_tenant_id

    def tearDown(self):
        self.tempdir.cleanup()

    def test_claim_then_replay(self):
        self.assertEqual(self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)["action"], "execute")
        self.assertEqual(self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)["action"], "in_flight")
        self.storage.complete_idempotency_key("k1", 201, '{"id":"x"}', tenant_id=self.tenant)
        replay = self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)
        self.assertEqual(replay["action"], "replay")
        self.assertEqual(replay["status"], 201)
        self.assertEqual(replay["body"], '{"id":"x"}')

    def test_same_key_different_request_is_a_mismatch(self):
        self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)
        self.storage.complete_idempotency_key("k1", 201, "{}", tenant_id=self.tenant)
        self.assertEqual(self.storage.claim_idempotency_key("k1", "DIFFERENT", tenant_id=self.tenant)["action"],
                         "mismatch")

    def test_a_5xx_releases_the_key(self):
        """Otherwise a database blip makes the key permanently unusable for the retry it exists
        to serve."""
        self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)
        self.storage.complete_idempotency_key("k1", 500, "boom", tenant_id=self.tenant)
        self.assertEqual(self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)["action"], "execute")

    def test_keys_are_scoped_per_tenant(self):
        other = self.storage.create_tenant("Other Co")["id"]
        self.storage.claim_idempotency_key("shared", "h1", tenant_id=self.tenant)
        self.storage.complete_idempotency_key("shared", 201, '{"mine":true}', tenant_id=self.tenant)
        self.assertEqual(self.storage.claim_idempotency_key("shared", "h1", tenant_id=other)["action"], "execute",
                         "another tenant's identical key must not collide")

    def test_an_expired_key_is_reusable(self):
        self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)
        self.storage.complete_idempotency_key("k1", 201, "{}", tenant_id=self.tenant)
        with self.storage.connect() as session:
            row = session.scalar(
                __import__("sqlalchemy").select(IdempotencyRecord).where(IdempotencyRecord.key == "k1"))
            row.created_at = datetime.utcnow() - timedelta(hours=48)
        self.assertEqual(self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)["action"], "execute")

    def test_release_frees_the_key(self):
        self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)
        self.storage.release_idempotency_key("k1", tenant_id=self.tenant)
        self.assertEqual(self.storage.claim_idempotency_key("k1", "h1", tenant_id=self.tenant)["action"], "execute")


class IdempotencyEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "e2e.db"))
        self.client = TestClient(app_main.app)
        self.headers = {"x-api-key": app_main.storage.default_api_key or ""}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _body(self, name="idem_conn"):
        return {"name": name, "type": "rest", "config": {}}

    def test_a_retry_does_not_create_a_second_object(self):
        """The whole point of the feature."""
        headers = {**self.headers, "Idempotency-Key": "key-abc"}
        first = self.client.post("/api/v1/connectors", headers=headers, json=self._body())
        self.assertLess(first.status_code, 300, first.text[:200])

        second = self.client.post("/api/v1/connectors", headers=headers, json=self._body())
        self.assertEqual(second.status_code, first.status_code)
        self.assertEqual(second.json()["id"], first.json()["id"], "the retry must replay, not re-create")
        self.assertEqual(second.headers.get("Idempotent-Replay"), "true")

        listing = self.client.get("/api/v1/connectors", headers=self.headers).json()
        self.assertEqual(len([c for c in listing if c.get("name") == "idem_conn"]), 1,
                         "exactly one object may exist after the retry")

    def test_without_the_header_nothing_changes(self):
        """No header, no behaviour change -- otherwise the upgrade breaks every integration."""
        a = self.client.post("/api/v1/connectors", headers=self.headers, json=self._body("plain"))
        b = self.client.post("/api/v1/connectors", headers=self.headers, json=self._body("plain"))
        self.assertLess(a.status_code, 300)
        self.assertLess(b.status_code, 300)
        self.assertNotEqual(a.json()["id"], b.json()["id"], "unkeyed writes stay independent")

    def test_reusing_a_key_for_a_different_body_is_422(self):
        """A client that generates one key and reuses it would otherwise silently lose the
        second write."""
        headers = {**self.headers, "Idempotency-Key": "key-reused"}
        self.client.post("/api/v1/connectors", headers=headers, json=self._body("first"))
        clash = self.client.post("/api/v1/connectors", headers=headers, json=self._body("second"))
        self.assertEqual(clash.status_code, 422)
        self.assertEqual(clash.json()["detail"]["error"], "idempotency_key_reused")

        listing = self.client.get("/api/v1/connectors", headers=self.headers).json()
        self.assertEqual([c["name"] for c in listing if c["name"] == "second"], [],
                         "the rejected request must not have been applied")

    def test_the_request_body_still_reaches_the_handler(self):
        """The middleware consumes the request body to fingerprint it. That is only safe
        because starlette's BaseHTTPMiddleware wraps the request in _CachedRequest, which
        caches the body on a .body() call and replays it downstream.

        This test guards that dependency. It does NOT prove any code in this repo -- an
        earlier version of the middleware carried a hand-rolled receive-channel replay, and
        removing that line changed nothing, which is how the redundancy was found. What the
        test would catch is a starlette upgrade that drops the caching, at which point every
        keyed POST would reach its handler with an empty payload."""
        res = self.client.post("/api/v1/connectors",
                               headers={**self.headers, "Idempotency-Key": "key-body"},
                               json=self._body("carried-through"))
        self.assertLess(res.status_code, 300, res.text[:200])
        self.assertEqual(res.json()["name"], "carried-through")

    def test_a_get_is_untouched(self):
        res = self.client.get("/api/v1/connectors", headers={**self.headers, "Idempotency-Key": "key-get"})
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.headers.get("Idempotent-Replay"))

    def test_a_replayed_error_is_also_replayed(self):
        """4xx is a deterministic outcome; replaying it is correct, and NOT caching it would let
        a client retry its way around validation."""
        headers = {**self.headers, "Idempotency-Key": "key-bad"}
        first = self.client.post("/api/v1/policies", headers=headers, json={"name": "no steps"})
        self.assertEqual(first.status_code, 422)
        second = self.client.post("/api/v1/policies", headers=headers, json={"name": "no steps"})
        self.assertEqual(second.status_code, 422)
        self.assertEqual(second.headers.get("Idempotent-Replay"), "true")

    def test_keys_do_not_leak_across_tenants(self):
        other = app_main.storage.create_tenant("Other Co")
        other_headers = {
            "x-api-key": app_main.storage.generate_api_key_for_tenant(other["id"])["plaintext"],
            "Idempotency-Key": "shared-key",
        }
        mine = self.client.post("/api/v1/connectors",
                                headers={**self.headers, "Idempotency-Key": "shared-key"},
                                json=self._body("mine"))
        theirs = self.client.post("/api/v1/connectors", headers=other_headers, json=self._body("theirs"))
        self.assertLess(theirs.status_code, 300,
                        "the other tenant's identical key must not replay my response")
        self.assertNotEqual(theirs.json()["id"], mine.json()["id"])


if __name__ == "__main__":
    unittest.main()
