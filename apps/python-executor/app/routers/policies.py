"""Policy endpoints — CRUD, input-schema, execute, MECE analysis, lifecycle, diff, promote.
Extracted verbatim from app/main.py. Stable helpers/models imported by value from app.main; direct
storage calls use main.storage live."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query

from .. import main
from ..logic import find_by_id
from ..main import (
    LifecycleTransitionRequest,
    PolicyUpsertRequest,
    PromoteRequest,
    TestPayloadRequest,
    active_tenant_id,
    current_rule_map,
    current_policy_map,
    ensure_exists,
    make_id,
    maybe_compile_bundle,
    promote_entity,
    record_error,
    test_policy_entity,
    validate_policy_steps,
)

router = APIRouter()


@router.get("/api/v1/policies")
def list_policies(status: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    return main.storage.list_policies(status=status)


@router.get("/api/v1/policies/{policy_id}")
def get_policy(policy_id: str) -> Dict[str, Any]:
    return ensure_exists(main.storage.get_policy(policy_id), "policy", policy_id)


@router.get("/api/v1/policies/{policy_id}/input-schema")
def policy_input_schema(policy_id: str) -> Dict[str, Any]:
    """The real input fields this policy reads — the union of the schema fields of
    the connector sources it references, with a sample value per field. Powers
    accurate simulation: synthetic/uploaded cases must populate THESE fields (e.g.
    `bureau_score`), not arbitrary ones, or they never reach the policy's rules."""
    policy = ensure_exists(main.storage.get_policy(policy_id), "policy", policy_id)
    connectors = {c["id"]: c for c in main.storage.list_connectors()}
    sources: List[Dict[str, Any]] = []
    fields: List[Dict[str, Any]] = []
    seen_sources: set = set()
    seen_fields: set = set()
    for step in policy.get("steps", []):
        if step.get("type") != "connector":
            continue
        cid = step.get("ref_id") or step.get("ref")
        connector = connectors.get(cid)
        if not connector or cid in seen_sources:
            continue
        seen_sources.add(cid)
        sample = connector.get("sample_payload", {}) or {}
        source_fields = []
        for name in connector.get("schema_paths", []) or []:
            entry = {"name": name, "sample": sample.get(name), "source_id": cid}
            source_fields.append(entry)
            if name not in seen_fields:
                seen_fields.add(name)
                fields.append(entry)
        sources.append({"source_id": cid, "name": connector.get("name", cid), "fields": source_fields})
    return {"policy_id": policy_id, "sources": sources, "fields": fields}


@router.post("/api/v1/policies")
def create_policy(request: PolicyUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:
    steps = [item.model_dump() for item in request.steps]
    validate_policy_steps(steps)
    policy_id = make_id(request.name, current_policy_map())
    created = main.storage.create_policy(
        {
            "id": policy_id,
            "name": request.name,
            "trigger": request.trigger,
            "steps": steps,
            "defaultOutcome": request.defaultOutcome,
            "status": request.status,
            "last_test_result": None,
            "version": 1,
        }
    )
    if created["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return created


@router.put("/api/v1/policies/{policy_id}")
def update_policy(
    policy_id: str,
    request: PolicyUpsertRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    steps = [item.model_dump() for item in request.steps]
    validate_policy_steps(steps)
    existing = ensure_exists(main.storage.get_policy(policy_id), "policy", policy_id)
    updated = main.storage.update_policy(
        policy_id,
        {
            "name": request.name,
            "trigger": request.trigger,
            "steps": steps,
            "defaultOutcome": request.defaultOutcome,
            "status": request.status,
        },
    )
    if existing["status"] == "prod" or request.status == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return ensure_exists(updated, "policy", policy_id)


@router.delete("/api/v1/policies/{policy_id}")
def delete_policy(policy_id: str) -> Dict[str, Any]:
    policy = ensure_exists(main.storage.get_policy(policy_id), "policy", policy_id)
    if policy["status"] != "dev":
        raise HTTPException(status_code=409, detail="Only DEV policies can be deleted.")
    return main.storage.delete_policy(policy_id) or policy


@router.post("/api/v1/policies/{policy_id}/execute")
@router.post("/api/v1/test/policy/{policy_id}")
def execute_policy_endpoint(policy_id: str, request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    policy = ensure_exists(main.storage.get_policy(policy_id), "policy", policy_id)
    result = test_policy_entity(policy, request.payload)
    if result["result"].get("outcome") == "reject":
        record_error("policies", "execute", "Policy execution rejected on sample payload.", "policy", policy_id, {"trace": result["result"].get("trace", [])})
    return result


@router.post("/api/v1/policies/{policy_id}/analyze-mece")
def analyze_policy_mece(policy_id: str, payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """Analyze the rules within a policy for MECE (Mutually Exclusive & Collectively Exhaustive) compliance.

    Analyzes the persisted policy by default. If the caller posts a ``steps`` array
    (e.g. the visual workflow builder validating unsaved edits), those steps are
    analyzed instead so the check reflects what's on the canvas.
    """
    from ..mece import analyze_mece

    policy = find_by_id(main.storage.list_policies(), policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found.")

    rules_map = current_rule_map()
    posted_steps = payload.get("steps") if isinstance(payload, dict) else None
    steps = posted_steps if isinstance(posted_steps, list) else (policy.get("steps") or [])
    rule_inputs = []
    for step in steps:
        ref_id = step.get("ref_id") or step.get("ref")
        if step.get("type") == "rule" and ref_id in rules_map:
            rule = rules_map[ref_id]
            rule_inputs.append({
                "id": rule["id"],
                "name": rule.get("name", rule["id"]),
                "definition": rule.get("definition") or {"nodes": rule.get("nodes", []), "connections": rule.get("connections", [])},
            })

    return analyze_mece(rule_inputs)


@router.get("/api/v1/policies/{policy_id}/lifecycle")
def get_policy_lifecycle(policy_id: str) -> Dict[str, Any]:
    from ..lifecycle import LIFECYCLE_LABELS, LIFECYCLE_STAGES, allowed_transitions

    policy = ensure_exists(main.storage.get_policy(policy_id), "policy", policy_id)
    stage = policy.get("lifecycle_status", "draft")
    return {
        "policy_id": policy_id,
        "stage": stage,
        "label": LIFECYCLE_LABELS.get(stage, stage),
        "allowedTransitions": allowed_transitions(stage),
        "stages": [{"id": s, "label": LIFECYCLE_LABELS[s]} for s in LIFECYCLE_STAGES],
    }


@router.post("/api/v1/policies/{policy_id}/lifecycle")
def transition_policy_lifecycle(policy_id: str, request: LifecycleTransitionRequest) -> Dict[str, Any]:
    """Move a policy to a new lifecycle stage, enforcing the allowed transitions."""
    from ..lifecycle import LIFECYCLE_LABELS, allowed_transitions, can_transition

    tenant_id = active_tenant_id()
    policy = ensure_exists(main.storage.get_policy(policy_id, tenant_id=tenant_id), "policy", policy_id)
    current = policy.get("lifecycle_status", "draft")
    if not can_transition(current, request.target):
        raise HTTPException(
            status_code=422,
            detail=f"Cannot move from '{current}' to '{request.target}'. Allowed: {allowed_transitions(current)}",
        )
    updated = main.storage.update_policy(policy_id, {"lifecycle_status": request.target}, bump_version=False, tenant_id=tenant_id)
    main.storage.add_audit_event(
        {
            "tenant_id": tenant_id,
            "event_type": "policy_lifecycle_changed",
            "entity_type": "policy",
            "entity_id": policy_id,
            "detail": f"Lifecycle {current} → {request.target}.",
            "metadata": {"from": current, "to": request.target, "by": request.actor, "note": request.note},
        },
        tenant_id=tenant_id,
    )
    return {
        "policy_id": policy_id,
        "stage": request.target,
        "label": LIFECYCLE_LABELS.get(request.target, request.target),
        "allowedTransitions": allowed_transitions(request.target),
    }


@router.get("/api/v1/policies/{policy_id}/diff")
def policy_promotion_diff(policy_id: str) -> Dict[str, Any]:
    """The decision-logic delta between the working policy and the last promoted snapshot:
    steps added/removed and rules/scorecards/decision-tables added/removed/changed. Surface this
    before a promotion so a reviewer sees exactly what is shipping."""
    from ..policy_diff import diff_snapshots, policy_snapshot

    policy = ensure_exists(main.storage.get_policy(policy_id), "policy", policy_id)
    tenant_id = active_tenant_id()
    current = policy_snapshot(main.storage, tenant_id, policy)
    previous = main.storage.last_promotion_snapshot("policy", policy_id, tenant_id=tenant_id)
    return diff_snapshots(current, previous)


@router.post("/api/v1/policies/{policy_id}/promote")
def promote_policy(
    policy_id: str,
    request: PromoteRequest,
    background_tasks: BackgroundTasks = None,
    skip_mece: bool = Query(default=False, alias="skipMece"),
) -> Dict[str, Any]:
    # MECE gate: check rules within this policy for overlaps before promotion
    if not skip_mece:
        from ..mece import analyze_mece
        policy = find_by_id(main.storage.list_policies(), policy_id)
        if policy:
            rules_map = current_rule_map()
            steps = policy.get("steps") or []
            rule_inputs = []
            for step in steps:
                ref_id = step.get("ref_id") or step.get("ref")
                if step.get("type") == "rule" and ref_id in rules_map:
                    rule = rules_map[ref_id]
                    rule_inputs.append({
                        "id": rule["id"],
                        "name": rule.get("name", rule["id"]),
                        "definition": rule.get("definition") or {"nodes": rule.get("nodes", []), "connections": rule.get("connections", [])},
                    })
            if len(rule_inputs) > 1:
                mece_result = analyze_mece(rule_inputs)
                hard_overlaps = [d for d in mece_result.get("diagnostics", []) if d.get("type") == "overlap" and d.get("severity") == "error"]
                if hard_overlaps:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "message": "Promotion blocked: MECE overlap detected between rules in this policy.",
                            "diagnostics": hard_overlaps,
                            "meceResult": mece_result,
                        },
                    )

    promoted = promote_entity("policy", policy_id, request.promoted_by, request.reason)
    if promoted["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return promoted
