"""Authoring endpoints — connector and variable CRUD, test, draft-test, promote, history, graph.
Extracted verbatim from app/main.py.

Shared helpers and request models are imported by value from ``app.main`` (they are stable — never
reassigned — and internally read the live ``main.storage``). Direct ``main.storage`` calls in
handler bodies go through the module object so the test harness's per-test storage swap is honored
(see the routers package docstring)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query

from .. import main
from ..logic import now_iso
from ..main import (
    ConnectorCreateRequest,
    ConnectorUpdateRequest,
    PromoteRequest,
    TestPayloadRequest,
    VariableDraftTestRequest,
    VariableUpsertRequest,
    active_tenant_id,
    connector_payload,
    current_connectors,
    current_variable_map,
    engine_limits,
    ensure_exists,
    make_id,
    maybe_compile_bundle,
    promote_entity,
    record_error,
    test_variable_entity,
)
from ..sandbox import execute_variable

router = APIRouter()


# ── Connectors ─────────────────────────────────────────────────────────────
@router.get("/api/v1/connectors")
def list_connectors() -> List[Dict[str, Any]]:
    return main.storage.list_connectors()


@router.post("/api/v1/connectors")
def create_connector(request: ConnectorCreateRequest) -> Dict[str, Any]:
    connector_id = make_id(request.name, current_connectors())
    return main.storage.create_connector(
        {
            "id": connector_id,
            "name": request.name,
            "icon": request.icon,
            "color": request.color,
            "description": request.description,
            "schema_paths": request.schema_paths,
            "sample_payload": request.sample_payload,
            "is_active": request.is_active,
            "config": request.config,
        }
    )


@router.get("/api/v1/connectors/{connector_id}")
def get_connector(connector_id: str) -> Dict[str, Any]:
    return ensure_exists(main.storage.get_connector(connector_id), "connector", connector_id)


@router.put("/api/v1/connectors/{connector_id}")
def update_connector(connector_id: str, request: ConnectorUpdateRequest) -> Dict[str, Any]:
    connector = ensure_exists(main.storage.get_connector(connector_id), "connector", connector_id)
    updated = main.storage.update_connector(
        connector_id,
        {
            "name": request.name or connector["name"],
            "icon": request.icon or connector["icon"],
            "color": request.color or connector["color"],
            "description": request.description if request.description is not None else connector.get("description"),
            "schema_paths": request.schema_paths or connector.get("schema_paths", []),
            "sample_payload": request.sample_payload if request.sample_payload is not None else connector.get("sample_payload", {}),
            "is_active": connector["is_active"] if request.is_active is None else request.is_active,
            "config": request.config if request.config is not None else connector.get("config", {}),
        },
    )
    return ensure_exists(updated, "connector", connector_id)


@router.delete("/api/v1/connectors/{connector_id}")
def delete_connector(connector_id: str) -> Dict[str, Any]:
    connector = ensure_exists(main.storage.get_connector(connector_id), "connector", connector_id)
    if connector.get("is_active"):
        raise HTTPException(status_code=409, detail="Deactivate the connector before deleting it.")
    return main.storage.delete_connector(connector_id) or connector


@router.post("/api/v1/connectors/{connector_id}/test")
def test_connector(connector_id: str) -> Dict[str, Any]:
    connector = ensure_exists(main.storage.get_connector(connector_id), "connector", connector_id)
    return {
        "connector_id": connector["id"],
        "passed": True,
        "schema_paths": connector["schema_paths"],
        "sample_payload": connector["sample_payload"],
        "config_summary": {
            "auth_type": connector.get("config", {}).get("auth_type", "api_key"),
            "base_url": connector.get("config", {}).get("base_url", ""),
            "has_webhook": bool(connector.get("config", {}).get("webhook_url")),
        },
    }


# ── Variables ──────────────────────────────────────────────────────────────
@router.get("/api/v1/variables")
def list_variables(
    source: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    return main.storage.list_variables(source_id=source, status=status, category=category)


@router.post("/api/v1/variables")
def create_variable(request: VariableUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:
    connectors = current_connectors()
    if request.source_id not in connectors:
        raise HTTPException(status_code=422, detail="Unknown source connector.")
    variable_id = make_id(request.name, current_variable_map())
    created = main.storage.create_variable(
        {
            "id": variable_id,
            "name": request.name,
            "category": request.category,
            "source_id": request.source_id,
            "code": request.code,
            "description": request.description,
            "status": request.status,
            "last_test_result": None,
            "version": 1,
        }
    )
    if created["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return created


@router.put("/api/v1/variables/{variable_id}")
def update_variable(
    variable_id: str,
    request: VariableUpsertRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    existing = ensure_exists(main.storage.get_variable(variable_id), "variable", variable_id)
    updated = main.storage.update_variable(
        variable_id,
        {
            "name": request.name,
            "category": request.category,
            "source_id": request.source_id,
            "code": request.code,
            "description": request.description,
            "status": request.status,
        },
    )
    if existing["status"] == "prod" or request.status == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return ensure_exists(updated, "variable", variable_id)


@router.delete("/api/v1/variables/{variable_id}")
def delete_variable(variable_id: str) -> Dict[str, Any]:
    variable = ensure_exists(main.storage.get_variable(variable_id), "variable", variable_id)
    if variable["status"] != "dev":
        raise HTTPException(status_code=409, detail="Only DEV variables can be deleted.")
    return main.storage.delete_variable(variable_id) or variable


@router.post("/api/v1/variables/{variable_id}/test")
def test_variable(variable_id: str, request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    variable = ensure_exists(main.storage.get_variable(variable_id), "variable", variable_id)
    result = test_variable_entity(variable, request.payload)
    if result["result"].get("error"):
        record_error("variables", "test", result["result"]["error"], "variable", variable_id, {"payload": request.payload})
    return result


@router.post("/api/v1/variables/test-draft")
def test_variable_draft(request: VariableDraftTestRequest) -> Dict[str, Any]:
    connectors = current_connectors()
    if request.source_id not in connectors:
      raise HTTPException(status_code=422, detail="Unknown source connector.")
    limits = engine_limits()
    execution = execute_variable(
        request.code,
        connector_payload(request.source_id, request.payload),
        {},
        timeout_ms=limits["timeout_ms"],
        memory_mb=limits["memory_mb"],
    )
    result = {
        "result": {
            "value": execution.get("value"),
            "error": execution.get("error"),
            "latency_ms": execution.get("latency_ms"),
            "passed": execution.get("error") in (None, ""),
            "tested_at": now_iso(),
        }
    }
    if result["result"]["error"]:
        record_error("variables", "draft_test", result["result"]["error"], "variable", None, {"source_id": request.source_id})
    return result


@router.post("/api/v1/variables/{variable_id}/promote")
def promote_variable(
    variable_id: str,
    request: PromoteRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    promoted = promote_entity("variable", variable_id, request.promoted_by, request.reason)
    if promoted["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return promoted


@router.get("/api/v1/variables/{variable_id}/history")
def variable_history(variable_id: str) -> List[Dict[str, Any]]:
    return main.storage.get_history("variable", variable_id)


@router.get("/api/v1/variables/graph")
def variable_graph() -> Dict[str, Any]:
    variables = main.storage.list_variables()
    rules = main.storage.list_rules()
    scorecards = main.storage.list_scorecards()
    policies = main.storage.list_policies()
    nodes = []
    edges = []
    for variable in variables:
        nodes.append({"id": variable["id"], "type": "variable", "label": variable["name"], "status": variable["status"]})
    for rule in rules:
        nodes.append({"id": rule["id"], "type": "rule", "label": rule["name"], "status": rule["status"]})
        referenced = {node.get("variable") for node in rule.get("nodes", []) if node.get("type") == "condition" and node.get("variable")}
        for variable_id in referenced:
            edges.append({"from": variable_id, "to": rule["id"], "relation": "used_by_rule"})
    for scorecard in scorecards:
        nodes.append({"id": scorecard["id"], "type": "scorecard", "label": scorecard["name"], "status": scorecard["status"]})
        for factor in scorecard.get("bins", []):
            edges.append({"from": factor.get("variable_id"), "to": scorecard["id"], "relation": "used_by_scorecard"})
    for policy in policies:
        nodes.append({"id": policy["id"], "type": "policy", "label": policy["name"], "status": policy["status"]})
        for step in policy.get("steps", []):
            if step.get("type") in {"rule", "scorecard"}:
                edges.append({"from": step.get("ref_id"), "to": policy["id"], "relation": "used_by_policy"})
    return {"nodes": nodes, "edges": edges}


@router.get("/api/v1/variables/{variable_id}")
def get_variable(variable_id: str) -> Dict[str, Any]:
    return ensure_exists(main.storage.get_variable(variable_id), "variable", variable_id)
