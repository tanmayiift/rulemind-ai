"""Transactional outbox for decision events — the durable half of parity P09.

WHAT THIS IS NOT. `decision_bus.py` is a best-effort Redis pub/sub fan-out for the dashboard's
live SSE feed; its own docstring says publishing "is best-effort and never allowed to affect the
decision write path", and it has one producer and one consumer. That is a *view*, not delivery.
Nothing in this codebase durably delivered a decision anywhere until this module existed, which
is why P09's e2eFlow -- kill a consumer mid-flight, the message lands in the DLQ, redelivery does
not double-log -- had nothing to run against.

THE PROBLEM. A decision is written to Postgres and published to Kafka. Two systems, so there is
no commit covering both:

  - write then publish  -> the process dies in between and the message is lost forever
  - publish then write  -> the process dies in between and a decision is announced that never
                           existed, which is worse

THE SHAPE OF THE FIX, in three parts, of which only the first two live here:

  1. `enqueue()` inserts an outbox row in the SAME session as the Decision (see
     storage.add_decision). They commit together or neither does. Nothing is published yet.
  2. `relay_once()` moves committed rows to Kafka with an idempotent producer and stamps
     sent_at. This is at-least-once: a crash after the produce but before the stamp re-sends.
  3. The CONSUMER deduplicates on the outbox id, which is what turns at-least-once into
     effectively-once. That lives in decision_consumer.py.

A NOTE ON "EXACTLY-ONCE". Kafka's transactional producer gives exactly-once within Kafka --
read-process-write between topics. It cannot extend that guarantee to Postgres, because Postgres
is not a participant in the Kafka transaction. Anyone claiming end-to-end exactly-once across two
stores without a distributed transaction is describing at-least-once delivery plus idempotent
consumption. That is what this is, and it is the correct pattern; the naming here says so rather
than borrowing a stronger word.

WITHOUT KAFKA CONFIGURED this module still enqueues. Rows accumulate unsent and the relay never
runs, so turning the broker off costs delivery and never data -- the same degradation
`decision_bus` chose for Redis.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DecisionOutbox

LOGGER = logging.getLogger(__name__)

DEFAULT_TOPIC = "rulemind.decisions"

# Bounded so one relay pass cannot load an unbounded backlog into memory. A pass that hits the
# cap simply runs again -- the loop is driven by the scheduler, not by draining to empty.
DEFAULT_BATCH = 100


def topic() -> str:
    return os.getenv("RULEMIND_KAFKA_TOPIC", DEFAULT_TOPIC)


def bootstrap_servers() -> str:
    return os.getenv("KAFKA_BOOTSTRAP_SERVERS", "")


def enabled() -> bool:
    """Delivery is opt-in. Enqueue is not — see the note at the bottom of the module docstring."""
    return bool(bootstrap_servers())


def event_payload(decision: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
    """The published event.

    Deliberately NOT the whole decision. `payload_preview`, `computed_variables`, `rule_results`
    and `trace` are decrypted PII (see storage._decision_to_dict) and a Kafka topic is a
    different trust boundary from the database -- it is retained for days, replayable by any
    consumer group, and typically readable by more systems than the DB is. A consumer that needs
    the full context can read it back by id, through the API that already authorises for it.
    """
    return {
        "decision_id": decision.get("id"),
        # Passed in, NOT read from the decision dict: storage._decision_to_dict does not include
        # tenant_id. Reading it from there yielded `"tenant_id": None` on every event, and
        # decision_consumer.handle_message dead-letters a message with no tenant -- so the entire
        # stream would have gone to the DLQ. Caught by the PII test asserting the field is
        # present, which is the sort of thing a happy-path test never looks at.
        "tenant_id": tenant_id,
        "policy_id": decision.get("policy_id"),
        "outcome": decision.get("outcome"),
        "source": decision.get("source"),
        "latency_ms": decision.get("latency_ms"),
        "experiment_id": decision.get("experiment_id"),
        "experiment_variant": decision.get("experiment_variant"),
        "created_at": str(decision.get("created_at") or ""),
    }


def enqueue(session: Session, tenant_id: str, decision: Dict[str, Any]) -> Optional[str]:
    """Insert the outbox row INSIDE the caller's transaction. Never commits on its own.

    Takes a `session` rather than opening one precisely so it joins the decision's transaction.
    Opening a second session here would reintroduce the two-phase problem this exists to solve.
    """
    try:
        row = DecisionOutbox(
            tenant_id=tenant_id,
            decision_id=str(decision.get("id") or ""),
            # Per-tenant key: Kafka orders within a partition and never across, so this keeps one
            # tenant's decisions in order while letting different tenants proceed in parallel.
            partition_key=str(tenant_id),
            topic=topic(),
            payload=event_payload(decision, tenant_id),
            created_at=datetime.utcnow(),
        )
        session.add(row)
        return row.id
    except Exception:  # noqa: BLE001 - a broken outbox must never cost the caller their decision
        # Logged at ERROR, not swallowed silently: unlike the SSE fan-out, losing this row means
        # the decision reaches no consumer, ever.
        LOGGER.exception("failed to enqueue decision %s to the outbox", decision.get("id"))
        return None


def pending(session: Session, limit: int = DEFAULT_BATCH) -> List[DecisionOutbox]:
    """Oldest unsent rows first — order matters, because per-tenant ordering is a guarantee."""
    return list(session.scalars(
        select(DecisionOutbox)
        .where(DecisionOutbox.sent_at.is_(None))
        .order_by(DecisionOutbox.created_at)
        .limit(limit)
    ).all())


def mark_sent(session: Session, row: DecisionOutbox) -> None:
    row.sent_at = datetime.utcnow()


def mark_failed(session: Session, row: DecisionOutbox, error: str) -> None:
    row.attempts = (row.attempts or 0) + 1
    # Truncated: a driver traceback can be kilobytes, and this column is read by humans.
    row.last_error = str(error)[:2000]


def unsent_count(storage: Any) -> int:
    from sqlalchemy import func

    with storage.connect() as session:
        return int(session.scalar(
            select(func.count()).select_from(DecisionOutbox).where(DecisionOutbox.sent_at.is_(None))
        ) or 0)


def serialize(row: DecisionOutbox) -> bytes:
    envelope = {
        # The idempotency key the consumer deduplicates on. The OUTBOX id, not the decision id:
        # a decision could legitimately be re-enqueued (a replay, a backfill) and those are
        # distinct events, whereas a redelivered outbox row is the same event twice.
        "event_id": row.id,
        "decision_id": row.decision_id,
        "tenant_id": row.tenant_id,
        "payload": row.payload,
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- the relay (J20)
#
# Moves committed outbox rows to Kafka. Runs on the scheduler, leader-gated, so N replicas do not
# all relay the same rows -- ordering within a tenant is a guarantee, and two relays racing the
# same partition would break it.

_PRODUCER = None


def _producer():
    """One idempotent producer per process, created lazily.

    `enable.idempotence` is the setting that matters and it is not a default. Without it a
    producer that retries an un-acked send writes the message twice INSIDE Kafka, so a consumer
    sees a duplicate that no amount of outbox bookkeeping can detect -- the outbox row was sent
    once as far as this process knows. With it, the broker assigns a sequence number per
    producer per partition and drops the retry itself.
    """
    global _PRODUCER
    if _PRODUCER is not None:
        return _PRODUCER
    from confluent_kafka import Producer  # lazy: no broker, no import cost

    _PRODUCER = Producer({
        "bootstrap.servers": bootstrap_servers(),
        # The three that make a produce durable. acks=all alone is not enough: without
        # idempotence a retry duplicates, and with retries=0 a transient leader election loses
        # the message outright.
        "enable.idempotence": True,
        "acks": "all",
        "retries": 5,
        # Bounded so a broker outage surfaces as a failed relay pass -- which retries from the
        # outbox, where the row is still durable -- rather than as an in-memory queue growing
        # until the API process runs out of memory.
        "message.timeout.ms": 15000,
        "linger.ms": 5,
        "client.id": os.getenv("RULEMIND_KAFKA_CLIENT_ID", "rulemind-relay"),
    })
    return _PRODUCER


def reset_producer() -> None:
    """Drop the cached producer. For tests, and for a config change at runtime."""
    global _PRODUCER
    _PRODUCER = None


def relay_once(storage: Any, limit: int = DEFAULT_BATCH) -> Dict[str, int]:
    """One pass: take pending rows, produce them, stamp the ones the broker acknowledged.

    THE ORDER HERE IS THE CORRECTNESS. `flush()` is awaited BEFORE anything is marked sent, and
    only rows whose delivery callback fired without an error are stamped. Marking first would
    make this at-most-once -- a broker rejection after the stamp would drop the event silently,
    which is the exact failure the outbox exists to prevent.

    A crash between the produce and the stamp re-sends on the next pass. That is deliberate:
    at-least-once is recoverable and the consumer deduplicates on event_id, whereas at-most-once
    is not recoverable by anyone.
    """
    result = {"sent": 0, "failed": 0, "pending": 0}
    if not enabled():
        return result

    with storage.connect() as session:
        rows = pending(session, limit=limit)
        result["pending"] = len(rows)
        if not rows:
            return result

        producer = _producer()
        delivered: Dict[str, Optional[str]] = {}

        def _on_delivery(err, msg, _rid=None):
            delivered[_rid] = str(err) if err is not None else None

        for row in rows:
            try:
                producer.produce(
                    topic=row.topic or topic(),
                    key=(row.partition_key or "").encode("utf-8"),
                    value=serialize(row),
                    # functools.partial rather than a closure over `row`: a closure would
                    # capture the loop variable and every callback would report the last row.
                    on_delivery=lambda err, msg, _rid=row.id: _on_delivery(err, msg, _rid),
                )
            except Exception as exc:  # noqa: BLE001 - a full local queue is not fatal
                delivered[row.id] = str(exc)

        # Block until the broker has answered for everything queued in this pass.
        producer.flush(20)

        for row in rows:
            err = delivered.get(row.id, "no delivery report")
            if err is None:
                mark_sent(session, row)
                result["sent"] += 1
            else:
                mark_failed(session, row, err)
                result["failed"] += 1
                LOGGER.warning("outbox row %s failed to reach kafka: %s", row.id, err)
    return result
