"""Transactional outbox + effectively-once consumption (parity P09).

P09's e2eFlow is: **kill a consumer mid-flight -> the message lands in the DLQ -> redelivery does
not double-log.** Until this module existed there was nothing to run it against. `decision_bus`
is a best-effort Redis pub/sub fan-out for the dashboard's SSE feed -- one producer, one
consumer, and a docstring saying publishing "is best-effort and never allowed to affect the
decision write path". A live view is not delivery.

The tests split along the line that matters:

  - The OUTBOX half needs no broker at all, because its guarantee is a database transaction. Those
    tests always run.
  - The DELIVERY half needs a real Kafka. Those are `skipUnless` on KAFKA_BOOTSTRAP_SERVERS,
    matching how this repo already gates its live suites (REDIS_URL, CLICKHOUSE_URL,
    S3_TEST_ENDPOINT). Start one with `docker compose --profile kafka up -d kafka`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ.setdefault("RULEMIND_CONFIG_KEY", "rulemind-test-key")

from sqlalchemy import select  # noqa: E402

from app import decision_consumer, decision_outbox  # noqa: E402
from app.models import DecisionOutbox  # noqa: E402
from app.storage import Storage  # noqa: E402

KAFKA = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "")
requires_kafka = unittest.skipUnless(
    KAFKA, "KAFKA_BOOTSTRAP_SERVERS unset — start one with: docker compose --profile kafka up -d kafka")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(path=os.path.join(self.tempdir.name, "outbox.db"))
        self.tenant = self.storage.default_tenant_id

    def tearDown(self):
        try:
            self.storage.engine.dispose()
        except Exception:  # pragma: no cover
            pass
        self.tempdir.cleanup()

    def decide(self, outcome="approve", **extra):
        payload = {"policy_id": "p1", "outcome": outcome, "payload": {"x": 1}}
        payload.update(extra)
        return self.storage.add_decision(payload, tenant_id=self.tenant)

    def outbox_rows(self):
        """Plain dicts, read INSIDE the session.

        Returning the ORM objects raises DetachedInstanceError the moment a test touches an
        attribute, because `connect()` closes the session on exit and SQLAlchemy expires the
        instances. Copying the fields out here keeps that detail in one place instead of in
        every assertion.
        """
        with self.storage.connect() as session:
            return [{
                "id": r.id, "tenant_id": r.tenant_id, "decision_id": r.decision_id,
                "partition_key": r.partition_key, "topic": r.topic, "payload": dict(r.payload or {}),
                "sent_at": r.sent_at, "attempts": r.attempts, "created_at": r.created_at,
            } for r in session.scalars(select(DecisionOutbox)).all()]


class TheOutboxIsTransactional(_Base):
    """The half that needs no broker, because the guarantee is a transaction."""

    def test_writing_a_decision_enqueues_exactly_one_event(self):
        decision = self.decide()
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1, "a decision did not produce exactly one outbox row")
        self.assertEqual(rows[0]["decision_id"], decision["id"])
        self.assertIsNone(rows[0]["sent_at"], "the row was marked sent before any relay ran")

    def test_the_row_commits_WITH_the_decision_not_after_it(self):
        """The property the whole design rests on.

        If the outbox write happened after the decision's transaction, there would be a window
        where a decision exists and its event does not -- and a crash in that window loses the
        event permanently, with nothing to detect it. Same transaction means the count of
        decisions and the count of outbox rows can never diverge.
        """
        for _ in range(5):
            self.decide()
        decisions = self.storage.list_decisions(tenant_id=self.tenant)
        self.assertEqual(len(self.outbox_rows()), len(decisions),
                         "decisions and outbox rows diverged, so they are not in one transaction")

    def test_the_event_does_not_carry_decrypted_pii(self):
        """A Kafka topic is a different trust boundary from the database: retained for days,
        replayable by any consumer group, readable by more systems. storage._decision_to_dict
        DECRYPTS payload_preview, computed_variables, rule_results and trace, so publishing the
        decision dict verbatim would put applicant data on the bus."""
        self.decide(payload={"ssn": "AAAA-BB-CCCC", "salary": 1234567})
        row = self.outbox_rows()[0]
        body = json.dumps(row["payload"])
        self.assertNotIn("AAAA-BB-CCCC", body)
        self.assertNotIn("1234567", body)
        for banned in ("payload_preview", "computed_variables", "rule_results", "trace"):
            self.assertNotIn(banned, row["payload"], "%s must not be published" % banned)
        # ...while still carrying enough to be useful.
        self.assertEqual(row["payload"]["tenant_id"], self.tenant)
        self.assertIn("outcome", row["payload"])

    def test_the_partition_key_is_the_tenant(self):
        """Kafka orders within a partition and never across. Keying by tenant keeps one
        workspace's decisions ordered while letting different workspaces parallelise."""
        self.decide()
        self.assertEqual(self.outbox_rows()[0]["partition_key"], self.tenant)

    def test_pending_returns_oldest_first(self):
        for _ in range(3):
            self.decide()
        with self.storage.connect() as session:
            created = [r.created_at for r in decision_outbox.pending(session)]
        self.assertEqual(created, sorted(created), "the relay would publish out of order")

    def test_the_relay_is_a_no_op_without_a_broker(self):
        """Enqueue is unconditional; DELIVERY is opt-in. Turning Kafka off must cost delivery,
        never data -- the same degradation decision_bus chose for Redis."""
        self.decide()
        with unittest.mock.patch.dict(os.environ, {"KAFKA_BOOTSTRAP_SERVERS": ""}, clear=False):
            result = decision_outbox.relay_once(self.storage)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(self.outbox_rows()), 1, "the row was dropped when no broker existed")


class DedupeIsTheDatabase(_Base):
    """Effectively-once rests on the claim, not on the check before it."""

    def test_claiming_an_event_twice_fails_the_second_time(self):
        event = str(uuid.uuid4())
        self.assertTrue(decision_consumer.claim(self.storage, self.tenant, event))
        self.assertFalse(decision_consumer.claim(self.storage, self.tenant, event),
                         "the same event was claimed twice — redelivery would double-process")

    def test_two_tenants_may_claim_the_same_event_id(self):
        """idempotency_records is keyed per tenant. A global key would let one workspace's event
        id block another's, and would leak the existence of one tenant's traffic to another."""
        event = str(uuid.uuid4())
        other = self.storage.create_tenant("Other")["id"]
        self.assertTrue(decision_consumer.claim(self.storage, self.tenant, event))
        self.assertTrue(decision_consumer.claim(self.storage, other, event))

    def test_a_redelivered_message_is_reported_as_a_duplicate_not_processed(self):
        raw = json.dumps({"event_id": str(uuid.uuid4()), "tenant_id": self.tenant,
                          "decision_id": "d1", "payload": {}}).encode()
        seen = []
        self.assertEqual(decision_consumer.handle_message(self.storage, raw, handler=seen.append), "processed")
        self.assertEqual(decision_consumer.handle_message(self.storage, raw, handler=seen.append), "duplicate")
        self.assertEqual(len(seen), 1, "the handler ran twice for one event — that is a double-log")

    def test_malformed_json_is_a_dead_letter_not_an_exception(self):
        """A poison message must not raise out of the loop. If it did, the consumer would die,
        restart, poll the same uncommitted offset, and die again — one bad event stopping every
        later decision on that partition."""
        self.assertEqual(decision_consumer.handle_message(self.storage, b"{not json"), "dead-letter")

    def test_a_message_missing_its_ids_is_a_dead_letter(self):
        raw = json.dumps({"payload": {}}).encode()
        self.assertEqual(decision_consumer.handle_message(self.storage, raw), "dead-letter")

    def test_a_handler_that_raises_dead_letters_rather_than_propagating(self):
        raw = json.dumps({"event_id": str(uuid.uuid4()), "tenant_id": self.tenant,
                          "decision_id": "d1", "payload": {}}).encode()

        def boom(_envelope):
            raise RuntimeError("downstream is down")

        self.assertEqual(decision_consumer.handle_message(self.storage, raw, handler=boom), "dead-letter")

    def test_a_dead_lettered_event_is_audited_with_an_actor(self):
        """DoD item 3, and the one consumer outcome that deserves an audit row.

        A processed event already has its Decision row; a duplicate is the pipeline behaving as
        designed. A PARKED event is different: a decision exists in the database that no
        downstream consumer will ever hear about, and nothing else records that divergence.

        The actor is `system:decision-consumer`, written explicitly rather than left null. A
        consumer runs on the scheduler with no request and no caller, so there is no
        authenticated actor to record — and a null would leave a reader unable to tell whether
        the actor was lost or never existed.
        """
        raw = json.dumps({"event_id": str(uuid.uuid4()), "tenant_id": self.tenant,
                          "decision_id": "d1", "payload": {}}).encode()

        def boom(_envelope):
            raise RuntimeError("downstream is down")

        self.assertEqual(decision_consumer.handle_message(self.storage, raw, handler=boom),
                         "dead-letter")

        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                  if e.get("event_type") == "decision_event_dead_lettered"]
        self.assertTrue(events, "parking an event wrote no audit row")
        metadata = events[0].get("metadata") or {}
        self.assertEqual(metadata.get("actor_role"), "system:decision-consumer")
        self.assertIn("RuntimeError", str(metadata.get("reason")),
                      "the audit row does not say WHY it was parked")

    def test_a_successfully_processed_event_is_NOT_audited(self):
        """The counterweight. Auditing every consumed event would write one audit row per
        decision — doubling the audit table to record that the pipeline worked."""
        raw = json.dumps({"event_id": str(uuid.uuid4()), "tenant_id": self.tenant,
                          "decision_id": "d1", "payload": {}}).encode()
        self.assertEqual(decision_consumer.handle_message(self.storage, raw), "processed")
        events = [e for e in self.storage.list_audit_events(tenant_id=self.tenant)
                  if e.get("event_type") == "decision_event_dead_lettered"]
        self.assertEqual(events, [], "a healthy event was audited as dead-lettered")

    def test_the_dedupe_key_cannot_collide_with_an_api_idempotency_key(self):
        """Consumer events and caller-supplied Idempotency-Key values share one column. A caller
        who sends the header `Idempotency-Key: <some event id>` must not be able to suppress a
        decision event, or vice versa."""
        key = decision_consumer._dedupe_key("abc")
        self.assertTrue(key.startswith("kafka:decision:"))
        self.assertNotEqual(key, "abc")


@requires_kafka
class RoundTripThroughARealBroker(_Base):
    """The e2eFlow from the parity row, against a real broker."""

    def setUp(self):
        super().setUp()
        # A unique topic and group per test run: a shared one would make these tests depend on
        # the order they ran in, and on whatever a previous run left behind.
        self.topic = "rulemind.test.decisions." + uuid.uuid4().hex[:8]
        self._env = unittest.mock.patch.dict(os.environ, {
            "RULEMIND_KAFKA_TOPIC": self.topic,
            "RULEMIND_KAFKA_GROUP": "test-" + uuid.uuid4().hex[:8],
            "RULEMIND_KAFKA_DLQ_TOPIC": self.topic + ".dlq",
        }, clear=False)
        self._env.start()
        decision_outbox.reset_producer()

    def tearDown(self):
        self._env.stop()
        decision_outbox.reset_producer()
        super().tearDown()

    def test_the_relay_sends_and_stamps_only_what_the_broker_acknowledged(self):
        self.decide()
        result = decision_outbox.relay_once(self.storage)
        self.assertEqual(result["sent"], 1, result)
        self.assertEqual(result["failed"], 0, result)
        self.assertIsNotNone(self.outbox_rows()[0]["sent_at"], "an acknowledged row was not stamped")

    def test_a_rejected_row_is_NOT_stamped_sent(self):
        """The at-most-once failure, which every other test here is blind to.

        A mutation that stamps every row regardless of the delivery report survived the whole
        suite: on a healthy broker nothing is rejected, so "stamped after the ack" and "stamped
        always" are indistinguishable. They differ only when the produce fails -- and that is
        exactly when it matters, because a row stamped sent is never retried. The event is gone,
        silently, with the decision still in the database.

        The failure is forced by pointing the producer at a port with nothing on it, which is
        what a broker outage looks like to librdkafka.
        """
        self.decide()
        decision_outbox.reset_producer()
        with unittest.mock.patch.dict(os.environ, {"KAFKA_BOOTSTRAP_SERVERS": "127.0.0.1:9"}, clear=False):
            result = decision_outbox.relay_once(self.storage)
        decision_outbox.reset_producer()

        self.assertEqual(result["sent"], 0, "a row was reported sent while the broker was down")
        self.assertEqual(result["failed"], 1, result)
        row = self.outbox_rows()[0]
        self.assertIsNone(row["sent_at"],
                          "the row was stamped sent despite no acknowledgement — the event is "
                          "now lost, because nothing will ever retry it")
        self.assertEqual(row["attempts"], 1, "a failed attempt was not recorded")

    def test_a_relayed_event_is_consumed_exactly_once_even_when_redelivered(self):
        """Kill a consumer mid-flight -> redelivery does not double-log.

        The kill is simulated by processing the same bytes twice, which is precisely what a
        consumer that dies before committing its offset causes on restart. Asserting on the
        handler call count is the real check -- 'the message arrived twice' is expected and fine;
        'the effect happened twice' is the bug.
        """
        self.decide()
        decision_outbox.relay_once(self.storage)

        seen = []
        first = decision_consumer.consume_once(self.storage, max_messages=10, timeout=5.0,
                                               handler=seen.append)
        self.assertEqual(first["processed"], 1, first)

        # The same event again, as an uncommitted offset would redeliver it.
        with self.storage.connect() as session:
            row = session.scalars(select(DecisionOutbox)).first()
            raw = decision_outbox.serialize(row)
        self.assertEqual(decision_consumer.handle_message(self.storage, raw, handler=seen.append),
                         "duplicate")
        self.assertEqual(len(seen), 1, "the effect happened twice — this is the double-log P09 forbids")

    def test_a_poison_message_reaches_the_dead_letter_topic(self):
        from confluent_kafka import Producer

        producer = Producer({"bootstrap.servers": KAFKA})
        producer.produce(topic=self.topic, value=b"{definitely not json")
        producer.flush(10)

        stats = decision_consumer.consume_once(self.storage, max_messages=10, timeout=5.0)
        self.assertEqual(stats["dead-letter"], 1, stats)

        parked = decision_consumer.dead_letters(self.storage, limit=5)
        self.assertTrue(parked, "the poison message never reached the DLQ")

    def test_the_partition_keeps_moving_after_a_poison_message(self):
        """The reason a DLQ exists at all. A poison message whose offset is never committed is
        redelivered forever, and every decision behind it on that partition stops."""
        from confluent_kafka import Producer

        producer = Producer({"bootstrap.servers": KAFKA})
        producer.produce(topic=self.topic, value=b"{broken")
        producer.flush(10)
        self.decide()
        decision_outbox.relay_once(self.storage)

        stats = decision_consumer.consume_once(self.storage, max_messages=10, timeout=5.0)
        self.assertEqual(stats["dead-letter"], 1, stats)
        self.assertEqual(stats["processed"], 1,
                         "the good decision behind the poison message never got through")


if __name__ == "__main__":
    unittest.main()
