"""Tamper-evident hash chain over the audit log.

Each audit event carries a per-tenant sequence number, the hash of the event before it, and
its own hash over a canonical serialization of its content. Rewriting or deleting any event
breaks every link after it, so silent edits become detectable by a chain walk.

WHAT THIS DOES AND DOES NOT BUY YOU -- stated plainly, because a hash chain is routinely
oversold. An operator with write access to the database can rewrite an event AND recompute
every subsequent hash; the chain alone cannot stop that, and any claim that it makes the log
"immutable" is false. What the chain gives you is:

  * detection of PARTIAL tampering -- the common case. Editing a row through ordinary SQL,
    dropping an inconvenient event, or restoring a doctored subset all break the chain.
  * a single 64-char head hash that commits to the entire history. Publish that head somewhere
    the operator does not control -- an append-only replica, a WORM bucket, a compliance
    vendor, a daily email to the auditor -- and full-history forgery stops being possible
    without also forging the anchor. `verify()` returns the head for exactly this purpose.

So: the chain is necessary but not sufficient. It is the part that belongs in the database;
the anchor is an operational control that belongs in the runbook.

Chain state is per tenant -- tenants are isolated, and one tenant's events must never be
required to verify another's.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

# prev_hash of the first event in a tenant's chain. Not a real hash -- no preimage produces it,
# so a forged event cannot claim to be the genesis of an existing chain.
GENESIS_HASH = "0" * 64

# The fields committed to by the hash, in the order they are named here (the canonical form
# sorts keys, so this tuple documents the SET, not the byte order).
#
# Adding a field here changes every future hash and INVALIDATES existing rows, because their
# stored hash was computed over the old field set. Treat this tuple as a wire format: to add a
# field, add it alongside a chain version bump and a migration that re-anchors, never in place.
CHAINED_FIELDS = (
    "id",
    "tenant_id",
    "event_type",
    "entity_type",
    "entity_id",
    "detail",
    "metadata",
    "user_id",
    "ip_address",
    "created_at",
    "seq",
    "prev_hash",
)


def canonical_payload(event: Dict[str, Any]) -> str:
    """Deterministic serialization of the chained fields.

    Determinism is the whole game: the same event must hash identically on SQLite and
    Postgres, on any Python build, and after a JSON round trip through the API. Hence
    sort_keys (dict order is an implementation detail), no whitespace (formatting must not
    change the digest), and default=str so a datetime that slipped through as an object
    serializes the same way the API's ISO-8601 string does rather than raising.
    """
    subset = {key: event.get(key) for key in CHAINED_FIELDS}
    return json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_hash(event: Dict[str, Any]) -> str:
    """sha256 of the canonical payload, hex. Never includes the row's own `hash`."""
    return hashlib.sha256(canonical_payload(event).encode("utf-8")).hexdigest()


def verify(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Walk a tenant's events in seq order and report on the chain.

    `events` must be ordered oldest-first. Rows written before the chain existed carry a NULL
    hash; they are counted as `pre_chain` and are NEVER reported as verified. That distinction
    is the point of the whole migration -- reporting a backfilled row as verified would be a
    lie told by the compliance feature itself, which is worse than having no feature.

    Returns a report rather than raising: a broken chain is an ANSWER, not an error, and the
    caller needs the break location to investigate.
    """
    pre_chain = 0
    verified = 0
    breaks: List[Dict[str, Any]] = []
    expected_prev: Optional[str] = None
    expected_seq: Optional[int] = None
    head: Optional[str] = None
    chain_started = False

    for event in events:
        stored = event.get("hash")
        if not stored:
            if chain_started:
                # A hashless row AFTER the chain began cannot be a legacy row -- legacy rows all
                # precede the migration. It is an insert that bypassed add_audit_event.
                breaks.append({
                    "id": event.get("id"),
                    "seq": event.get("seq"),
                    "reason": "unchained_row_after_chain_start",
                })
            else:
                pre_chain += 1
            continue

        if not chain_started:
            chain_started = True
            # The first chained row must anchor to GENESIS. Anything else means rows before it
            # were deleted -- a truncation attack, which is invisible if you only check links.
            if event.get("prev_hash") != GENESIS_HASH and pre_chain == 0:
                breaks.append({
                    "id": event.get("id"),
                    "seq": event.get("seq"),
                    "reason": "chain_does_not_start_at_genesis",
                })
        else:
            if event.get("prev_hash") != expected_prev:
                breaks.append({
                    "id": event.get("id"),
                    "seq": event.get("seq"),
                    "reason": "prev_hash_mismatch",
                    "expected": expected_prev,
                    "found": event.get("prev_hash"),
                })
            if expected_seq is not None and event.get("seq") != expected_seq:
                # Catches deletion of a whole run of events: the links still join up if the
                # attacker re-stitched them, but the sequence has a hole.
                breaks.append({
                    "id": event.get("id"),
                    "seq": event.get("seq"),
                    "reason": "seq_gap",
                    "expected": expected_seq,
                    "found": event.get("seq"),
                })

        recomputed = compute_hash(event)
        if recomputed != stored:
            breaks.append({
                "id": event.get("id"),
                "seq": event.get("seq"),
                "reason": "content_hash_mismatch",
                "expected": recomputed,
                "found": stored,
            })
        else:
            verified += 1

        expected_prev = stored
        head = stored
        seq = event.get("seq")
        expected_seq = (seq + 1) if isinstance(seq, int) else None

    return {
        "ok": not breaks,
        "verified": verified,
        "pre_chain": pre_chain,
        "breaks": breaks,
        # Publish this outside the operator's control to close the full-rewrite hole.
        "head": head,
        "anchor_note": (
            "A chain proves partial tampering only. Export `head` to storage the database "
            "operator cannot rewrite to also detect a full-history recompute."
        ),
    }
