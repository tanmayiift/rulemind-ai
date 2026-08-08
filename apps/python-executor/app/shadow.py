"""Shadow execution / dark launch.

Run a candidate policy *alongside* a live one on the same real traffic, logging what the candidate
WOULD have decided — without ever changing the outcome returned to the caller. This lets you
validate a new/changed policy against production traffic risk-free before you promote it, and
measure exactly how much of your live traffic it would flip.

Registration is stored in tenant settings (``engine_config.shadow_map``: live_policy_id ->
candidate_policy_id), so no schema change is needed. ``run_shadow`` is called best-effort from the
decision path AFTER the live decision is computed; it never raises and never affects the live path.
Shadow decisions are logged with ``source="shadow"`` and a ``shadow_of`` marker so reporting can
compute the divergence rate.
"""
from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional

_SHADOW_KEY = "shadow_map"


def _engine_config(storage: Any, tenant_id: Optional[str]) -> Dict[str, Any]:
    return copy.deepcopy(storage.get_settings(tenant_id=tenant_id).get("engine_config", {}) or {})


def register_shadow(storage: Any, live_policy_id: str, candidate_policy_id: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Dark-launch ``candidate_policy_id`` behind ``live_policy_id``. Both must exist."""
    if not storage.get_policy(live_policy_id, tenant_id=tenant_id):
        raise ValueError("Live policy {0} not found.".format(live_policy_id))
    if not storage.get_policy(candidate_policy_id, tenant_id=tenant_id):
        raise ValueError("Candidate policy {0} not found.".format(candidate_policy_id))
    cfg = _engine_config(storage, tenant_id)
    shadow = cfg.get(_SHADOW_KEY, {})
    shadow[live_policy_id] = candidate_policy_id
    cfg[_SHADOW_KEY] = shadow
    storage.update_settings({"engine_config": cfg}, tenant_id=tenant_id)
    return {"live_policy_id": live_policy_id, "candidate_policy_id": candidate_policy_id, "active": True}


def unregister_shadow(storage: Any, live_policy_id: str, tenant_id: Optional[str] = None) -> Dict[str, Any]:
    cfg = _engine_config(storage, tenant_id)
    shadow = cfg.get(_SHADOW_KEY, {})
    removed = shadow.pop(live_policy_id, None)
    cfg[_SHADOW_KEY] = shadow
    storage.update_settings({"engine_config": cfg}, tenant_id=tenant_id)
    return {"live_policy_id": live_policy_id, "removed": removed is not None}


def shadow_target(storage: Any, live_policy_id: str, tenant_id: Optional[str] = None) -> Optional[str]:
    return _engine_config(storage, tenant_id).get(_SHADOW_KEY, {}).get(live_policy_id)


def run_shadow(
    storage: Any,
    live_policy_id: str,
    payload: Dict[str, Any],
    live_outcome: str,
    tenant_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """If a shadow candidate is registered for ``live_policy_id``, evaluate it on the same payload,
    log the shadow decision (source='shadow'), and return {candidate_policy_id, shadow_outcome,
    live_outcome, diverged}. Best-effort: any failure returns None and never affects the live path."""
    try:
        candidate_id = shadow_target(storage, live_policy_id, tenant_id)
        if not candidate_id:
            return None
        candidate = storage.get_policy(candidate_id, tenant_id=tenant_id)
        if not candidate:
            return None
        from .fast_decide import fast_decide

        shadow_decision = fast_decide(storage, candidate, payload, tenant_id, log=False)
        shadow_outcome = shadow_decision.get("outcome")
        diverged = shadow_outcome != live_outcome
        # Log the shadow decision so divergence is auditable — tagged, never counted as a real decision.
        storage.add_decision(
            {
                "id": str(uuid.uuid4()),
                "policy_id": candidate_id,
                "payload": payload,
                "computed_variables": shadow_decision.get("variables", {}),
                "outcome": shadow_outcome,
                "source": "shadow",
                "trace": [{"shadow_of": live_policy_id, "live_outcome": live_outcome, "diverged": diverged}],
            },
            tenant_id=tenant_id,
        )
        return {
            "candidate_policy_id": candidate_id,
            "shadow_outcome": shadow_outcome,
            "live_outcome": live_outcome,
            "diverged": diverged,
        }
    except Exception:
        return None  # dark launch must NEVER break the live decision


def shadow_report(storage: Any, live_policy_id: str, tenant_id: Optional[str] = None, limit: int = 2000) -> Dict[str, Any]:
    """Divergence summary for a live policy's dark-launched candidate: how many shadow decisions
    were logged and what fraction would have flipped the outcome."""
    candidate_id = shadow_target(storage, live_policy_id, tenant_id)
    if not candidate_id:
        return {"live_policy_id": live_policy_id, "active": False, "shadow_count": 0}
    rows = storage.sample_policy_decisions(candidate_id, tenant_id=tenant_id, limit=limit)
    shadow_rows = [r for r in rows if r.get("source") == "shadow"]
    total = len(shadow_rows)
    diverged = 0
    transitions: Dict[str, int] = {}
    for r in shadow_rows:
        trace = r.get("trace") or []
        meta = trace[0] if trace and isinstance(trace[0], dict) else {}
        if meta.get("diverged"):
            diverged += 1
        key = "{0}->{1}".format(meta.get("live_outcome"), r.get("outcome"))
        transitions[key] = transitions.get(key, 0) + 1
    return {
        "live_policy_id": live_policy_id,
        "candidate_policy_id": candidate_id,
        "active": True,
        "shadow_count": total,
        "diverged": diverged,
        "divergence_rate_pct": round((diverged / total) * 100, 2) if total else 0.0,
        "transitions": transitions,
    }
