"""Outbound webhooks: subscriptions, event filtering, signed delivery (parity P03).

WHAT EXISTED BEFORE, AND WHY IT IS NOT THIS. `app/webhooks.py` is INBOUND -- an `endpoint_path`
a partner POSTs to in order to trigger a policy. The only outbound call in the product was the
ad-hoc `alertUrl` on a monitor step (`executor._execute_monitor`): one URL, hard-wired into one
step of one policy, fired once, best-effort, with the exception swallowed into
`result["alertError"]`. That is a notification, not a subscription. Nothing could say "tell this
URL whenever ANY policy in this workspace is promoted", and nothing retried.

A subscription is the opposite shape in all four ways: it belongs to the workspace rather than to
a step, it selects events by TYPE and by FILTER rather than being one call site, it is signed so
the receiver can verify the sender, and a failed delivery is retried and then dead-lettered
rather than dropped.

DESIGN NOTES

* Filters go through `logic.compare` -- the operator set the five engines are held to by the
  parity corpus, and the same one the rule-condition builder emits. A subscription filter reading
  `outcome == "reject"` has to mean what it means in a rule, and a second comparison
  implementation here would be a second set of semantics to keep in step.

* Signing reuses `auth.hmac_signature`, which is exactly what `webhooks.py` VERIFIES on the way
  in. One scheme in both directions means a partner integrating both ways writes one
  implementation, and it means the signature format cannot drift between them.

* Delivery goes through the SSRF-guarded egress path. A subscription URL is author-supplied and
  the request carries workspace data, so it is the same threat as a connector fetch: resolve,
  range-check, pin to the vetted IP, and never follow a redirect (a 3xx would connect to a host
  that never passed `vet_url`).

* Delivery is BEST EFFORT FOR THE DECISION and durable for the event. (This bullet was written
  before it was true: until 2026-09-24 "durable" meant a daemon thread and last-known state.
  It is now the `webhook_deliveries` table + a scheduler drain -- see "emission" below.) A webhook that is slow or
  down must never change or delay a decision -- so nothing here is awaited on the decision path;
  failures are recorded, retried by the scheduler, and dead-lettered after a bound. That is the
  opposite of `connector_fetch`, which fails a decision CLOSED, and the difference is the
  direction of the dependency: a decision NEEDS the connector's answer and merely ANNOUNCES to a
  subscriber.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from . import env
from .auth import hmac_signature
from .egress import EgressBlocked, build_pinned_request, clamp_retries, clamp_timeout_ms
from .jsonpath import resolve_jsonpath
from .logic import compare

logger = logging.getLogger("rulemind.webhook_dispatch")


def _now_iso() -> str:
    from .logic import now_iso

    return now_iso()

# The event types a subscription may select. Deliberately a closed list rather than "any string":
# a typo in `event_types` otherwise produces a subscription that silently never fires, which is
# indistinguishable from a working one until someone notices the absence of alerts.
#
# Every name here is emitted somewhere in the backend today -- they are the existing audit event
# types plus the decision lifecycle, not an aspirational taxonomy.
EVENT_TYPES = (
    "decision.completed",
    "decision.rejected",
    "decision.review_required",
    "policy_lifecycle_changed",
    "experiment_promoted",
    "review_completed",
    "slo_breach",
    "cron_executed",
    "report_delivered",
    "webhook_triggered",
    "sdk_execution_synced",
    "login_failed_repeatedly",
    "monitor_scheduled",
)

MAX_SUBSCRIPTIONS_PER_TENANT = env.env_int("RULEMIND_WEBHOOK_MAX_SUBSCRIPTIONS", 50)
SIGNATURE_HEADER = "x-rulemind-signature"
EVENT_HEADER = "x-rulemind-event"
DELIVERY_HEADER = "x-rulemind-delivery"


class SubscriptionInvalid(ValueError):
    """A subscription that cannot be stored as written. Raised at authoring time, on purpose:
    the alternative is a row that accepts everything and delivers nothing."""


def validate_event_types(raw: Any) -> List[str]:
    if not isinstance(raw, list) or not raw:
        raise SubscriptionInvalid(
            "event_types must be a non-empty list; choose from: {0}".format(", ".join(EVENT_TYPES)))
    cleaned = []
    for item in raw:
        name = str(item).strip()
        if name not in EVENT_TYPES:
            raise SubscriptionInvalid(
                "unknown event type {0!r}; choose from: {1}".format(name, ", ".join(EVENT_TYPES)))
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def validate_filters(raw: Any) -> List[Dict[str, Any]]:
    """Filters are ANDed. An empty list means "every event of the selected types"."""
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise SubscriptionInvalid("filters must be a list of {path, operator, value} objects")
    cleaned: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SubscriptionInvalid("each filter must be an object")
        path = str(item.get("path") or item.get("field") or "").strip()
        if not path:
            raise SubscriptionInvalid("each filter needs a `path`")
        operator = str(item.get("operator") or item.get("op") or "==").strip()
        cleaned.append({"path": path, "operator": operator,
                        "value": item.get("value"), "value2": item.get("value2"),
                        "type": item.get("type")})
    return cleaned


def event_matches(subscription: Dict[str, Any], event: Dict[str, Any]) -> bool:
    """Does this event belong to this subscription?

    Fails CLOSED in the delivery sense -- an unparseable filter means "do not deliver" rather than
    "deliver everything". Over-delivery is the worse failure here: these payloads carry decision
    data to a third party, so a filter nobody can evaluate must not become a firehose.
    """
    if not subscription.get("is_active", True):
        return False
    if str(event.get("type") or "") not in (subscription.get("event_types") or []):
        return False
    for condition in subscription.get("filters") or []:
        if not isinstance(condition, dict):
            return False
        path = str(condition.get("path") or "")
        if not path:
            return False
        actual = resolve_jsonpath(path if path.startswith("$.") else "$." + path.lstrip("."), event)
        try:
            if not compare(actual, str(condition.get("operator") or "=="),
                           condition.get("value"), condition.get("value2"), condition.get("type")):
                return False
        except Exception:  # pragma: no cover - a bad operator must not become a firehose
            return False
    return True


def build_request(subscription: Dict[str, Any], event: Dict[str, Any],
                  delivery_id: str) -> Dict[str, Any]:
    """The signed HTTP request for one delivery.

    The signature is over the EXACT bytes sent, computed once and reused, because signing a
    re-serialised copy is how a receiver ends up rejecting a valid message: `json.dumps` is not
    guaranteed to reproduce byte-identical output across calls once ordering or separators differ.
    """
    body = json.dumps({
        "id": delivery_id,
        "type": event.get("type"),
        "created_at": event.get("created_at"),
        "tenant_id": event.get("tenant_id"),
        "data": event.get("data") or {},
    }, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")
    headers = {
        "content-type": "application/json",
        EVENT_HEADER: str(event.get("type") or ""),
        DELIVERY_HEADER: delivery_id,
    }
    secret = subscription.get("secret") or ""
    if secret:
        headers[SIGNATURE_HEADER] = hmac_signature(secret, body)
    return {"url": str(subscription.get("url") or ""), "headers": headers, "body": body}


async def deliver(subscription: Dict[str, Any], event: Dict[str, Any],
                  delivery_id: str) -> Dict[str, Any]:
    """One delivery attempt series. Returns a result; never raises into the caller.

    Never raises because every caller is either a fire-and-forget hook on the decision path or a
    scheduler drain: an exception in either place would turn "a subscriber is down" into "a
    decision failed" or "the drain stopped at the first bad row".
    """
    request = build_request(subscription, event, delivery_id)
    timeout = clamp_timeout_ms(subscription.get("timeout_ms", 5000)) / 1000.0
    retries = clamp_retries(subscription.get("retries", 2))
    if not request["url"]:
        return {"delivered": False, "error": "subscription has no url", "attempts": 0}

    try:
        pinned = build_pinned_request(request["url"])
    except EgressBlocked as blocked:
        # Recorded, not raised, and NOT retried: a blocked target is a configuration decision,
        # not a transient failure, so retrying it just repeats the same refusal on a timer.
        return {"delivered": False, "error": "blocked_by_egress_policy: {0}".format(blocked),
                "attempts": 0, "blocked": True}

    headers = dict(request["headers"])
    headers["Host"] = pinned.host_header
    last_error: Optional[str] = None
    status: Optional[int] = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for attempt in range(retries + 1):
            try:
                response = await client.post(
                    pinned.url, content=request["body"], headers=headers,
                    extensions={"sni_hostname": pinned.sni_hostname})
                status = response.status_code
                if 200 <= status < 300:
                    return {"delivered": True, "status": status, "attempts": attempt + 1}
                last_error = "HTTP {0}".format(status)
            except Exception as error:  # noqa: BLE001 - every transport failure is a report
                last_error = str(error)[:500]
    return {"delivered": False, "error": last_error, "status": status, "attempts": retries + 1}


# ── emission: durable, then prompt ─────────────────────────────────────────
# WRITE FIRST, DELIVER AFTER. This used to start one daemon thread per event per subscription,
# retry inside that thread, and record only last-known state on the subscription row. So a
# rolling deploy or a SIGTERM dropped every in-flight event with no record it had existed, a
# receiver down for ten minutes lost everything emitted in those ten minutes, and there was no
# dead-letter queue -- while this module's own docstring promised one. And one event x 50
# subscriptions was 50 unbounded OS threads started from inside a request handler.
#
# Now:
#   1. `emit` INSERTS one `webhook_deliveries` row per matching subscription. A cheap write on
#      the caller's path; still never delays or changes a decision, still never raises.
#   2. ONE long-lived worker thread delivers them promptly. One, not one per delivery.
#   3. A leader-gated scheduler drain (`drain`) is the durability guarantee: it delivers
#      anything due -- retries whose backoff has elapsed, and rows a dead process claimed and
#      never finished -- so nothing depends on the worker surviving.
#   4. Failure reschedules with backoff; after WEBHOOK_MAX_ATTEMPTS the row is DEAD, kept,
#      listable, and replayable. A target blocked by egress policy goes straight to dead: that is
#      a configuration decision, and retrying it just repeats the refusal on a timer.
#
# Delivery is at-least-once (a process can die after the receiver accepted and before we record
# it); the delivery id is sent so a receiver can dedupe.

_EMIT_ENABLED = True

# Seconds to wait before attempt N+1 after attempt N failed. ~9 hours end to end, so a receiver
# down for an afternoon still gets its events; each subscription's own `retries` still governs
# the immediate in-attempt retries inside `deliver()` that absorb a transient blip.
BACKOFF_SECONDS = (10, 30, 120, 600, 1800, 7200, 21600)


def _max_attempts() -> int:
    return max(1, env.env_int("WEBHOOK_MAX_ATTEMPTS", len(BACKOFF_SECONDS) + 1))


def set_emission_enabled(enabled: bool) -> None:
    """Test hook. A suite that is not testing delivery wants it off rather than racing with it."""
    global _EMIT_ENABLED
    _EMIT_ENABLED = bool(enabled)


def _retry_at(attempts_so_far: int) -> Optional[datetime]:
    """When to try again after the (attempts_so_far)th failure, or None when it is dead."""
    if attempts_so_far >= _max_attempts():
        return None
    delay = BACKOFF_SECONDS[min(attempts_so_far - 1, len(BACKOFF_SECONDS) - 1)]
    return datetime.utcnow() + timedelta(seconds=delay)


def deliver_claimed(storage: Any, delivery: Dict[str, Any]) -> Dict[str, Any]:
    """Deliver ONE claimed row and record the outcome. Never raises."""
    tenant_id = delivery.get("tenant_id")
    try:
        subscription = storage.get_webhook_subscription(
            delivery["subscription_id"], tenant_id=tenant_id, include_secret=True)
    except Exception:  # pragma: no cover - a lookup failure is recorded like any other
        subscription = None
    if not subscription or not subscription.get("is_active", True):
        # Deleted or disabled after the event was queued: dead, with the reason, rather than a
        # delivery to a subscriber who has since said stop.
        return storage.complete_webhook_delivery(
            delivery["id"], ok=False, status=None, dead=True, retry_at=None,
            error="subscription deleted or inactive") or {}
    try:
        result = asyncio.run(deliver(subscription, delivery["event"], delivery["id"]))
    except Exception as exc:  # pragma: no cover - deliver() already never raises
        result = {"delivered": False, "error": str(exc)[:500]}
    ok = bool(result.get("delivered"))
    attempts = int(delivery.get("attempts") or 0) + 1
    retry_at = None if ok or result.get("blocked") else _retry_at(attempts)
    record = storage.complete_webhook_delivery(
        delivery["id"], ok=ok, status=result.get("status"), error=result.get("error"),
        retry_at=retry_at, dead=bool(result.get("blocked")))
    try:
        storage.record_webhook_delivery(subscription["id"], ok, error=result.get("error"),
                                        tenant_id=tenant_id)
    except Exception:  # pragma: no cover - health is secondary to the delivery record
        logger.warning("could not record webhook health for %s", subscription.get("id"))
    return record or {}


def drain(storage: Any, limit: int = 50, ids: Optional[List[str]] = None) -> Dict[str, int]:
    """Deliver everything due. Shared by the prompt worker and the scheduler drain."""
    stale = env.env_int("WEBHOOK_CLAIM_TIMEOUT_SECONDS", 300)
    counts = {"delivered": 0, "retrying": 0, "dead": 0}
    for delivery in storage.claim_due_webhook_deliveries(limit=limit, stale_after_seconds=stale, ids=ids):
        outcome = deliver_claimed(storage, delivery).get("status")
        if outcome == "delivered":
            counts["delivered"] += 1
        elif outcome == "dead":
            counts["dead"] += 1
        else:
            counts["retrying"] += 1
    return counts


class _Worker:
    """One thread, woken on emit. Replaces a thread per delivery.

    It only delivers rows THIS process enqueued (by id), so N replicas do not all stampede the
    same backlog -- the backlog is the leader-gated scheduler drain's job. If this thread dies, or
    the process does, nothing is lost: the rows are in the table and the drain picks them up.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._pending: Dict[str, Any] = {}      # delivery id -> storage
        self._thread: Optional[threading.Thread] = None

    def submit(self, storage: Any, delivery_ids: List[str]) -> None:
        with self._lock:
            for delivery_id in delivery_ids:
                self._pending[delivery_id] = storage
            self._idle.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="webhook-dispatcher", daemon=True)
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=5.0)
            self._wake.clear()
            with self._lock:
                batch = dict(self._pending)
                self._pending.clear()
            by_storage: Dict[int, Any] = {}
            ids_by_storage: Dict[int, List[str]] = {}
            for delivery_id, storage in batch.items():
                by_storage[id(storage)] = storage
                ids_by_storage.setdefault(id(storage), []).append(delivery_id)
            for key, storage in by_storage.items():
                try:
                    drain(storage, limit=len(ids_by_storage[key]), ids=ids_by_storage[key])
                except Exception:  # pragma: no cover - the scheduler drain is the backstop
                    logger.warning("webhook worker drain failed", exc_info=True)
            with self._lock:
                if not self._pending:
                    self._idle.set()

    def flush(self, timeout: float = 10.0) -> bool:
        """Block until everything submitted so far has been attempted. For tests and shutdown."""
        return self._idle.wait(timeout)


_WORKER = _Worker()


def flush(timeout: float = 10.0) -> bool:
    return _WORKER.flush(timeout)


def emit(storage: Any, tenant_id: str, event_type: str, data: Dict[str, Any]) -> int:
    """Queue one event for every matching subscription. Returns how many were queued.

    Never raises: every call site is either a decision or an audit write, and neither may fail
    because a webhook subscription is misconfigured.
    """
    if not _EMIT_ENABLED or event_type not in EVENT_TYPES:
        return 0
    try:
        candidates = storage.subscriptions_for_event(tenant_id, event_type)
    except Exception:  # pragma: no cover - a lookup failure must not break the caller
        logger.warning("could not load webhook subscriptions for %s", event_type, exc_info=True)
        return 0
    event = {"type": event_type, "tenant_id": tenant_id,
             "created_at": _now_iso(), "data": data or {}}
    queued: List[str] = []
    for subscription in candidates:
        if not event_matches(subscription, event):
            continue
        try:
            row = storage.enqueue_webhook_delivery(tenant_id, subscription["id"], event)
            queued.append(row["id"])
        except Exception:  # pragma: no cover - one bad insert must not block the others
            logger.warning("could not queue webhook for %s", subscription.get("id"), exc_info=True)
    if queued:
        _WORKER.submit(storage, queued)
    return len(queued)


def on_audit_event(storage: Any, event: Dict[str, Any]) -> None:
    """Registered with `storage.register_audit_listener` at startup.

    `add_audit_event` is the single funnel for ten of the thirteen event types -- policy
    lifecycle, experiment promotion, review completion, SLO breach, cron, reports, the inbound
    webhook trigger, SDK sync, repeated login failure, monitor scheduling. Hooking there rather
    than at each call site means a new emitter of an existing event type is subscribable the day
    it lands, instead of the day someone remembers to add a second call.
    """
    event_type = str(event.get("event_type") or "")
    if event_type not in EVENT_TYPES:
        return
    emit(storage, str(event.get("tenant_id") or ""), event_type, {
        "entity_type": event.get("entity_type"),
        "entity_id": event.get("entity_id"),
        "detail": event.get("detail"),
        "metadata": event.get("metadata") or event.get("metadata_json") or {},
    })


# Outcome -> the event types it produces. `decision.completed` always fires, so a subscriber that
# wants everything needs one subscription rather than three; the outcome-specific types exist so
# the common case ("tell me about rejections") does not need a filter to express.
_OUTCOME_EVENTS = {
    "reject": "decision.rejected",
    "review": "decision.review_required",
}


def on_decision(storage: Any, decision: Dict[str, Any]) -> None:
    """Registered with `storage.register_decision_listener` at startup.

    Hooked at `add_decision` rather than at /decide for the reason that function's own comments
    give about metering: the endpoint has a fast path AND an executor path, and three further
    entry points besides. A hook at a handler would miss whichever path was added last -- which
    is precisely how input validation, the evaluation budget and sample seeding each ended up
    behaving differently under FAST_DECIDE this cycle.
    """
    tenant_id = str(decision.get("tenant_id") or "")
    if not tenant_id:
        return
    outcome = str(decision.get("outcome") or "")
    data = {
        "decision_id": decision.get("id"),
        "policy_id": decision.get("policy_id"),
        "outcome": outcome,
        "user_id": decision.get("user_id"),
        "source": decision.get("source"),
        # The payload and the variable values are deliberately NOT included. They are the
        # customer's bureau/bank data, and a subscription is a standing feed to a third party --
        # an outcome plus an id is enough to act on, and the receiver can fetch the rest through
        # an authenticated API call if it is entitled to.
        "created_at": decision.get("created_at"),
    }
    emit(storage, tenant_id, "decision.completed", data)
    specific = _OUTCOME_EVENTS.get(outcome)
    if specific:
        emit(storage, tenant_id, specific, data)
