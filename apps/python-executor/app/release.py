"""Release snapshots + one-click rollback.

Every promotion already captures a full snapshot of the policy's decision definition (its steps
plus the definitions of every referenced rule / scorecard / decision table) in ``Promotion.snapshot_json``
(see ``policy_diff.policy_snapshot``). That snapshot history IS the release history — this module
turns it into an addressable timeline and a one-click restore:

  * ``list_releases`` — the ordered promotion/snapshot timeline for a policy (each an immutable
    point-in-time release you can diff or roll back to).
  * ``rollback_policy`` — restore the policy AND its referenced assets to a chosen release's
    snapshot, then record the rollback itself as a new promotion (history is append-only; a
    rollback is a forward event, never a rewrite, so you can always roll forward again).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


def list_releases(storage: Any, policy_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The release timeline for one policy: promotions that captured a snapshot, newest first."""
    releases = []
    for promo in storage.list_promotions(tenant_id=tenant_id):
        if promo.get("entity_type") == "policy" and promo.get("entity_id") == policy_id and promo.get("has_snapshot"):
            releases.append({
                "promotion_id": promo.get("id"),
                "from_status": promo.get("from_status"),
                "to_status": promo.get("to_status"),
                "promoted_by": promo.get("promoted_by"),
                "reason": promo.get("reason"),
                "created_at": promo.get("created_at"),
                "has_snapshot": True,
            })
    return sorted(releases, key=lambda r: (r.get("created_at") or ""), reverse=True)


def _find_snapshot(storage: Any, policy_id: str, promotion_id: int, tenant_id: Optional[str]) -> Optional[Dict[str, Any]]:
    promo = storage.get_promotion(promotion_id, tenant_id=tenant_id)
    if promo and promo.get("entity_type") == "policy" and promo.get("entity_id") == policy_id:
        return promo.get("snapshot")
    return None


def rollback_policy(
    storage: Any,
    policy_id: str,
    promotion_id: int,
    actor: str,
    tenant_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore ``policy_id`` (and its referenced rules/scorecards/decision tables) to the snapshot
    captured by promotion ``promotion_id``, then log a 'rollback' promotion. Returns a summary of
    what was restored. Raises ValueError if the policy or the target snapshot is missing."""
    policy = storage.get_policy(policy_id, tenant_id=tenant_id)
    if not policy:
        raise ValueError("Policy {0} not found.".format(policy_id))
    snapshot = _find_snapshot(storage, policy_id, promotion_id, tenant_id)
    if not snapshot:
        raise ValueError("No snapshot found for promotion {0} of policy {1}.".format(promotion_id, policy_id))

    restored = {"rules": [], "scorecards": [], "decision_tables": []}

    # 1) Restore referenced assets to their snapshotted definitions.
    for rid, rule in (snapshot.get("rules") or {}).items():
        if storage.get_rule(rid, tenant_id=tenant_id):
            storage.update_rule(rid, {"name": rule.get("name"), "tree": rule.get("tree"),
                                      "nodes": rule.get("nodes")}, tenant_id=tenant_id)
            restored["rules"].append(rid)
    for sid, sc in (snapshot.get("scorecards") or {}).items():
        if storage.get_scorecard(sid, tenant_id=tenant_id):
            storage.update_scorecard(sid, {"name": sc.get("name"), "attributes": sc.get("attributes"),
                                           "bands": sc.get("bands")}, tenant_id=tenant_id)
            restored["scorecards"].append(sid)
    for tid, table in (snapshot.get("decisionTables") or {}).items():
        if storage.get_decision_table(tid, tenant_id=tenant_id):
            storage.update_decision_table(tid, {"name": table.get("name"), "hit_policy": table.get("hit_policy"),
                                                "rows": table.get("rows")}, tenant_id=tenant_id)
            restored["decision_tables"].append(tid)

    # 2) Restore the policy shell (name, defaultOutcome, ordered steps).
    snap_policy = snapshot.get("policy") or {}
    patch: Dict[str, Any] = {}
    if snap_policy.get("name") is not None:
        patch["name"] = snap_policy["name"]
    if snap_policy.get("defaultOutcome") is not None:
        patch["defaultOutcome"] = snap_policy["defaultOutcome"]
    if snapshot.get("steps"):
        # Re-hydrate steps from the snapshot (type/ref_id/id preserved).
        patch["steps"] = copy.deepcopy(snapshot["steps"])
    storage.update_policy(policy_id, patch, tenant_id=tenant_id)

    # 3) Append the rollback to the promotion ledger (forward-only history).
    current_status = policy.get("status", "dev")
    new_snapshot = None
    try:
        from .policy_diff import policy_snapshot
        new_snapshot = policy_snapshot(storage, tenant_id, storage.get_policy(policy_id, tenant_id=tenant_id))
    except Exception:
        pass
    storage.add_promotion(
        "policy", policy_id, current_status, current_status, actor,
        reason or "Rollback to promotion #{0}".format(promotion_id),
        tenant_id=tenant_id, snapshot=new_snapshot,
    )
    return {"policy_id": policy_id, "rolled_back_to": promotion_id, "restored": restored}
