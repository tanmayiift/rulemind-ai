"""Kafka consumer for decision events — effectively-once processing (parity P09).

THE GUARANTEE, STATED HONESTLY. The relay in decision_outbox.py is at-least-once: a crash
between a successful produce and the sent_at stamp re-sends. This module is what makes that
safe, by deduplicating on the event id before doing any work. Together they are
**at-least-once delivery plus idempotent consumption**, which is what "exactly-once" means
everywhere outside a single Kafka read-process-write transaction.

It is worth being precise rather than borrowing the stronger phrase. Kafka's transactional
producer CAN give exactly-once, but only for Kafka-to-Kafka: the transaction covers the produce
and the offset commit because both live in Kafka. Postgres cannot join that transaction, so the
moment a consumer writes to the database the guarantee becomes "the effect happens once", not
"the message is delivered once". This module delivers the former and says so.

THE THREE THINGS THAT MAKE IT WORK:

  1. `enable.auto.commit` is OFF. The default commits offsets on a timer, which acknowledges
     messages the handler may never have processed -- a rebalance or a crash then skips them
     permanently. Offsets are committed HERE, after the handler returns.
  2. The dedupe write and the offset commit are ordered so a failure repeats work rather than
     skipping it. Committing first would make this at-most-once.
  3. A message the handler cannot process goes to the DEAD LETTER TOPIC and its offset IS
     committed. Without that a poison message blocks its partition forever: every consumer in
     the group picks it up, fails, does not commit, and redelivers it to itself. One malformed
     event stops every later decision for that tenant.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from .models import IdempotencyRecord

LOGGER = logging.getLogger(__name__)

DEFAULT_GROUP = "rulemind-decisions"


def group_id() -> str:
    return os.getenv("RULEMIND_KAFKA_GROUP", DEFAULT_GROUP)


def dead_letter_topic() -> str:
    from . import decision_outbox

    return os.getenv("RULEMIND_KAFKA_DLQ_TOPIC", decision_outbox.topic() + ".dlq")


def already_processed(storage: Any, tenant_id: str, event_id: str) -> bool:
    """Has this event already been handled?

    Reuses `idempotency_records` rather than adding a table. That entity already means exactly
    this -- "an operation with this key, for this tenant, has been performed" -- and it already
    carries the (tenant_id, key) unique constraint that makes the claim below atomic. A second
    table would be a second thing to keep correct for the same invariant.
    """
    from sqlalchemy import select

    with storage.connect() as session:
        return session.scalar(
            select(IdempotencyRecord.id)
            .where(IdempotencyRecord.tenant_id == tenant_id,
                   IdempotencyRecord.key == _dedupe_key(event_id))
            .limit(1)
        ) is not None


def _dedupe_key(event_id: str) -> str:
    # Namespaced so a consumer event can never collide with a caller-supplied Idempotency-Key
    # for an API request. Both live in the same column.
    return "kafka:decision:{0}".format(event_id)


def claim(storage: Any, tenant_id: str, event_id: str) -> bool:
    """Atomically claim an event. True if this consumer won and should process it.

    The CLAIM is the dedupe, not the check above. `already_processed` races -- two consumers can
    both read "no" for the same event and both proceed. The unique constraint on
    (tenant_id, key) is what actually decides, and losing the insert is the signal that someone
    else has it. Same shape as the fix in Storage.seed_if_empty: let the database arbitrate.
    """
    from sqlalchemy.exc import IntegrityError

    try:
        with storage.connect() as session:
            session.add(IdempotencyRecord(
                tenant_id=tenant_id,
                key=_dedupe_key(event_id),
                # Not a request body, so there is nothing to hash for the replay-with-a-different-
                # body check. The event id is already unique per event.
                request_hash=event_id,
            ))
        return True
    except IntegrityError:
        return False


def handle_message(storage: Any, raw: bytes, handler: Optional[Callable] = None) -> str:
    """Process one message. Returns 'processed', 'duplicate', or 'dead-letter'.

    Never raises for bad input: a malformed message is a dead letter, not an exception that
    stops the loop. That distinction is the difference between one bad event and a stalled
    partition.
    """
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - unparseable IS the dead-letter case
        LOGGER.warning("decision event is not valid JSON; dead-lettering")
        # No tenant to scope the audit to -- an unparseable body has no fields at all. Logged
        # above and parked below; there is nothing truthful to write to a tenant's audit trail.
        return "dead-letter"

    event_id = envelope.get("event_id")
    tenant_id = envelope.get("tenant_id")
    if not event_id or not tenant_id:
        LOGGER.warning("decision event missing event_id/tenant_id; dead-lettering")
        return "dead-letter"

    if not claim(storage, tenant_id, event_id):
        # Not an error. This is the redelivery the relay is allowed to produce, and dropping it
        # here is precisely what makes the pipeline effectively-once.
        return "duplicate"

    if handler is not None:
        try:
            handler(envelope)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("decision event handler failed for %s; dead-lettering", event_id)
            _audit_dead_letter(storage, tenant_id, event_id,
                               "handler raised {0}".format(type(exc).__name__))
            return "dead-letter"
    return "processed"


def consume_once(storage: Any, max_messages: int = 100, timeout: float = 1.0,
                 handler: Optional[Callable] = None) -> Dict[str, int]:
    """Poll, process, then commit — in that order, deliberately.

    A single bounded pass rather than a `while True`, so it can be driven by the scheduler, and
    so a test can run exactly one and assert on the result.
    """
    from . import decision_outbox

    stats = {"processed": 0, "duplicate": 0, "dead-letter": 0, "polled": 0}
    if not decision_outbox.enabled():
        return stats

    from confluent_kafka import Consumer

    consumer = Consumer({
        "bootstrap.servers": decision_outbox.bootstrap_servers(),
        "group.id": group_id(),
        # Start at the beginning for a brand-new group: the alternative ('latest') silently
        # skips everything produced before the consumer first ran, which during a deploy is
        # every decision made in the gap.
        "auto.offset.reset": "earliest",
        # THE setting. See the module docstring: auto-commit acknowledges messages the handler
        # may never have seen.
        "enable.auto.commit": False,
    })
    consumer.subscribe([decision_outbox.topic()])
    try:
        for _ in range(max_messages):
            msg = consumer.poll(timeout)
            if msg is None:
                break
            if msg.error():
                LOGGER.warning("kafka consumer error: %s", msg.error())
                break
            stats["polled"] += 1
            outcome = handle_message(storage, msg.value(), handler=handler)
            stats[outcome] += 1
            if outcome == "dead-letter":
                _to_dead_letter(msg.value())
            # Committed AFTER the handler, including for a dead letter -- the message is now
            # safely in the DLQ, and not committing it would redeliver the poison forever.
            consumer.commit(message=msg, asynchronous=False)
    finally:
        consumer.close()
    return stats


def _audit_dead_letter(storage: Any, tenant_id: Optional[str], event_id: Optional[str],
                       reason: str) -> None:
    """Record that an event was parked, with the actor.

    THE ACTOR HERE IS THE SYSTEM, NOT A PERSON, and that is the point rather than a gap. Every
    other audited action in this codebase carries `actor_api_key_id` / `actor_role` from the
    authenticated request; a consumer runs on the scheduler with no request and no caller. Naming
    it `system:decision-consumer` says so explicitly instead of writing a null and leaving a
    reader to guess whether the actor was lost or never existed.

    Dead-lettering is the one consumer outcome worth auditing. A processed event already has its
    Decision row, and a duplicate is the pipeline working as designed -- but a parked event means
    a decision exists in the database that no downstream consumer will ever hear about, and
    nothing else in the system records that divergence.
    """
    try:
        storage.add_audit_event(
            {"tenant_id": tenant_id, "event_type": "decision_event_dead_lettered",
             "entity_type": "decision_event", "entity_id": event_id or "unknown",
             "detail": "Decision event parked in the dead-letter topic: {0}.".format(reason),
             "metadata": {"actor_api_key_id": None, "actor_role": "system:decision-consumer",
                          "reason": reason, "dlq_topic": dead_letter_topic()}},
            tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 - audit is best effort; it must not block the DLQ write
        LOGGER.exception("could not audit the dead-lettered event %s", event_id)


def _to_dead_letter(raw: bytes) -> None:
    """Park a message nothing can process, so its partition keeps moving.

    Best-effort by necessity: if the broker is unreachable there is nowhere to park it. The
    offset is still committed by the caller, because the alternative -- redelivering a message
    that has already failed -- blocks the partition indefinitely. The message is logged in full
    first so it is recoverable from logs when the topic is not.
    """
    from . import decision_outbox

    try:
        producer = decision_outbox._producer()
        producer.produce(topic=dead_letter_topic(), value=raw)
        producer.flush(10)
    except Exception:  # noqa: BLE001
        LOGGER.error("could not write to the dead-letter topic; message body: %s",
                     raw.decode("utf-8", "replace")[:2000])


def dead_letters(storage: Any, limit: int = 100) -> List[Dict[str, Any]]:
    """Read the dead-letter topic from the beginning, without joining the live consumer group.

    A distinct group id on purpose: reading the DLQ must not move the real consumer's offsets.
    """
    from . import decision_outbox

    if not decision_outbox.enabled():
        return []
    from confluent_kafka import Consumer

    consumer = Consumer({
        "bootstrap.servers": decision_outbox.bootstrap_servers(),
        "group.id": group_id() + ".dlq-reader",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([dead_letter_topic()])
    out: List[Dict[str, Any]] = []
    try:
        for _ in range(limit):
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                break
            try:
                out.append(json.loads(msg.value().decode("utf-8")))
            except Exception:  # noqa: BLE001 - an unparseable body is exactly what lands here
                out.append({"raw": msg.value().decode("utf-8", "replace")[:2000]})
    finally:
        consumer.close()
    return out
