"""Conflict-free collaborative editing (CRDT) + time-travel version history for policy documents.

This is the backend foundation for multi-author editing and a time-travel editor:

  * **CRDT merge** — a per-field Last-Writer-Wins register (a well-known state-based CRDT). Each
    field carries a Lamport-style (timestamp, actor) stamp; merging two concurrent versions is
    associative, commutative, and idempotent, so any two replicas that have seen the same set of
    edits converge to the SAME document regardless of order — no lost updates, no manual conflict
    resolution. Edits to different fields both survive; edits to the same field resolve
    deterministically by (timestamp, actor) — never by "whoever saved last wins the whole doc".

  * **Time-travel** — every committed edit appends an immutable version. You can read the document
    as of any past version and restore it (restore is itself a new forward edit, so history is
    never rewritten — you can always travel back again).

Pure/stateless here; persistence is handled by ``storage`` (see ``EditHistory`` usage in the
collab router). Live presence / cursor sharing over WebSocket is a separate transport concern and
is intentionally not in this module — this layer guarantees *convergence*, which is the hard part.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

# A registered field value: {"value": <any>, "ts": <float lamport/wall clock>, "actor": <str>}.
Field = Dict[str, Any]
CRDTDoc = Dict[str, Field]


def _wins(a: Field, b: Field) -> Field:
    """Deterministic winner of two field registers: higher timestamp wins; ties break by actor id
    (lexicographically greater) so every replica picks the SAME winner without coordination."""
    at, bt = a.get("ts", 0), b.get("ts", 0)
    if at != bt:
        return a if at > bt else b
    return a if str(a.get("actor", "")) >= str(b.get("actor", "")) else b


def new_field(value: Any, ts: float, actor: str) -> Field:
    return {"value": copy.deepcopy(value), "ts": ts, "actor": actor}


def to_crdt(plain: Dict[str, Any], ts: float, actor: str) -> CRDTDoc:
    """Lift a plain {field: value} document into CRDT registers stamped by (ts, actor)."""
    return {k: new_field(v, ts, actor) for k, v in plain.items()}


def to_plain(doc: CRDTDoc) -> Dict[str, Any]:
    """Project a CRDT document back to plain {field: value} (drops metadata)."""
    return {k: copy.deepcopy(f.get("value")) for k, f in doc.items()}


def apply_edit(doc: CRDTDoc, changes: Dict[str, Any], ts: float, actor: str) -> CRDTDoc:
    """Return a new CRDT doc with ``changes`` applied as (ts, actor)-stamped registers. A field set
    to None is a tombstone-by-value here (kept as an explicit null); callers that need delete
    semantics can filter nulls in to_plain — kept simple on purpose."""
    merged = copy.deepcopy(doc)
    for k, v in changes.items():
        candidate = new_field(v, ts, actor)
        merged[k] = _wins(merged[k], candidate) if k in merged else candidate
    return merged


def merge(a: CRDTDoc, b: CRDTDoc) -> CRDTDoc:
    """Merge two CRDT documents. Associative, commutative, idempotent — the core CRDT guarantee:
    merge(a,b) == merge(b,a), and merging the same doc twice changes nothing, so replicas converge."""
    out: CRDTDoc = copy.deepcopy(a)
    for k, bf in b.items():
        out[k] = _wins(out[k], bf) if k in out else copy.deepcopy(bf)
    return out


def three_way_merge(
    base: Dict[str, Any],
    edit_a: Dict[str, Any],
    edit_b: Dict[str, Any],
    ts_a: float,
    actor_a: str,
    ts_b: float,
    actor_b: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """Merge two concurrent plain-dict edits made against the same ``base``.

    Returns (merged_plain, conflicted_fields). Fields edited by only one author both survive;
    fields edited by BOTH (a genuine conflict) resolve by (ts, actor) LWW and are reported in
    conflicted_fields so the UI can surface "your change to X was superseded by <actor>"."""
    doc = to_crdt(base, ts=0.0, actor="__base__")
    a_changed = {k: v for k, v in edit_a.items() if base.get(k) != v}
    b_changed = {k: v for k, v in edit_b.items() if base.get(k) != v}
    conflicts = sorted(set(a_changed) & set(b_changed))
    doc = apply_edit(doc, a_changed, ts_a, actor_a)
    doc = apply_edit(doc, b_changed, ts_b, actor_b)
    return to_plain(doc), conflicts


# --------------------------------------------------------------------------- #
# Time-travel over an ordered version history (list of {version, doc, actor, ts, label}).
# --------------------------------------------------------------------------- #
def doc_as_of(history: List[Dict[str, Any]], version: int) -> Optional[Dict[str, Any]]:
    """The document state at (or the latest before) ``version``. None if before the first version."""
    chosen = None
    for entry in sorted(history, key=lambda e: e["version"]):
        if entry["version"] <= version:
            chosen = entry
        else:
            break
    return copy.deepcopy(chosen["doc"]) if chosen else None


def diff_versions(history: List[Dict[str, Any]], v_from: int, v_to: int) -> Dict[str, Any]:
    """Field-level diff between two versions: {field: {"from": x, "to": y}} for changed fields."""
    a = doc_as_of(history, v_from) or {}
    b = doc_as_of(history, v_to) or {}
    changed: Dict[str, Any] = {}
    for key in set(a) | set(b):
        if a.get(key) != b.get(key):
            changed[key] = {"from": a.get(key), "to": b.get(key)}
    return changed
