"""Webhook delivery survives a restart, retries with backoff, and dead-letters -- as promised.

`webhook_dispatch.py`'s docstring said a failed delivery is "retried by the scheduler, and
dead-lettered after a bound". What shipped was a daemon thread per event per subscription with an
in-thread retry loop and only last-known state on the subscription row. So:

  * a SIGTERM or a rolling deploy dropped every in-flight event, with no record it existed;
  * a receiver down for ten minutes lost everything emitted in those ten minutes;
  * there was no dead-letter queue to replay from;
  * one event x 50 subscriptions was 50 OS threads started inside a request handler.

Each test below is a property the old design could not have had. The one that matters most is
`test_a_delivery_claimed_by_a_process_that_died_is_recovered`: it is the SIGTERM case, simulated
by claiming a row and never finishing it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402
from app import webhook_dispatch  # noqa: E402
from app.models import WebhookDelivery  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests._storage_global import preserve_storage_global  # noqa: E402


class DurableWebhookTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tempdir.cleanup)
        preserve_storage_global(self)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "hooks.db"))
        app_main.storage = self.storage
        self.tenant = self.storage.default_tenant_id
        self.client = TestClient(app_main.app)
        self.addCleanup(self.client.close)
        self.headers = {"x-api-key": self.storage.default_api_key or ""}
        webhook_dispatch.set_emission_enabled(True)
        self.addCleanup(webhook_dispatch.flush, 5.0)
        created = self.client.post("/api/v1/webhook-subscriptions", headers=self.headers, json={
            "name": "durability", "url": "https://receiver.example.com/hook",
            "event_types": ["policy_lifecycle_changed"], "retries": 0})
        self.assertEqual(created.status_code, 200, created.text)
        self.subscription = created.json()

    def _deliver_returns(self, result):
        async def fake(subscription, event, delivery_id):
            return dict(result)
        return mock.patch.object(webhook_dispatch, "deliver", side_effect=fake)

    def _emit(self):
        return webhook_dispatch.emit(self.storage, self.tenant, "policy_lifecycle_changed", {"policy_id": "p1"})

    def _only_delivery(self):
        rows = self.storage.list_webhook_deliveries(
            subscription_id=self.subscription["id"], tenant_id=self.tenant)
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    # ── the event is written before it is sent ─────────────────────────────

    def test_the_event_is_recorded_before_any_delivery_is_attempted(self):
        """The whole point. With delivery stubbed to hang, the row must already exist -- a
        process killed at that moment leaves a record the drain can finish from."""
        gate = threading.Event()

        async def hang(subscription, event, delivery_id):
            gate.wait(5)
            return {"delivered": True, "status": 200}

        with mock.patch.object(webhook_dispatch, "deliver", side_effect=hang):
            self.assertEqual(self._emit(), 1)
            row = self._only_delivery()
            self.assertIn(row["status"], ("pending", "delivering"))
            self.assertEqual(row["event"]["type"], "policy_lifecycle_changed")
            gate.set()
            webhook_dispatch.flush(5)
        self.assertEqual(self._only_delivery()["status"], "delivered")

    def test_a_successful_delivery_is_marked_delivered(self):
        with self._deliver_returns({"delivered": True, "status": 204}):
            self._emit()
            webhook_dispatch.flush(5)
        row = self._only_delivery()
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_status"], 204)

    # ── retry, backoff, dead letter ────────────────────────────────────────

    def test_a_failure_is_rescheduled_with_backoff_not_dropped(self):
        """A receiver that is down now is not a receiver that is down forever."""
        with self._deliver_returns({"delivered": False, "status": 503, "error": "HTTP 503"}):
            self._emit()
            webhook_dispatch.flush(5)
        row = self._only_delivery()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        due = datetime.fromisoformat(row["next_attempt_at"].rstrip("Z"))
        self.assertGreater(due, datetime.utcnow(), "the retry must be in the future, not immediate")

    def test_it_is_dead_lettered_after_the_bound_and_kept(self):
        with mock.patch.dict(os.environ, {"WEBHOOK_MAX_ATTEMPTS": "2"}):
            with self._deliver_returns({"delivered": False, "status": 500, "error": "HTTP 500"}):
                self._emit()
                webhook_dispatch.flush(5)
                self._make_due()
                webhook_dispatch.drain(self.storage)
        row = self._only_delivery()
        self.assertEqual(row["status"], "dead")
        self.assertEqual(row["attempts"], 2)
        dead = self.client.get(
            f"/api/v1/webhook-subscriptions/{self.subscription['id']}/deliveries?status=dead",
            headers=self.headers).json()
        self.assertEqual([r["id"] for r in dead], [row["id"]], "the dead letter must be listable")

    def test_a_target_blocked_by_egress_goes_straight_to_dead(self):
        """A configuration refusal, not a transient failure -- retrying it just repeats the
        refusal on a timer for nine hours."""
        with self._deliver_returns({"delivered": False, "blocked": True, "error": "blocked", "attempts": 0}):
            self._emit()
            webhook_dispatch.flush(5)
        self.assertEqual(self._only_delivery()["status"], "dead")

    def test_a_dead_letter_can_be_replayed(self):
        """A dead-letter queue you cannot drain is just a graveyard."""
        with mock.patch.dict(os.environ, {"WEBHOOK_MAX_ATTEMPTS": "1"}):
            with self._deliver_returns({"delivered": False, "status": 500, "error": "HTTP 500"}):
                self._emit()
                webhook_dispatch.flush(5)
        row = self._only_delivery()
        self.assertEqual(row["status"], "dead")
        with self._deliver_returns({"delivered": True, "status": 200}):
            replay = self.client.post(f"/api/v1/webhook-deliveries/{row['id']}/redeliver",
                                      headers=self.headers)
            self.assertEqual(replay.status_code, 200, replay.text)
            webhook_dispatch.flush(5)
        after = self._only_delivery()
        self.assertEqual(after["status"], "delivered")
        self.assertEqual(after["attempts"], 2, "the history must show it was dead and replayed")

    # ── crash recovery ─────────────────────────────────────────────────────

    def test_a_delivery_claimed_by_a_process_that_died_is_recovered(self):
        """THE test -- the SIGTERM case. A row claimed (`delivering`) by a process that then
        died would, without stale-claim recovery, sit in a state nothing ever retries."""
        webhook_dispatch.set_emission_enabled(False)
        row = self.storage.enqueue_webhook_delivery(
            self.tenant, self.subscription["id"], {"type": "policy_lifecycle_changed", "data": {}})
        claimed = self.storage.claim_due_webhook_deliveries(limit=10)
        self.assertEqual([c["id"] for c in claimed], [row["id"]])
        # ...and the process dies here. Age the claim past the timeout:
        with self.storage.connect() as session:
            model = session.get(WebhookDelivery, row["id"])
            model.claimed_at = datetime.utcnow() - timedelta(minutes=30)
        with self._deliver_returns({"delivered": True, "status": 200}):
            counts = webhook_dispatch.drain(self.storage)
        self.assertEqual(counts["delivered"], 1)
        self.assertEqual(self._only_delivery()["status"], "delivered")

    def test_a_fresh_claim_is_not_stolen(self):
        """The other side: a claim still inside its timeout belongs to a live worker, and a
        second drain taking it would deliver the event twice while the first is mid-send."""
        webhook_dispatch.set_emission_enabled(False)
        self.storage.enqueue_webhook_delivery(
            self.tenant, self.subscription["id"], {"type": "policy_lifecycle_changed", "data": {}})
        self.assertEqual(len(self.storage.claim_due_webhook_deliveries(limit=10)), 1)
        self.assertEqual(self.storage.claim_due_webhook_deliveries(limit=10), [])

    def test_the_claim_is_exclusive_under_a_race(self):
        """Two claimers that both SELECTed the same pending row must not both win it. Driven on
        the guarded write directly, because a sequential test of the whole claim cannot produce
        the race -- the second claim's SELECT filters the row out first, which is how the first
        mutation of this guard (accept every write) survived the test above."""
        webhook_dispatch.set_emission_enabled(False)
        row = self.storage.enqueue_webhook_delivery(
            self.tenant, self.subscription["id"], {"type": "policy_lifecycle_changed", "data": {}})
        now = datetime.utcnow()
        stale_before = now - timedelta(minutes=5)
        with self.storage.connect() as session:
            first = Storage._try_claim_webhook_delivery(session, row["id"], now, stale_before)
        # The loser: it SELECTed while the row was still pending, and writes after the winner.
        with self.storage.connect() as session:
            second = Storage._try_claim_webhook_delivery(session, row["id"], now, stale_before)
        self.assertTrue(first)
        self.assertFalse(second, "both claimers won the same row -- it would be delivered twice")

    def test_a_deleted_subscription_dead_letters_its_queue_rather_than_delivering(self):
        """Queued before the subscriber said stop; must not be sent after."""
        webhook_dispatch.set_emission_enabled(False)
        self.storage.enqueue_webhook_delivery(
            self.tenant, self.subscription["id"], {"type": "policy_lifecycle_changed", "data": {}})
        self.client.delete(f"/api/v1/webhook-subscriptions/{self.subscription['id']}",
                           headers=self.headers)
        with self._deliver_returns({"delivered": True, "status": 200}) as sent:
            webhook_dispatch.drain(self.storage)
            self.assertEqual(sent.call_count, 0)

    # ── bounded concurrency ────────────────────────────────────────────────

    def test_one_event_to_many_subscriptions_does_not_start_a_thread_each(self):
        """It was one OS thread per event per subscription, started inside the request."""
        for i in range(10):
            self.client.post("/api/v1/webhook-subscriptions", headers=self.headers, json={
                "name": f"fan {i}", "url": f"https://r{i}.example.com/h",
                "event_types": ["policy_lifecycle_changed"]})
        before = {t.ident for t in threading.enumerate()}
        with self._deliver_returns({"delivered": True, "status": 200}):
            self.assertEqual(self._emit(), 11)
            new_threads = [t for t in threading.enumerate() if t.ident not in before]
            webhook_dispatch.flush(5)
        self.assertLessEqual(len(new_threads), 1, [t.name for t in new_threads])

    # ── tenancy ────────────────────────────────────────────────────────────

    def test_deliveries_and_replay_are_tenant_scoped(self):
        with self._deliver_returns({"delivered": True, "status": 200}):
            self._emit()
            webhook_dispatch.flush(5)
        row = self._only_delivery()
        other = self.storage.create_tenant("Other Co")["id"]
        key = {"x-api-key": self.storage.generate_api_key_for_tenant(other)["plaintext"]}
        listing = self.client.get(f"/api/v1/webhook-subscriptions/{self.subscription['id']}/deliveries",
                                  headers=key)
        self.assertEqual(listing.status_code, 404, "another tenant must not see this subscription's log")
        replay = self.client.post(f"/api/v1/webhook-deliveries/{row['id']}/redeliver", headers=key)
        self.assertEqual(replay.status_code, 404, "nor replay its deliveries")

    def test_replay_needs_the_same_capability_as_the_subscription(self):
        """Redelivery re-sends workspace data to a third party -- the authority a subscription
        needs. It was left out of rbac.py's map and fell to the fail-closed default; mapped now,
        and the documented role boundary holds: a policy_maker cannot."""
        with self._deliver_returns({"delivered": True, "status": 200}):
            self._emit()
            webhook_dispatch.flush(5)
        row = self._only_delivery()
        tenant = self.storage.default_tenant_id
        maker = {"x-api-key": self.storage.generate_api_key_for_tenant(tenant, role="policy_maker")["plaintext"]}
        admin = {"x-api-key": self.storage.generate_api_key_for_tenant(tenant, role="admin")["plaintext"]}
        path = f"/api/v1/webhook-deliveries/{row['id']}/redeliver"
        self.assertEqual(self.client.post(path, headers=maker).status_code, 403)
        self.assertEqual(self.client.post(path, headers=admin).status_code, 200)

    def test_the_drain_job_is_leader_gated(self):
        """Every replica draining the same backlog would deliver each event N times."""
        source = (APP_ROOT / "app" / "scheduler.py").read_text(encoding="utf-8")
        block = source.split("async def _scheduled_drain_webhooks", 1)[1][:900]
        self.assertIn("if not _IS_LEADER", block)

    def _make_due(self):
        with self.storage.connect() as session:
            for model in session.query(WebhookDelivery).all():
                model.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)


if __name__ == "__main__":
    unittest.main()
