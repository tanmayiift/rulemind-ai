"""The flow parity row P03 actually asks for:

    subscribe to an event with a filter -> something happens -> a SIGNED payload reaches a
    receiver

Everything else in P03 -- the table, the CRUD, the filters, the signing -- is machinery for this
one sentence, and a subscription that never fires on a real event is not a feature. The `/test`
endpoint does not count: it proves the URL is reachable, not that the product emits anything.

WHY THE HOOK IS ON add_audit_event. It is the single funnel for ten of the thirteen event types
(policy lifecycle, experiment promotion, review completion, SLO breach, cron, reports, the
inbound webhook trigger, SDK sync, repeated login failure, monitor scheduling). Hooking there
rather than at each call site means a new emitter of an existing event type is subscribable the
day it lands, rather than the day someone remembers to add a second call.

DELIVERY IS FIRE-AND-FORGET ON A THREAD, so these tests join on the delivery rather than
asserting immediately. That is the property under test, not an inconvenience: a subscriber must
never be able to delay or fail the thing that emitted the event.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")
os.environ.pop("AUTH_MODE", None)

import app.main as app_main  # noqa: E402
from app import storage as storage_module  # noqa: E402
from app import webhook_dispatch  # noqa: E402
from app.auth import verify_hmac_signature  # noqa: E402
from app.storage import Storage  # noqa: E402


# Long and distinctive ON PURPOSE. The previous leak assertion searched for b"410" -- the score
# in the fixture -- and three digits collide with random UUIDs and timestamps often enough to
# false-fail about 1 run in 100. A sentinel this shape cannot appear unless the payload leaked.
PII_SENTINEL = "PII-SENTINEL-DO-NOT-SHIP-8f2a1c7d4e"


class _Receiver:
    """A stub receiver that records exactly what arrived. Stands in for the partner endpoint;
    `build_pinned_request` refuses loopback, so a real local server is not reachable from the
    delivery path (the same constraint as tests/test_connector_fetch.py)."""

    def __init__(self, status=200):
        self.status = status
        self.received = []
        self.arrived = threading.Event()

    def client_factory(self):
        receiver = self

        class _Pinned:
            url = "https://93.184.216.34/hook"
            host_header = "hooks.example.com"
            sni_hostname = "hooks.example.com"

        class _Response:
            def __init__(self, status):
                self.status_code = status
                self.text = ""

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self_inner):
                return self_inner
            async def __aexit__(self_inner, *a):
                return False
            async def post(self_inner, url, **kwargs):
                receiver.received.append({"url": url, **kwargs})
                receiver.arrived.set()
                return _Response(receiver.status)

        return _Pinned, _Client


class _Base(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: delivery is fire-and-forget on a daemon thread by design, so a
        # case can finish while one is still writing. A cleanup failure is noise about the
        # temp directory, never a signal about the code under test.
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig_storage = app_main.storage
        app_main.storage = Storage(path=os.path.join(self.tempdir.name, "emit.db"))
        self.storage = app_main.storage
        self.tenant = self.storage.default_tenant_id
        storage_module.clear_audit_listeners()
        storage_module.clear_decision_listeners()
        storage_module.register_audit_listener(webhook_dispatch.on_audit_event)
        storage_module.register_decision_listener(webhook_dispatch.on_decision)
        webhook_dispatch.set_emission_enabled(True)

    def tearDown(self):
        storage_module.clear_audit_listeners()
        storage_module.clear_decision_listeners()
        # Dispose the engine, not just the tempdir. db._engine_cache is keyed by URL and never
        # evicts -- correct in a server (a handful of URLs for the process lifetime), a slow leak
        # in a suite where every test mints a fresh temp path. The pool keeps SQLite handles open
        # against a directory the next line deletes, and at ~1700 tests that surfaced as
        # "unable to open database file" in an unrelated module.
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover - teardown must not fail the test
            pass
        app_main.storage = self._orig_storage
        self.tempdir.cleanup()

    def subscribe(self, **overrides):
        payload = {"name": "ops", "url": "https://hooks.example.com/hook",
                   "event_types": ["policy_lifecycle_changed"], "filters": [],
                   "secret": "shhh-secret"}
        payload.update(overrides)
        return self.storage.create_webhook_subscription(payload, tenant_id=self.tenant)

    def audit(self, event_type="policy_lifecycle_changed", **overrides):
        payload = {"tenant_id": self.tenant, "event_type": event_type,
                   "entity_type": "policy", "entity_id": "pol_loan",
                   "detail": "Lifecycle uat -> prod.",
                   "metadata": {"to": "prod", "from": "uat"}}
        payload.update(overrides)
        return self.storage.add_audit_event(payload, tenant_id=self.tenant)

    def emit_and_wait(self, receiver, timeout=5.0, **audit_overrides):
        pinned, client = receiver.client_factory()
        with mock.patch.object(webhook_dispatch, "build_pinned_request", return_value=pinned()):
            with mock.patch.object(webhook_dispatch.httpx, "AsyncClient", client):
                self.audit(**audit_overrides)
                receiver.arrived.wait(timeout)
                # JOIN, rather than sleeping until `received` is populated. The POST is recorded
                # before record_webhook_delivery runs, so waiting on `received` returns while the
                # health write is still in flight -- and a test that emits twice then gets the
                # FIRST delivery's write landing after the second's, leaving consecutive_failures
                # at 1 after a success. Joining makes the ordering deterministic instead of
                # likely, and costs nothing because these deliveries are stubbed.
                for thread in threading.enumerate():
                    if thread.name == "webhook-delivery":
                        thread.join(timeout=timeout + 2.0)
        return receiver.received


class TheEndToEndFlowTest(_Base):

    def test_a_real_event_reaches_the_receiver_signed(self):
        self.subscribe()
        receiver = _Receiver()
        received = self.emit_and_wait(receiver)

        self.assertEqual(len(received), 1, "the audit event did not reach the subscriber")
        body = received[0]["content"]
        headers = received[0]["headers"]
        self.assertTrue(verify_hmac_signature("shhh-secret", body,
                                              headers[webhook_dispatch.SIGNATURE_HEADER]),
                        "the payload arrived but the signature does not verify")
        payload = json.loads(body)
        self.assertEqual(payload["type"], "policy_lifecycle_changed")
        self.assertEqual(payload["tenant_id"], self.tenant)
        self.assertEqual(payload["data"]["entity_id"], "pol_loan")

    def test_a_filter_that_does_not_match_means_no_delivery(self):
        self.subscribe(filters=[{"path": "data.metadata.to", "operator": "==", "value": "uat"}])
        receiver = _Receiver()
        self.assertEqual(self.emit_and_wait(receiver, timeout=1.0), [],
                         "an event the filter excludes was still delivered")

    def test_a_filter_that_matches_delivers(self):
        """The positive control for the test above -- without it, a build that delivers nothing
        at all would pass."""
        self.subscribe(filters=[{"path": "data.metadata.to", "operator": "==", "value": "prod"}])
        self.assertEqual(len(self.emit_and_wait(_Receiver())), 1)

    def test_an_unsubscribed_event_type_does_not_deliver(self):
        self.subscribe(event_types=["slo_breach"])
        self.assertEqual(self.emit_and_wait(_Receiver(), timeout=1.0), [])

    def test_an_inactive_subscription_does_not_deliver(self):
        self.subscribe(is_active=False)
        self.assertEqual(self.emit_and_wait(_Receiver(), timeout=1.0), [])

    def test_another_workspaces_subscription_never_receives_this_events(self):
        """Tenant isolation on the DELIVERY path, which is a different code path from the CRUD
        isolation covered in test_webhook_subscriptions_api.py -- and the one where a leak would
        actually send another workspace's data somewhere."""
        other = self.storage.create_tenant("Other Co")["id"]
        self.storage.create_webhook_subscription(
            {"name": "theirs", "url": "https://hooks.example.com/hook",
             "event_types": ["policy_lifecycle_changed"], "filters": [], "secret": "s"},
            tenant_id=other)
        receiver = _Receiver()
        # The audit event belongs to self.tenant; the only subscription is the other workspace's.
        self.assertEqual(self.emit_and_wait(receiver, timeout=1.0), [],
                         "one workspace's event was delivered to another's subscriber")


class DeliveryNeverAffectsTheEmitterTest(_Base):
    """A subscriber must not be able to delay, fail, or alter the thing that emitted the event."""

    def test_a_failing_receiver_does_not_fail_the_audit_write(self):
        self.subscribe()
        receiver = _Receiver(status=500)
        self.emit_and_wait(receiver)
        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                  if e["event_type"] == "policy_lifecycle_changed"]
        self.assertEqual(len(events), 1, "a 500 from a subscriber lost the audit event")

    def test_a_listener_that_raises_does_not_fail_the_audit_write(self):
        def exploding(storage, event):
            raise RuntimeError("listener is broken")

        storage_module.register_audit_listener(exploding)
        self.audit()
        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                  if e["event_type"] == "policy_lifecycle_changed"]
        self.assertEqual(len(events), 1, "a broken listener took down the audit write")

    def test_the_audit_write_does_not_block_on_a_slow_receiver(self):
        """The whole reason delivery is on a thread. A receiver that never answers must not hold
        the caller that recorded the event."""
        self.subscribe()

        class _Pinned:
            url = "https://93.184.216.34/hook"
            host_header = "hooks.example.com"
            sni_hostname = "hooks.example.com"

        class _SlowClient:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self_inner):
                return self_inner
            async def __aexit__(self_inner, *a):
                return False
            async def post(self_inner, url, **kwargs):
                time.sleep(3.0)
                raise AssertionError("should not be waited on")

        started = time.perf_counter()
        with mock.patch.object(webhook_dispatch, "build_pinned_request", return_value=_Pinned()):
            with mock.patch.object(webhook_dispatch.httpx, "AsyncClient", _SlowClient):
                self.audit()
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertLess(elapsed_ms, 800,
                        "recording an audit event waited on webhook delivery ({0:.0f}ms)".format(
                            elapsed_ms))
        # Join the delivery thread before returning. The assertion above is the subject of this
        # test; cleaning up after it is orthogonal, and NOT doing so is a real bug -- the thread
        # sleeps 3s and then writes delivery health, while tearDown deletes the tempdir holding
        # the SQLite file underneath it. That surfaced as a tearDown error in an unrelated
        # module's slot during a full run, which is a miserable thing to debug.
        for thread in threading.enumerate():
            if thread.name == "webhook-delivery":
                thread.join(timeout=6.0)

    def test_delivery_health_is_recorded_so_a_silent_failure_is_visible(self):
        subscription = self.subscribe()
        self.emit_and_wait(_Receiver(status=500))
        for _ in range(50):
            row = self.storage.get_webhook_subscription(subscription["id"], tenant_id=self.tenant)
            if row["consecutive_failures"]:
                break
            time.sleep(0.02)
        self.assertFalse(row["last_delivery_ok"])
        self.assertGreaterEqual(row["consecutive_failures"], 1)

    def test_a_success_clears_the_failure_streak(self):
        subscription = self.subscribe()
        self.emit_and_wait(_Receiver(status=500))
        self.emit_and_wait(_Receiver(status=200))
        for _ in range(50):
            row = self.storage.get_webhook_subscription(subscription["id"], tenant_id=self.tenant)
            if row["last_delivery_ok"]:
                break
            time.sleep(0.02)
        self.assertTrue(row["last_delivery_ok"])
        self.assertEqual(row["consecutive_failures"], 0)


class EmissionIsScopedTest(_Base):

    def test_an_event_type_outside_the_closed_list_emits_nothing(self):
        """`add_audit_event` carries types this feature deliberately does not offer. They must
        not become deliverable by accident."""
        self.subscribe()
        self.assertEqual(
            webhook_dispatch.emit(self.storage, self.tenant, "some_internal_thing", {}), 0)

    def test_emission_can_be_switched_off(self):
        self.subscribe()
        webhook_dispatch.set_emission_enabled(False)
        try:
            self.assertEqual(self.emit_and_wait(_Receiver(), timeout=0.5), [])
        finally:
            webhook_dispatch.set_emission_enabled(True)


class TheParityRowsStatedFlowTest(_Base):
    """P03's e2eFlow, verbatim: "subscribe to decision.rejected with a filter -> run a policy ->
    signed payload reaches a stub receiver". Everything else in this file supports it.

    Driven through `storage.add_decision` rather than through /decide, because that is where the
    hook is and where it has to be: /decide has a fast path AND an executor path, and a hook at
    the handler would cover whichever one the test happened to take. The fast/slow divergence
    trap was hit three separate times this cycle; this avoids a fourth by construction.
    """

    def decide(self, outcome="reject", policy_id="pol_loan"):
        return self.storage.add_decision(
            {"id": "dec_{0}".format(outcome), "tenant_id": self.tenant, "policy_id": policy_id,
             "outcome": outcome,
             # PII_SENTINEL alongside the score, because the leak assertion below needs a value
             # that CANNOT appear by chance. See test_the_payload_data_is_not_shipped.
             "payload": {"score": 410, "bureau_ref": PII_SENTINEL}, "source": "api"},
            tenant_id=self.tenant)

    def run_decision_and_wait(self, receiver, timeout=5.0, **kwargs):
        pinned, client = receiver.client_factory()
        with mock.patch.object(webhook_dispatch, "build_pinned_request", return_value=pinned()):
            with mock.patch.object(webhook_dispatch.httpx, "AsyncClient", client):
                self.decide(**kwargs)
                receiver.arrived.wait(timeout)
                for _ in range(50):
                    if receiver.received:
                        break
                    time.sleep(0.02)
        return receiver.received

    def test_subscribe_to_rejections_run_a_policy_receive_a_signed_payload(self):
        self.subscribe(event_types=["decision.rejected"],
                       filters=[{"path": "data.policy_id", "operator": "==", "value": "pol_loan"}])
        received = self.run_decision_and_wait(_Receiver())

        self.assertEqual(len(received), 1, "a rejected decision did not reach the subscriber")
        body = received[0]["content"]
        self.assertTrue(verify_hmac_signature(
            "shhh-secret", body, received[0]["headers"][webhook_dispatch.SIGNATURE_HEADER]))
        payload = json.loads(body)
        self.assertEqual(payload["type"], "decision.rejected")
        self.assertEqual(payload["data"]["outcome"], "reject")
        self.assertEqual(payload["data"]["policy_id"], "pol_loan")

    def test_the_filter_excludes_a_different_policy(self):
        self.subscribe(event_types=["decision.rejected"],
                       filters=[{"path": "data.policy_id", "operator": "==", "value": "pol_card"}])
        self.assertEqual(self.run_decision_and_wait(_Receiver(), timeout=1.0), [])

    def test_an_approval_does_not_fire_the_rejection_subscription(self):
        self.subscribe(event_types=["decision.rejected"])
        self.assertEqual(
            self.run_decision_and_wait(_Receiver(), timeout=1.0, outcome="approve"), [])

    def test_decision_completed_fires_for_every_outcome(self):
        """So a subscriber that wants everything needs one subscription, not three."""
        for outcome in ("approve", "reject", "review"):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.subscribe(event_types=["decision.completed"])
                received = self.run_decision_and_wait(_Receiver(), outcome=outcome)
                self.assertEqual(len(received), 1)
                self.assertEqual(json.loads(received[0]["content"])["data"]["outcome"], outcome)

    def test_the_payload_data_is_not_shipped_to_the_subscriber(self):
        """A subscription is a standing feed to a third party. The outcome and an id are enough
        to act on; the applicant's bureau and bank data are not the webhook's to send.

        THIS TEST USED TO BE FLAKY, and the flake was in the assertion rather than the code: it
        searched the raw body for b"410", the score in the fixture payload. Three digits is short
        enough to occur by chance -- it failed on a run whose tenant UUID was
        `9c0baaa6-1cb9-4100-83cb-...`, where "4100" contains it. Roughly a 1-in-100 false
        failure, on a SECURITY test, which is the worst place for one: a test that cries wolf on
        a leak assertion is a test people learn to re-run.

        So the leak check now uses a sentinel that cannot collide, and the structural assertion
        does the real work -- walking the parsed body for anything from the payload rather than
        grepping bytes.
        """
        self.subscribe(event_types=["decision.rejected"])
        received = self.run_decision_and_wait(_Receiver())
        body = json.loads(received[0]["content"])
        self.assertNotIn("payload", body["data"])

        # Structural: nothing anywhere in the delivered document may come from the payload.
        def _values(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from _values(value)
            elif isinstance(node, list):
                for item in node:
                    yield from _values(item)
            else:
                yield node

        delivered = list(_values(body))
        self.assertNotIn("bureau_ref", delivered, "a payload FIELD NAME reached the subscriber")
        self.assertNotIn(PII_SENTINEL, delivered, "a payload VALUE reached the subscriber")
        self.assertNotIn(410, delivered, "the score reached the subscriber")

        # And on the raw bytes, which catches a leak that structural walking would miss -- a
        # value serialised into a string somewhere. Collision-proof, unlike b"410".
        self.assertNotIn(PII_SENTINEL.encode(), received[0]["content"])

    def test_a_review_outcome_fires_its_own_type(self):
        self.subscribe(event_types=["decision.review_required"])
        received = self.run_decision_and_wait(_Receiver(), outcome="review")
        self.assertEqual(json.loads(received[0]["content"])["type"], "decision.review_required")

    def test_a_failing_subscriber_does_not_fail_the_decision(self):
        self.subscribe(event_types=["decision.rejected"])
        self.run_decision_and_wait(_Receiver(status=500))
        rows = self.storage.list_decisions(tenant_id=self.tenant)
        self.assertEqual(len(rows), 1, "a 500 from a subscriber lost the decision row")


if __name__ == "__main__":
    unittest.main()
