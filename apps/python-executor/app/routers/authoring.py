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
from ..connector_probe import ConnectorMisconfigured, probe_connector, redact_connector_secrets
from ..logic import now_iso
from ..main import (
    ConnectorCreateRequest,
    ConnectorUpdateRequest,
    PromoteRequest,
    TestPayloadRequest,
    VariableDraftTestRequest,
    VariableLintRequest,
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
    connector_id = make_id(request.name, current_connectors(), getattr(request, "id", None))
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
    """Actually connect to the connector's endpoint and report what happened.

    This used to return ``passed: true`` unconditionally without opening a socket, which made the
    dashboard's "Test connection" button a source of false confidence right before a production
    cutover. See ``app/connector_probe.py`` for why the egress guard and the secret redaction are
    not optional here, and why a misconfigured connector is a 422 while a downstream failure is a
    200 carrying ``passed: false``.
    """
    connector = ensure_exists(
        main.storage.get_connector(connector_id, include_secrets=True), "connector", connector_id
    )
    try:
        probe = probe_connector(connector)
    except ConnectorMisconfigured as bad:
        record_error("connectors", "connector_test", bad.reason, "connector", connector_id, {"code": bad.code})
        raise HTTPException(status_code=422, detail=bad.reason) from bad
    if not probe["passed"]:
        record_error(
            "connectors",
            "connector_test",
            "{0}: {1}".format(probe["outcome"], probe["reason"]),
            "connector",
            connector_id,
            {"outcome": probe["outcome"], "status": probe["status"]},
        )
    config = connector.get("config") or {}
    result = {
        "connector_id": connector["id"],
        "passed": probe["passed"],
        "probe": probe,
        "schema_paths": connector["schema_paths"],
        "sample_payload": connector["sample_payload"],
        "config_summary": {
            "auth_type": config.get("auth_type", "api_key"),
            "base_url": config.get("base_url", ""),
            "has_webhook": bool(config.get("webhook_url")),
        },
    }
    # The connector was loaded WITH secrets so the probe could authenticate. Nothing derived from
    # them may leave here — scrubbed by value (a secret pasted into base_url's query, or echoed
    # inside a transport error) and then by key name, over the whole body at once.
    scrubbed: Dict[str, Any] = redact_connector_secrets(result, connector)
    return scrubbed


# ── Variables ──────────────────────────────────────────────────────────────
@router.get("/api/v1/variables")
def list_variables(
    source: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    return main.storage.list_variables(source_id=source, status=status, category=category)


@router.post("/api/v1/variables")
def create_variable(request: VariableUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:  # type: ignore[assignment]
    connectors = current_connectors()
    if request.source_id not in connectors:
        raise HTTPException(status_code=422, detail="Unknown source connector.")
    variable_id = make_id(request.name, current_variable_map(), getattr(request, "id", None))
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
    # FastAPI injects this; Optional[BackgroundTasks] is rejected by its response-field
    # validation at import time, so the bare annotation is required here.
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
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


@router.post("/api/v1/variables/lint")
def lint_variable_draft(request: VariableLintRequest) -> Dict[str, Any]:
    """Diagnostics for the editor, WITHOUT running the code (P13).

    The editor needs to say "line 4: unexpected indent" or "line 7: Import not allowed: os"
    before the author clicks Test. Doing that in the browser would mean a Python parser in
    JavaScript and a second copy of the sandbox policy -- two implementations that would drift
    from the ones that actually decide whether the code runs, and a linter that disagrees with
    the runtime is worse than no linter, because the author learns to ignore it.

    So this reuses `sandbox.validate_source`: the exact function the sandbox calls before
    executing anything. A diagnostic here is therefore not an approximation of the rule, it IS
    the rule.

    It does not EXECUTE. `test-draft` above already does that, in a real sandbox with a timeout
    and a memory cap; this is the cheap check that runs while someone is typing, so it must not
    spawn a process or touch a connector.
    """
    from ..sandbox import validate_source

    code = request.code or ""
    if not code.strip():
        # A FAST PATH, not a correctness guard -- `ast.parse("")` succeeds, so removing this
        # returns the same answer. It exists because an empty editor is the state every new
        # variable starts in and the linter fires on a timer: this skips a parse and a cache
        # write on the most common input. Stated plainly because a mutation removing it changes
        # nothing observable, and a comment claiming otherwise would send someone hunting.
        return {"ok": True, "diagnostics": []}
    try:
        validate_source(code)
    except SyntaxError as bad:
        # SyntaxError carries the position; ValueError (a policy violation) does not, so the two
        # are reported differently rather than pretending to a precision only one of them has.
        return {"ok": False, "diagnostics": [{
            "severity": "error",
            "message": bad.msg or "Syntax error",
            "line": bad.lineno or 1,
            "column": bad.offset or 1,
            "kind": "syntax",
        }]}
    except ValueError as refused:
        return {"ok": False, "diagnostics": [{
            "severity": "error",
            "message": str(refused),
            # No line number: validate_source raises on an AST walk without carrying the node
            # position out. Reporting line 1 would put the marker on the wrong line, which is
            # worse than putting it on none -- the message names the symbol.
            "line": None,
            "column": None,
            "kind": "policy",
        }]}
    return {"ok": True, "diagnostics": []}


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
        record_error("variables", "draft_test", str(result["result"]["error"]), "variable", None, {"source_id": request.source_id})
    return result


@router.post("/api/v1/variables/{variable_id}/promote")
def promote_variable(
    variable_id: str,
    request: PromoteRequest = Body(default=PromoteRequest()),
    # FastAPI injects this; Optional[BackgroundTasks] is rejected by its response-field
    # validation at import time, so the bare annotation is required here.
    background_tasks: BackgroundTasks = None,  # type: ignore[assignment]
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
