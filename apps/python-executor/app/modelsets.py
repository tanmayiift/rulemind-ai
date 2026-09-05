"""Model sets: a versioned group of models with a routing rule (parity P05).

WHAT EXISTED BEFORE. Models were flat blobs addressed one at a time -- a `model` step names one
`public_id` and that is the whole story. There was no way to say "use the thin-file model for
applicants with no bureau history and the main model otherwise", and no way to compare two
candidates on the same input without wiring two policies.

THREE THINGS A SET ADDS, and the middle one is the reason it is called a VERSIONED set:

  * GROUPING -- an ordered list of members, each naming a hosted model.
  * VERSION PINNING -- each member records the model VERSION it was added at.
    `hosted_models` is unique on (tenant, public_id) and carries `version` as a COLUMN, so a
    retrained model replaces its row in place and every reference silently follows. That is fine
    for a single model step, where the author is looking at the model they named. It is not fine
    for a set whose whole purpose is a controlled comparison: a member that moved underneath the
    set makes yesterday's routing decision and today's incomparable, and nothing would say so.
    The pin does not FREEZE the model -- the blob is gone, there is nothing to freeze -- it
    records what was expected so the drift is visible.
  * ROUTING -- the first member whose condition matches wins, and a default catches the rest.

Conditions are `executor.evaluate_condition` strings, the same thing a BRANCH step takes, because
routing between models is the same shape of decision as routing between steps and an author
should not have to learn a second syntax three lines away.

Fails closed in the routing sense: a member whose condition cannot be evaluated is SKIPPED rather
than treated as matching. Falling through to the default is a defined outcome an author chose; a
malformed condition silently selecting a model is not.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("rulemind.modelsets")

MAX_MEMBERS = 20


class ModelSetInvalid(ValueError):
    """A set that cannot be stored as written -- refused at authoring time rather than saved as a
    row that routes to nothing."""


def validate_members(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ModelSetInvalid("members must be a non-empty list")
    if len(raw) > MAX_MEMBERS:
        raise ModelSetInvalid(
            "a model set may hold at most {0} members".format(MAX_MEMBERS))
    cleaned: List[Dict[str, Any]] = []
    seen_labels = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ModelSetInvalid("each member must be an object")
        model_id = str(item.get("model_id") or "").strip()
        if not model_id:
            raise ModelSetInvalid("each member needs a model_id")
        label = str(item.get("label") or model_id).strip()
        if label in seen_labels:
            # Labels key the comparison output; duplicates would silently overwrite each other
            # and the operator would see fewer candidates than they configured.
            raise ModelSetInvalid("duplicate member label: {0}".format(label))
        seen_labels.add(label)
        condition = item.get("condition")
        if condition is not None and not isinstance(condition, str):
            raise ModelSetInvalid(
                "a member condition must be a string expression, like a branch condition")
        cleaned.append({
            "model_id": model_id,
            "label": label,
            "condition": (condition or "").strip() or None,
            "pinned_version": item.get("pinned_version"),
        })
    return cleaned


def fingerprint(model: Dict[str, Any]) -> Optional[str]:
    """A content hash of the model blob -- the pin that actually detects a retrain.

    THE VERSION COLUMN CANNOT DO THIS JOB. `routers/models.py` writes `"version": 1` on every
    upload and nothing increments it, so re-uploading a retrained model leaves the column at 1 and
    a version pin would report "in sync" against different weights. That was found here by a test
    that SKIPPED rather than passed, which is worth noting: a skip is not a pass, and a feature
    whose premise rests on a column nobody maintains is a feature that quietly does nothing.

    The bytes cannot lie the way a counter can. `version` is still recorded alongside because it
    is what an operator reads, but drift is decided on this.
    """
    blob = model.get("model_blob")
    if not isinstance(blob, (bytes, bytearray)):
        return None
    return hashlib.sha256(bytes(blob)).hexdigest()


def pin_members(members: List[Dict[str, Any]], storage: Any, tenant_id: str) -> List[Dict[str, Any]]:
    """Record each member's current version AND content fingerprint, and refuse a member that
    names nothing.

    Done at write time rather than read time on purpose: a set that silently contains a
    non-existent model routes to an error at decision time, which is the worst place to find out.
    """
    pinned = []
    for member in members:
        model = storage.get_model(member["model_id"], include_blob=True, tenant_id=tenant_id)
        if not model:
            raise ModelSetInvalid("unknown model: {0}".format(member["model_id"]))
        pinned.append({**member,
                       "pinned_version": int(model.get("version") or 1),
                       "pinned_fingerprint": fingerprint(model)})
    return pinned


# Kept as the old name for any caller that predates the rename.
pin_versions = pin_members


def member_drift(members: List[Dict[str, Any]], storage: Any, tenant_id: str) -> List[Dict[str, Any]]:
    """Members whose model has moved since it was pinned.

    Reported, never enforced. Retraining a model is a legitimate thing to do and blocking a
    decision because of it would make this feature a liability; what the set owes the operator is
    that the change is VISIBLE, not that it is prevented.
    """
    drifted = []
    for member in members:
        model = storage.get_model(member["model_id"], include_blob=True, tenant_id=tenant_id)
        if not model:
            drifted.append({"label": member["label"], "model_id": member["model_id"],
                            "pinned_version": member.get("pinned_version"),
                            "current_version": None, "reason": "model no longer exists"})
            continue
        current = int(model.get("version") or 1)
        current_print = fingerprint(model)
        pinned_print = member.get("pinned_fingerprint")
        # Fingerprint FIRST: it is the one that actually moves when a model is retrained. The
        # version comparison is kept for the case of a set pinned before fingerprints existed.
        if pinned_print and current_print and current_print != pinned_print:
            drifted.append({"label": member["label"], "model_id": member["model_id"],
                            "pinned_version": member.get("pinned_version"),
                            "current_version": current,
                            "reason": "the model's weights changed since it was pinned"})
        elif member.get("pinned_version") is not None and current != int(member["pinned_version"]):
            drifted.append({"label": member["label"], "model_id": member["model_id"],
                            "pinned_version": member["pinned_version"],
                            "current_version": current,
                            "reason": "model version changed since it was pinned"})
    return drifted


def resolve_member(model_set: Dict[str, Any], view: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """The member this decision routes to, and why. Returns (member, reason).

    Order matters and is the author's: the FIRST matching condition wins, exactly as a branch
    step's branches are evaluated. A member with no condition is unconditional and therefore acts
    as the default if it is reached -- which is why a trailing member with no condition is the
    idiomatic way to write "everything else".
    """
    from .executor import evaluate_condition

    members = model_set.get("members") or []
    for member in members:
        condition = member.get("condition")
        if not condition:
            return member, "unconditional member '{0}'".format(member["label"])
        try:
            if evaluate_condition(condition, view):
                return member, "matched '{0}'".format(condition)
        except Exception:  # noqa: BLE001 - a bad condition must not select a model
            # SKIPPED, not matched. Falling through to the default is a defined outcome the
            # author chose; a malformed condition silently selecting a model is not.
            logger.warning("model set %s: unevaluable condition on member %s",
                           model_set.get("id"), member.get("label"))
            continue
    return None, "no member matched and there is no unconditional member"
