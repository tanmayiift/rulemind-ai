"""A/B experiment endpoints — list/CRUD, status transition, champion/challenger promotion.
Extracted verbatim from app/main.py. Stable helpers/models imported by value from app.main; direct
storage calls use main.storage live."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from .. import main
from ..analytics import experiment_analytics
from ..experiments import apply_experiment_overrides
from ..logic import now_iso
from ..main import (
    ExperimentPromoteRequest,
    ExperimentStatusRequest,
    ExperimentUpsertRequest,
    active_tenant_id,
    ensure_exists,
    make_id,
)

router = APIRouter()


@router.get("/api/v1/experiments")
def list_experiments() -> List[Dict[str, Any]]:
    return main.storage.list_experiments()


@router.get("/api/v1/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> Dict[str, Any]:
    experiment = main.storage.get_experiment(experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    return experiment


def _assert_single_running_experiment(experiment_id: str, status: Optional[str], target_policy_id: Optional[str]) -> None:
    """Enforce at most one *running* experiment per policy. Two running experiments on the same
    policy is an ambiguous state — a decision could be assigned to either — so the API refuses to
    start/keep a second one. Pause the other first. (The decide path resolves deterministically
    regardless, but this keeps the data model unambiguous.)"""
    if status != "running" or not target_policy_id:
        return
    tenant_id = active_tenant_id()
    for exp in main.storage.list_experiments(tenant_id=tenant_id):
        if exp.get("id") == experiment_id:
            continue
        if exp.get("status") == "running" and exp.get("target_policy_id") == target_policy_id:
            raise HTTPException(
                status_code=409,
                detail="Experiment '{0}' is already running on policy '{1}'. Pause it before starting another.".format(
                    exp.get("id"), target_policy_id
                ),
            )


@router.post("/api/v1/experiments")
def create_experiment(request: ExperimentUpsertRequest) -> Dict[str, Any]:
    experiment_id = request.id or make_id(request.name, {item["id"]: item for item in main.storage.list_experiments()})
    payload = {**request.model_dump(), "id": experiment_id}
    _assert_single_running_experiment(experiment_id, payload.get("status"), payload.get("target_policy_id"))
    return main.storage.create_or_update_experiment(payload)


@router.put("/api/v1/experiments/{experiment_id}")
def update_experiment(experiment_id: str, request: ExperimentUpsertRequest) -> Dict[str, Any]:
    existing = main.storage.get_experiment(experiment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    merged = {**existing, **request.model_dump(exclude_none=True), "id": experiment_id}
    _assert_single_running_experiment(experiment_id, merged.get("status"), merged.get("target_policy_id"))
    return main.storage.create_or_update_experiment(merged)


@router.patch("/api/v1/experiments/{experiment_id}/status")
def update_experiment_status(experiment_id: str, request: ExperimentStatusRequest) -> Dict[str, Any]:
    existing = main.storage.get_experiment(experiment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    _assert_single_running_experiment(experiment_id, request.status, existing.get("target_policy_id"))
    return main.storage.create_or_update_experiment({**existing, "status": request.status, "id": experiment_id})


@router.delete("/api/v1/experiments/{experiment_id}")
def delete_experiment(experiment_id: str) -> Dict[str, bool]:
    existing = main.storage.get_experiment(experiment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    if existing["status"] != "draft":
        raise HTTPException(status_code=409, detail="Only draft experiments can be deleted.")
    main.storage.delete_experiment(experiment_id)
    return {"deleted": True}


@router.post("/api/v1/experiments/{experiment_id}/promote")
def promote_experiment(experiment_id: str, request: ExperimentPromoteRequest) -> Dict[str, Any]:
    """Promote a challenger variant to champion.

    Applies the winning variant's condition overrides to the live rules
    (maker/checker recorded) and completes the experiment. `force=true` bypasses
    the guardrail/significance safety check.
    """
    tenant_id = active_tenant_id()
    experiment = ensure_exists(main.storage.get_experiment(experiment_id, tenant_id=tenant_id), "experiment", experiment_id)
    variants = experiment.get("variants", [])
    variant = next((item for item in variants if item.get("id") == request.variant_id), None)
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found in experiment.")

    # Safety gate: only promote a challenger the analysis recommends, unless forced.
    if not request.force:
        analysis = experiment_analytics(main.storage, tenant_id, experiment_id).get("championChallenger", {})
        row = next((c for c in analysis.get("challengers", []) if c["id"] == request.variant_id), None)
        if row is None:
            raise HTTPException(status_code=422, detail="Only a challenger variant can be promoted.")
        if row["recommendation"] != "promote":
            raise HTTPException(
                status_code=422,
                detail=f"Promotion blocked: recommendation is '{row['recommendation']}'. "
                f"Guardrails: {row['guardrails'].get('breaches', [])}. Use force=true to override.",
            )

    # Bake the variant overrides into the live rules (permanent promotion).
    rules = {item["id"]: item for item in main.storage.list_rules(tenant_id=tenant_id)}
    patched = apply_experiment_overrides(rules, {"variant": variant})
    changed = []
    for rule_id, rule in patched.items():
        if rule != rules.get(rule_id):
            main.storage.update_rule(
                rule_id,
                {"tree": rule.get("tree"), "nodes": rule.get("nodes")},
                tenant_id=tenant_id,
            )
            changed.append(rule_id)

    main.storage.create_or_update_experiment(
        {
            **experiment,
            "id": experiment_id,
            "status": "completed",
            "promoted_variant_id": request.variant_id,
            "promoted_by": request.promoted_by,
            "promoted_at": now_iso(),
        }
    )
    main.storage.add_audit_event(
        {
            "tenant_id": tenant_id,
            "event_type": "experiment_promoted",
            "entity_type": "experiment",
            "entity_id": experiment_id,
            "detail": f"Promoted variant {request.variant_id} to champion.",
            "metadata": {"variant": request.variant_id, "rules_updated": changed, "promoted_by": request.promoted_by, "forced": request.force},
        },
        tenant_id=tenant_id,
    )
    return {"experiment_id": experiment_id, "promoted_variant": request.variant_id, "rules_updated": changed}
