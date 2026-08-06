"""Rule, scorecard, and decision-table endpoints — CRUD, test, promote, analyze, evaluate.
Extracted verbatim from app/main.py.

Stable helpers + request models are imported by value from app.main (they internally read the live
main.storage); direct storage calls in handler bodies use main.storage live."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query

from .. import main
from ..logic import generate_rule_expression_definition, now_iso, slugify
from ..main import (
    DecisionTableDraft,
    DecisionTableEvaluateRequest,
    DecisionTableRequest,
    PromoteRequest,
    RuleUpsertRequest,
    ScorecardUpsertRequest,
    TestPayloadRequest,
    active_tenant_id,
    current_rule_map,
    current_scorecard_map,
    current_variable_map,
    ensure_exists,
    make_id,
    maybe_compile_bundle,
    normalize_rule_payload,
    promote_entity,
    record_error,
    test_rule_entity,
    test_scorecard_entity,
    validate_scorecard_bins,
)

router = APIRouter()


# ── Rules ──────────────────────────────────────────────────────────────────
@router.get("/api/v1/rules")
def list_rules(status: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    return main.storage.list_rules(status=status)


@router.get("/api/v1/rules/{rule_id}")
def get_rule(rule_id: str) -> Dict[str, Any]:
    return ensure_exists(main.storage.get_rule(rule_id), "rule", rule_id)


@router.post("/api/v1/rules")
def create_rule(request: RuleUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:
    normalized = normalize_rule_payload(request)
    rule_id = make_id(request.name, current_rule_map())
    created = main.storage.create_rule(
        {
            "id": rule_id,
            "name": request.name,
            "nodes": normalized["nodes"],
            "tree": normalized["tree"],
            "rule_format": normalized["rule_format"],
            "expression": generate_rule_expression_definition(normalized, current_variable_map()),
            "status": request.status,
            "last_test_result": None,
            "version": 1,
        }
    )
    if created["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return created


@router.put("/api/v1/rules/{rule_id}")
def update_rule(
    rule_id: str,
    request: RuleUpsertRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    normalized = normalize_rule_payload(request)
    existing = ensure_exists(main.storage.get_rule(rule_id), "rule", rule_id)
    updated = main.storage.update_rule(
        rule_id,
        {
            "name": request.name,
            "nodes": normalized["nodes"],
            "tree": normalized["tree"],
            "rule_format": normalized["rule_format"],
            "expression": generate_rule_expression_definition(normalized, current_variable_map()),
            "status": request.status,
        },
    )
    if existing["status"] == "prod" or request.status == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return ensure_exists(updated, "rule", rule_id)


@router.delete("/api/v1/rules/{rule_id}")
def delete_rule(rule_id: str) -> Dict[str, Any]:
    rule = ensure_exists(main.storage.get_rule(rule_id), "rule", rule_id)
    if rule["status"] != "dev":
        raise HTTPException(status_code=409, detail="Only DEV rules can be deleted.")
    return main.storage.delete_rule(rule_id) or rule


@router.post("/api/v1/rules/{rule_id}/test")
@router.post("/api/v1/test/rule/{rule_id}")
def test_rule(rule_id: str, request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    rule = ensure_exists(main.storage.get_rule(rule_id), "rule", rule_id)
    result = test_rule_entity(rule, request.payload)
    if not result["result"].get("passed"):
        record_error("rules", "test", "Rule test did not pass.", "rule", rule_id, {"outcome": result["result"]})
    return result


@router.post("/api/v1/rules/{rule_id}/promote")
def promote_rule(
    rule_id: str,
    request: PromoteRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    promoted = promote_entity("rule", rule_id, request.promoted_by, request.reason)
    if promoted["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return promoted


# ── Scorecards ─────────────────────────────────────────────────────────────
@router.get("/api/v1/scorecards")
def list_scorecards(status: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    return main.storage.list_scorecards(status=status)


@router.get("/api/v1/scorecards/{scorecard_id}")
def get_scorecard(scorecard_id: str) -> Dict[str, Any]:
    return ensure_exists(main.storage.get_scorecard(scorecard_id), "scorecard", scorecard_id)


@router.post("/api/v1/scorecards")
def create_scorecard(request: ScorecardUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:
    bins = [item.model_dump() for item in request.bins]
    validate_scorecard_bins(bins)
    scorecard_id = make_id(request.name, current_scorecard_map())
    created = main.storage.create_scorecard(
        {
            "id": scorecard_id,
            "name": request.name,
            "base_score": request.base_score,
            "max_score": request.max_score,
            "bins": bins,
            "status": request.status,
            "last_test_result": None,
            "version": 1,
        }
    )
    if created["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return created


@router.put("/api/v1/scorecards/{scorecard_id}")
def update_scorecard(
    scorecard_id: str,
    request: ScorecardUpsertRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    bins = [item.model_dump() for item in request.bins]
    validate_scorecard_bins(bins)
    existing = ensure_exists(main.storage.get_scorecard(scorecard_id), "scorecard", scorecard_id)
    updated = main.storage.update_scorecard(
        scorecard_id,
        {
            "name": request.name,
            "base_score": request.base_score,
            "max_score": request.max_score,
            "bins": bins,
            "status": request.status,
        },
    )
    if existing["status"] == "prod" or request.status == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return ensure_exists(updated, "scorecard", scorecard_id)


@router.delete("/api/v1/scorecards/{scorecard_id}")
def delete_scorecard(scorecard_id: str) -> Dict[str, Any]:
    scorecard = ensure_exists(main.storage.get_scorecard(scorecard_id), "scorecard", scorecard_id)
    if scorecard["status"] != "dev":
        raise HTTPException(status_code=409, detail="Only DEV scorecards can be deleted.")
    return main.storage.delete_scorecard(scorecard_id) or scorecard


@router.post("/api/v1/scorecards/{scorecard_id}/test")
def test_scorecard(scorecard_id: str, request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    scorecard = ensure_exists(main.storage.get_scorecard(scorecard_id), "scorecard", scorecard_id)
    return test_scorecard_entity(scorecard, request.payload)


@router.post("/api/v1/scorecards/{scorecard_id}/promote")
def promote_scorecard(
    scorecard_id: str,
    request: PromoteRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    promoted = promote_entity("scorecard", scorecard_id, request.promoted_by, request.reason)
    if promoted["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return promoted


# ── Decision tables ────────────────────────────────────────────────────────
@router.get("/api/v1/decision-tables")
def list_decision_tables() -> List[Dict[str, Any]]:
    return main.storage.list_decision_tables()


@router.get("/api/v1/decision-tables/{table_id}")
def get_decision_table(table_id: str) -> Dict[str, Any]:
    table = main.storage.get_decision_table(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Decision table not found.")
    return table


@router.post("/api/v1/decision-tables")
def create_decision_table(request: DecisionTableRequest) -> Dict[str, Any]:
    from ..decision_tables import analyze_decision_table

    table_id = slugify(request.name)
    existing_ids = {t["id"] for t in main.storage.list_decision_tables()}
    if table_id in existing_ids:
        table_id = f"{table_id}_{uuid.uuid4().hex[:6]}"
    data = request.model_dump()
    data["id"] = table_id
    created = main.storage.create_decision_table(data)
    created["analysis"] = analyze_decision_table(created)
    return created


@router.put("/api/v1/decision-tables/{table_id}")
def update_decision_table(table_id: str, request: DecisionTableRequest) -> Dict[str, Any]:
    from ..decision_tables import analyze_decision_table

    updated = main.storage.update_decision_table(table_id, request.model_dump())
    if not updated:
        raise HTTPException(status_code=404, detail="Decision table not found.")
    updated["analysis"] = analyze_decision_table(updated)
    return updated


@router.delete("/api/v1/decision-tables/{table_id}")
def delete_decision_table(table_id: str) -> Dict[str, Any]:
    if not main.storage.delete_decision_table(table_id):
        raise HTTPException(status_code=404, detail="Decision table not found.")
    return {"deleted": True, "id": table_id}


@router.post("/api/v1/decision-tables/analyze")
def analyze_decision_table_draft(draft: DecisionTableDraft) -> Dict[str, Any]:
    """Run the optimiser on an unsaved draft — conflicts, gaps, unreachable rows,
    invalid values. Powers the live authoring overlay."""
    from ..decision_tables import analyze_decision_table

    return analyze_decision_table(draft.model_dump())


@router.post("/api/v1/decision-tables/{table_id}/analyze")
def analyze_saved_decision_table(table_id: str) -> Dict[str, Any]:
    from ..decision_tables import analyze_decision_table

    table = main.storage.get_decision_table(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Decision table not found.")
    return analyze_decision_table(table)


@router.post("/api/v1/decision-tables/{table_id}/evaluate")
def evaluate_saved_decision_table(table_id: str, request: DecisionTableEvaluateRequest) -> Dict[str, Any]:
    from ..decision_tables import evaluate_decision_table

    table = main.storage.get_decision_table(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Decision table not found.")
    result = evaluate_decision_table(table, request.variable_values)
    main.storage.update_decision_table(table_id, {"last_test_result": {
        "outcome": result.get("outcome"),
        "winning_row_id": result.get("winning_row_id"),
        "tested_at": now_iso(),
    }})
    return result
