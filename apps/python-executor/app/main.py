import copy
import asyncio
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Body, Cookie, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from .auth import JWT_COOKIE_NAME, bcrypt_verify, create_admin_jwt, decode_admin_jwt
from .analytics import decision_analytics, experiment_analytics, latency_analytics, sdk_analytics
from .compiler import BundleCompilationError, NoProductionAssetsError, compile_bundle, render_bundle_response
from .context import get_current_tenant_id
from .experience_studio import ADMIN_ENTITY_SCHEMAS, build_experience_manifest
from .executor import ExecutionContext, PolicyExecutor
from .logic import (
    execute_policy,
    flatten_tree_to_nodes,
    export_bundle,
    find_by_id,
    generate_rule_expression_definition,
    now_iso,
    redact_payload,
    evaluate_rule_definition,
    evaluate_rule_nodes,
    evaluate_scorecard,
    json_dumps,
    nodes_to_tree,
    slugify,
)
from .middleware import TenantContextMiddleware, admin_cookie_secure
from .reviews import submit_review_decision
from .runtime import is_local_dev, redis_client
from .sandbox import execute_variable
from .scheduler import execute_cron_policy, init_scheduler
from .storage import Storage
from .webhooks import WebhookAuthenticationError, trigger_webhook


def parse_origins() -> List[str]:
    raw = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    if "*" in origins:
        return ["*"]
    expanded = set(origins)
    for item in list(origins):
        if "localhost" in item:
            expanded.add(item.replace("localhost", "127.0.0.1"))
        if "127.0.0.1" in item:
            expanded.add(item.replace("127.0.0.1", "localhost"))
    return sorted(expanded)


storage = Storage()


def current_storage() -> Storage:
    return storage


app = FastAPI(title="RuleMind V4 API", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TenantContextMiddleware, storage=current_storage)


DECISIONS_TOTAL = Counter("rulemind_decisions_total", "RuleMind decisions", ["outcome", "source"])
DECISION_LATENCY = Histogram("rulemind_decision_latency_seconds", "RuleMind decision latency", ["source"])
BUNDLE_SYNCS_TOTAL = Counter("rulemind_bundle_syncs_total", "RuleMind bundle syncs", ["status"])
EVENTS_INGESTED_TOTAL = Counter("rulemind_events_ingested_total", "RuleMind SDK events ingested")


ALLOWED_NODE_TYPES = {"condition", "and", "or", "approve", "review", "reject"}
ALLOWED_OPERATORS = {">=", "<=", "==", ">", "<", "!="}
PROMOTION_FLOW = {"dev": "uat", "uat": "prod"}


class ConnectorUpdateRequest(BaseModel):
    name: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    schema_paths: Optional[List[str]] = None
    sample_payload: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None


class ConnectorCreateRequest(BaseModel):
    name: str
    icon: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    schema_paths: List[str] = Field(default_factory=list)
    sample_payload: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class TestPayloadRequest(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)


class PromoteRequest(BaseModel):
    promoted_by: str = "system"
    reason: str = "Manual promotion"


class VariableUpsertRequest(BaseModel):
    name: str
    category: str
    source_id: str
    code: str
    description: Optional[str] = None
    status: str = "dev"


class VariableDraftTestRequest(BaseModel):
    source_id: str
    code: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class RuleNodeModel(BaseModel):
    id: str
    type: str
    variable: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    label: Optional[str] = None


class RuleUpsertRequest(BaseModel):
    name: str
    nodes: List[RuleNodeModel] = Field(default_factory=list)
    tree: Optional[Dict[str, Any]] = None
    ruleFormat: str = "v1"
    status: str = "dev"


class ScorecardRangeModel(BaseModel):
    min: float
    max: float
    points: int = 0


class WoERangeModel(BaseModel):
    min: float
    max: float
    woe: float = 0.0
    iv: float = 0.0
    event_rate: float = 0.0
    non_event_rate: float = 0.0
    points: int = 0


class MetricFormulaModel(BaseModel):
    name: str
    formula: str
    unit: str = ""
    category: str = "financial"


class ScorecardBinModel(BaseModel):
    variable_id: str
    ranges: List[ScorecardRangeModel] = Field(default_factory=list)
    weight: float = 1.0
    woe_values: Optional[List[WoERangeModel]] = None
    coefficient: float = 0.0


class ScorecardUpsertRequest(BaseModel):
    name: str
    base_score: int = 300
    max_score: int = 900
    bins: List[ScorecardBinModel]
    scoring_method: str = "points"
    intercept: float = 0.0
    pdo: float = 20.0
    target_score: float = 600.0
    target_odds: float = 50.0
    formula: Optional[str] = None
    metrics: Optional[List[MetricFormulaModel]] = None
    status: str = "dev"


class PolicyStepModel(BaseModel):
    type: str
    ref_id: Optional[str] = None
    ref: Optional[str] = None
    id: Optional[str] = None
    name: Optional[str] = None
    label: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class PolicyUpsertRequest(BaseModel):
    name: str
    steps: List[PolicyStepModel]
    trigger: Optional[Dict[str, Any]] = None
    defaultOutcome: Optional[str] = None
    status: str = "dev"


class DecideRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    policy_id: str = Field(alias="policyId")
    payload: Dict[str, Any] = Field(default_factory=dict)


class BatchSimulationRequest(BaseModel):
    targetType: str
    targetId: Optional[str] = None
    payloads: List[Dict[str, Any]] = Field(default_factory=list)


class DeployItemModel(BaseModel):
    entity_type: str
    entity_id: str
    promoted_by: str = "system"
    reason: str = "Batch promotion"


class DeployPromoteRequest(BaseModel):
    items: List[DeployItemModel]


class ImportRequest(BaseModel):
    connectors: List[Dict[str, Any]] = Field(default_factory=list)
    variables: List[Dict[str, Any]] = Field(default_factory=list)
    rules: List[Dict[str, Any]] = Field(default_factory=list)
    scorecards: List[Dict[str, Any]] = Field(default_factory=list)
    policies: List[Dict[str, Any]] = Field(default_factory=list)
    settings: Dict[str, Any] = Field(default_factory=dict)


class SettingsRequest(BaseModel):
    api_base_url: Optional[str] = None
    auth_config: Optional[Dict[str, Any]] = None
    engine_config: Optional[Dict[str, Any]] = None
    source_defaults: Optional[Dict[str, Any]] = None
    audit_retention_days: Optional[int] = None
    theme_mode: Optional[str] = None


class TenantCreateRequest(BaseModel):
    name: str
    plan: str = "standard"
    config: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class TenantUpdateRequest(BaseModel):
    name: Optional[str] = None
    plan: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class AdminLoginRequest(BaseModel):
    email: str
    password: str


class MobileAdminLoginRequest(BaseModel):
    email: str
    password: str
    tenantId: Optional[str] = None


class ExperimentUpsertRequest(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    status: str = "draft"
    variants: List[Dict[str, Any]] = Field(default_factory=list)
    hash_key: str = "user_id"
    target_policy_id: Optional[str] = None


class ExperimentStatusRequest(BaseModel):
    status: str


class SdkDecideRequest(BaseModel):
    policyId: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    userId: Optional[str] = None
    requestId: Optional[str] = None
    sdkVersion: Optional[str] = None


class SdkEventsRequest(BaseModel):
    events: List[Dict[str, Any]] = Field(default_factory=list)


class SdkExecutionSyncRequest(BaseModel):
    executionId: str
    policyId: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    userId: Optional[str] = None
    outcome: str = "pending"
    status: str = "running"
    variables: Dict[str, Any] = Field(default_factory=dict)
    ruleResults: List[Dict[str, Any]] = Field(default_factory=list)
    scorecardResults: Dict[str, Any] = Field(default_factory=dict)
    actionResults: List[Dict[str, Any]] = Field(default_factory=list)
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    pendingOperations: List[Dict[str, Any]] = Field(default_factory=list)
    reviewTask: Optional[Dict[str, Any]] = None
    experimentId: Optional[str] = None
    experimentVariant: Optional[str] = None
    latencyMs: int = 0
    startedAt: Optional[str] = None
    completedAt: Optional[str] = None
    requestId: Optional[str] = None
    sdkVersion: Optional[str] = None
    source: str = "sdk_edge"


class SdkResumeExecutionRequest(BaseModel):
    decision: Optional[str] = None
    reviewerId: Optional[str] = None
    response: Dict[str, Any] = Field(default_factory=dict)


class WebhookUpsertRequest(BaseModel):
    policy_id: str
    is_active: bool = True
    secret: Optional[str] = None
    payload_mapping: Dict[str, Any] = Field(default_factory=dict)


class ScheduleUpsertRequest(BaseModel):
    policy_id: str
    cron_expression: str
    is_active: bool = True
    payload_source: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)


class ReviewDecisionRequest(BaseModel):
    decision: str
    reviewer_id: str
    response: Dict[str, Any] = Field(default_factory=dict)


def current_connectors() -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in storage.list_connectors()}


def current_variables() -> List[Dict[str, Any]]:
    return storage.list_variables()


def current_variable_map() -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in current_variables()}


def current_rule_map() -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in storage.list_rules()}


def current_scorecard_map() -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in storage.list_scorecards()}


def current_policy_map() -> Dict[str, Dict[str, Any]]:
    return {item["id"]: item for item in storage.list_policies()}


def ensure_exists(entity: Optional[Dict[str, Any]], label: str, entity_id: str) -> Dict[str, Any]:
    if not entity:
        raise HTTPException(status_code=404, detail="{0} '{1}' not found.".format(label, entity_id))
    return entity


def next_status(status: str) -> str:
    if status not in PROMOTION_FLOW:
        raise HTTPException(status_code=409, detail="Item is already in PROD.")
    return PROMOTION_FLOW[status]


def promotion_ready(entity: Dict[str, Any]) -> bool:
    result = entity.get("last_test_result")
    if not result:
        return False
    if isinstance(result, dict):
        if "passed" in result:
            return bool(result.get("passed"))
        return result.get("error") in (None, "")
    return False


def ensure_promotable(entity: Dict[str, Any]) -> None:
    if not promotion_ready(entity):
        raise HTTPException(status_code=409, detail="Latest test must pass before promotion.")


def make_id(name: str, existing: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    base = slugify(name) or "item"
    existing_map = existing or {}
    if base not in existing_map:
        return base
    index = 2
    while "{0}_{1}".format(base, index) in existing_map:
        index += 1
    return "{0}_{1}".format(base, index)


def connector_payload(source_id: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    connectors = current_connectors()
    connector = connectors.get(source_id, {})
    if not payload:
        return copy.deepcopy(connector.get("sample_payload", {}))
    if source_id in payload and isinstance(payload[source_id], dict):
        return payload[source_id]
    return payload


def payload_map(payload: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    connectors = current_connectors()
    normalized: Dict[str, Dict[str, Any]] = {}
    for connector_id, connector in connectors.items():
        normalized[connector_id] = copy.deepcopy(connector.get("sample_payload", {}))

    if not payload:
        return normalized

    for key, value in payload.items():
        if key in connectors and isinstance(value, dict):
            normalized[key] = value
    if not any(key in connectors for key in payload.keys()):
        normalized["custom"] = payload
    return normalized


def engine_limits() -> Dict[str, int]:
    settings = storage.get_settings().get("engine_config", {})
    timeout_ms = int(settings.get("timeout_ms", os.getenv("PYTHON_SANDBOX_TIMEOUT", "2000")))
    memory_mb = int(settings.get("memory_mb", os.getenv("PYTHON_SANDBOX_MEMORY", "128")))
    return {"timeout_ms": timeout_ms, "memory_mb": memory_mb}


def compute_variable_values(
    payloads: Dict[str, Dict[str, Any]],
    variables: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    variable_list = variables if variables is not None else current_variables()
    connectors = current_connectors()
    limits = engine_limits()
    computed_values: Dict[str, Any] = {}
    results: List[Dict[str, Any]] = []

    # Group variables by source so independent sources execute in parallel
    source_groups: Dict[str, List[Dict[str, Any]]] = {}
    for variable in variable_list:
        source_groups.setdefault(variable["source_id"], []).append(variable)

    def _execute_group(group_variables: List[Dict[str, Any]]) -> List[tuple]:
        group_computed: Dict[str, Any] = {}
        group_results = []
        for variable in group_variables:
            source_payload = payloads.get(variable["source_id"], {})
            execution = execute_variable(
                variable["code"],
                source_payload,
                group_computed,
                timeout_ms=limits["timeout_ms"],
                memory_mb=limits["memory_mb"],
            )
            group_computed[variable["id"]] = execution.get("value")
            group_results.append((variable, execution, dict(group_computed)))
        return group_results

    with ThreadPoolExecutor(max_workers=min(len(source_groups), 8)) as executor:
        futures = {
            executor.submit(_execute_group, group_vars): source_id
            for source_id, group_vars in source_groups.items()
        }
        for future in as_completed(futures):
            for variable, execution, group_computed in future.result():
                computed_values[variable["id"]] = execution.get("value")
                results.append(
                    {
                        "id": variable["id"],
                        "name": variable["name"],
                        "category": variable["category"],
                        "source_id": variable["source_id"],
                        "source_name": connectors.get(variable["source_id"], {}).get("name"),
                        "source_icon": connectors.get(variable["source_id"], {}).get("icon"),
                        "source_active": connectors.get(variable["source_id"], {}).get("is_active", False),
                        "value": execution.get("value"),
                        "error": execution.get("error"),
                        "latency_ms": execution.get("latency_ms"),
                        "variable_name": execution.get("variable_name"),
                        "passed": execution.get("error") in (None, ""),
                    }
                )
    computed_values.update({r["id"]: r["value"] for r in results})
    return {"values": computed_values, "results": results}


def validate_rule_nodes(nodes: List[Dict[str, Any]]) -> None:
    if not nodes:
        raise HTTPException(status_code=422, detail="Rules require at least one node.")

    variable_map = current_variable_map()
    outcome_count = 0
    for node in nodes:
        node_type = node.get("type")
        if node_type not in ALLOWED_NODE_TYPES:
            raise HTTPException(status_code=422, detail="Unsupported node type: {0}".format(node_type))
        if node_type == "condition":
            if not node.get("variable") or node.get("variable") not in variable_map:
                raise HTTPException(status_code=422, detail="Condition nodes require a valid variable.")
            if node.get("operator") not in ALLOWED_OPERATORS:
                raise HTTPException(status_code=422, detail="Unsupported operator: {0}".format(node.get("operator")))
        if node_type in {"approve", "review", "reject"}:
            outcome_count += 1
    if outcome_count == 0:
        raise HTTPException(status_code=422, detail="Rules require an outcome node.")


def validate_rule_tree(tree: Dict[str, Any], depth: int = 0) -> None:
    variable_map = current_variable_map()
    node_type = tree.get("type")
    if depth > 3:
        raise HTTPException(status_code=422, detail="Rules may only nest groups three levels deep.")
    if node_type == "condition":
        if not tree.get("variable") or tree.get("variable") not in variable_map:
            raise HTTPException(status_code=422, detail="Condition nodes require a valid variable.")
        if tree.get("operator") not in ALLOWED_OPERATORS:
            raise HTTPException(status_code=422, detail="Unsupported operator: {0}".format(tree.get("operator")))
        return
    if node_type == "not":
        child = tree.get("child")
        if not isinstance(child, dict):
            raise HTTPException(status_code=422, detail="NOT nodes require a child rule node.")
        validate_rule_tree(child, depth + 1)
        return
    if node_type == "group":
        logic = str(tree.get("logic", "AND")).upper()
        if logic not in {"AND", "OR"}:
            raise HTTPException(status_code=422, detail="Group logic must be AND or OR.")
        children = tree.get("children") or []
        if not children:
            raise HTTPException(status_code=422, detail="Rule groups require one or more child nodes.")
        for child in children:
            if not isinstance(child, dict):
                raise HTTPException(status_code=422, detail="Invalid child node in rule tree.")
            validate_rule_tree(child, depth + 1)
        if tree.get("onPass") not in {"approve", "review", "reject"}:
            raise HTTPException(status_code=422, detail="Rule tree onPass must be approve, review, or reject.")
        if tree.get("onFail") not in {"approve", "review", "reject"}:
            raise HTTPException(status_code=422, detail="Rule tree onFail must be approve, review, or reject.")
        return
    raise HTTPException(status_code=422, detail="Unsupported rule tree node type: {0}".format(node_type))


def normalize_rule_payload(request: RuleUpsertRequest) -> Dict[str, Any]:
    nodes = [item.model_dump() for item in request.nodes]
    rule_format = request.ruleFormat if request.ruleFormat in {"v1", "v2"} else ("v2" if request.tree else "v1")
    tree = copy.deepcopy(request.tree) if request.tree else None
    if tree:
        validate_rule_tree(tree)
        if not nodes:
            nodes = flatten_tree_to_nodes(tree)
        rule_format = "v2"
    else:
        validate_rule_nodes(nodes)
        tree = nodes_to_tree(nodes)
        rule_format = "v1"
    return {"nodes": nodes, "tree": tree, "rule_format": rule_format}


def validate_scorecard_bins(bins: List[Dict[str, Any]]) -> None:
    variable_map = current_variable_map()
    if not bins:
        raise HTTPException(status_code=422, detail="Scorecards require at least one variable bin.")
    for factor in bins:
        variable_id = factor.get("variable_id")
        if variable_id not in variable_map:
            raise HTTPException(status_code=422, detail="Unknown variable in scorecard: {0}".format(variable_id))
        if not factor.get("ranges"):
            raise HTTPException(status_code=422, detail="Scorecard bins require one or more ranges.")


def validate_policy_steps(steps: List[Dict[str, Any]]) -> None:
    rules_map = current_rule_map()
    scorecards_map = current_scorecard_map()
    connectors = current_connectors()
    if not steps:
        raise HTTPException(status_code=422, detail="Policies require at least one step.")
    for step in steps:
        step_type = step.get("type")
        ref_id = step.get("ref_id") or step.get("ref")
        if step_type == "connector" and ref_id not in connectors:
            raise HTTPException(status_code=422, detail="Unknown connector in policy: {0}".format(ref_id))
        if step_type == "rule" and ref_id not in rules_map:
            raise HTTPException(status_code=422, detail="Unknown rule in policy: {0}".format(ref_id))
        if step_type == "scorecard" and ref_id not in scorecards_map:
            raise HTTPException(status_code=422, detail="Unknown scorecard in policy: {0}".format(ref_id))
        if step_type not in {"connector", "rule", "scorecard", "outcome", "transform", "action", "review_gate"}:
            raise HTTPException(status_code=422, detail="Unsupported policy step type: {0}".format(step_type))


def record_error(scope: str, stage: str, message: str, entity_type: Optional[str] = None, entity_id: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> None:
    storage.add_error_event(
        {
            "scope": scope,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "stage": stage,
            "message": message,
            "details": details or {},
            "created_at": now_iso(),
        }
    )


def test_variable_entity(variable: Dict[str, Any], payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    source_payload = connector_payload(variable["source_id"], payload)
    limits = engine_limits()
    started = time.perf_counter()
    execution = execute_variable(
        variable["code"],
        source_payload,
        {},
        timeout_ms=limits["timeout_ms"],
        memory_mb=limits["memory_mb"],
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    last_test_result = {
        "value": execution.get("value"),
        "error": execution.get("error"),
        "latency_ms": latency_ms,
        "tested_at": now_iso(),
        "passed": execution.get("error") in (None, ""),
    }
    updated = storage.update_variable(variable["id"], {"last_test_result": last_test_result}, bump_version=False)
    return {"variable": updated or variable, "result": last_test_result}


def test_rule_entity(rule: Dict[str, Any], payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payloads = payload_map(payload)
    variable_lookup = current_variable_map()
    started = time.perf_counter()
    variable_results = compute_variable_values(payloads)
    evaluation = evaluate_rule_definition(rule, variable_results["values"], variable_lookup)
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    last_test_result = {
        "passed": evaluation["passed"],
        "outcome": evaluation["outcome"],
        "conditions": evaluation["conditions"],
        "groupResults": evaluation.get("groupResults", []),
        "latency_ms": latency_ms,
        "tested_at": now_iso(),
    }
    updated = storage.update_rule(
        rule["id"],
        {
            "expression": generate_rule_expression_definition(rule, variable_lookup),
            "last_test_result": last_test_result,
        },
        bump_version=False,
    )
    return {
        "rule": updated or rule,
        "expression": generate_rule_expression_definition(rule, variable_lookup),
        "variable_results": variable_results["results"],
        "values": variable_results["values"],
        "result": last_test_result,
    }


def test_scorecard_entity(scorecard: Dict[str, Any], payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payloads = payload_map(payload)
    variable_lookup = current_variable_map()
    started = time.perf_counter()
    variable_results = compute_variable_values(payloads)
    evaluation = evaluate_scorecard(scorecard, variable_results["values"], variable_lookup)
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    last_test_result = {
        "passed": True,
        "score": evaluation["score"],
        "latency_ms": latency_ms,
        "tested_at": now_iso(),
    }
    updated = storage.update_scorecard(scorecard["id"], {"last_test_result": last_test_result}, bump_version=False)
    return {"scorecard": updated or scorecard, "variable_results": variable_results["results"], "result": evaluation}


def test_policy_entity(policy: Dict[str, Any], payload: Optional[Dict[str, Any]], log_decision: bool = False) -> Dict[str, Any]:
    started = time.perf_counter()
    executor = workflow_executor()
    ctx = asyncio.run(executor.execute(policy=policy, payload=payload or {}, tenant_id=active_tenant_id(), source="test_console"))
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    variable_lookup = current_variable_map()
    connectors = current_connectors()
    variable_results = [
        {
            "id": variable_id,
            "name": variable_lookup.get(variable_id, {}).get("name", variable_id),
            "category": variable_lookup.get(variable_id, {}).get("category"),
            "source_id": variable_lookup.get(variable_id, {}).get("source_id"),
            "source_name": connectors.get(variable_lookup.get(variable_id, {}).get("source_id", ""), {}).get("name"),
            "source_icon": connectors.get(variable_lookup.get(variable_id, {}).get("source_id", ""), {}).get("icon"),
            "source_active": connectors.get(variable_lookup.get(variable_id, {}).get("source_id", ""), {}).get("is_active", False),
            "value": value,
            "error": None,
            "latency_ms": 0,
            "variable_name": variable_lookup.get(variable_id, {}).get("name", variable_id),
            "passed": True,
        }
        for variable_id, value in ctx.variables.items()
    ]
    scorecard_result = next(iter(ctx.scorecard_results.values()), None)
    outcome = {
        "policy_id": policy.get("id"),
        "outcome": ctx.outcome if ctx.outcome != "pending" else policy.get("defaultOutcome", "review"),
        "scorecard_result": scorecard_result,
        "trace": copy.deepcopy(ctx.step_trace),
        "status": ctx.status,
        "execution_id": ctx.execution_id,
    }
    last_test_result = {
        "passed": outcome["outcome"] in {"approve", "review", "reject"},
        "outcome": outcome["outcome"],
        "latency_ms": latency_ms,
        "tested_at": now_iso(),
    }
    updated = storage.update_policy(policy["id"], {"last_test_result": last_test_result}, bump_version=False)
    if log_decision:
        storage.add_decision(
            {
                "id": str(uuid.uuid4()),
                "policy_id": policy["id"],
                "payload": redact_payload(ctx.payload),
                "computed_variables": ctx.variables,
                "rule_results": copy.deepcopy(ctx.rule_results),
                "scorecard_result": scorecard_result,
                "trace": copy.deepcopy(ctx.step_trace),
                "outcome": outcome["outcome"],
                "latency_ms": int(latency_ms),
                "created_at": now_iso(),
            }
        )
    return {
        "policy": updated or policy,
        "variable_results": variable_results,
        "values": ctx.variables,
        "result": outcome,
        "latency_ms": latency_ms,
    }


def promote_entity(entity_type: str, entity_id: str, promoted_by: str, reason: str) -> Dict[str, Any]:
    update_map = {
        "variable": (storage.get_variable, storage.update_variable),
        "rule": (storage.get_rule, storage.update_rule),
        "scorecard": (storage.get_scorecard, storage.update_scorecard),
        "policy": (storage.get_policy, storage.update_policy),
    }
    if entity_type not in update_map:
        raise HTTPException(status_code=422, detail="Unsupported entity type for promotion.")
    getter, updater = update_map[entity_type]
    entity = ensure_exists(getter(entity_id), entity_type, entity_id)
    ensure_promotable(entity)
    target_status = next_status(entity["status"])
    updated = updater(entity_id, {"status": target_status}, bump_version=False)
    storage.add_promotion(entity_type, entity_id, entity["status"], target_status, promoted_by, reason)
    return ensure_exists(updated, entity_type, entity_id)


def require_platform_admin(admin_token: Optional[str]) -> Dict[str, Any]:
    if not admin_token:
        raise HTTPException(status_code=401, detail="Admin authentication required.")
    try:
        payload = decode_admin_jwt(admin_token)
    except Exception as error:  # pragma: no cover - defensive auth guard
        raise HTTPException(status_code=401, detail="Invalid admin session.") from error
    user = storage.get_platform_admin_user(str(payload.get("sub", "")))
    if not user or not user.get("is_active", False):
        raise HTTPException(status_code=401, detail="Admin session expired.")
    return user


def bearer_token(request: Request) -> Optional[str]:
    raw = request.headers.get("authorization", "")
    if raw.lower().startswith("bearer "):
        return raw.split(" ", 1)[1].strip() or None
    return None


def require_platform_admin_request(request: Request) -> Dict[str, Any]:
    return require_platform_admin(bearer_token(request))


def sanitize_admin_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in user.items() if key != "password_hash"}


def tenant_api_key_payload(tenant_id: str) -> Dict[str, Any]:
    if tenant_id == str(storage.default_tenant_id) and storage.default_api_key:
        existing = next((item for item in storage.list_api_keys(tenant_id) if item.get("is_active")), None)
        return {
            "kid": existing.get("kid") if existing else "default",
            "masked": existing.get("masked_key") if existing else storage.default_api_key[:8] + "****",
            "plaintext": storage.default_api_key,
        }
    created = storage.generate_api_key_for_tenant(tenant_id)
    return {"kid": created["kid"], "masked": created["masked_key"], "plaintext": created["plaintext"]}


def mobile_session_payload(
    *,
    base_url: str,
    user: Optional[Dict[str, Any]],
    tenant_id: str,
    mode: str,
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    tenant = storage.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    key_payload = tenant_api_key_payload(tenant_id)
    latest = storage.latest_bundle(tenant_id=tenant_id)
    manifest = build_experience_manifest(tenant_id, base_url, latest_bundle_version=int(latest["version"]) if latest else 0)
    return {
        "mode": mode,
        "accessToken": access_token,
        "user": sanitize_admin_user(user) if user else None,
        "tenant": tenant,
        "apiKey": key_payload["plaintext"],
        "apiKeyKid": key_payload["kid"],
        "apiKeyMasked": key_payload["masked"],
        "experienceManifest": manifest,
        "availableTenants": storage.list_tenants() if user else [tenant],
    }


def workflow_executor() -> PolicyExecutor:
    return PolicyExecutor(storage)


def maybe_compile_bundle(tenant_id: str, background_tasks: Optional[BackgroundTasks] = None, force: bool = False) -> None:
    if not storage.mark_bundle_compile_queued(tenant_id, force=force):
        return

    def _compile() -> None:
        try:
            compile_bundle(storage, tenant_id, force=True)
        except NoProductionAssetsError:
            return
        except BundleCompilationError as error:
            storage.add_error_event(
                {
                    "tenant_id": tenant_id,
                    "scope": "bundle",
                    "entity_type": "bundle",
                    "entity_id": error.entity_id,
                    "stage": "compile",
                    "message": str(error),
                    "details": {"entity_type": error.entity_type, "entity_id": error.entity_id},
                },
                tenant_id=tenant_id,
            )
        except Exception as error:
            storage.add_error_event(
                {
                    "tenant_id": tenant_id,
                    "scope": "bundle",
                    "entity_type": "bundle",
                    "entity_id": None,
                    "stage": "compile",
                    "message": str(error),
                    "details": {},
                },
                tenant_id=tenant_id,
            )

    if background_tasks is not None:
        background_tasks.add_task(_compile)
    else:
        _compile()


def active_tenant_id(request: Optional[Request] = None) -> str:
    if request is not None and getattr(request.state, "tenant_id", None):
        return str(request.state.tenant_id)
    if get_current_tenant_id():
        return str(get_current_tenant_id())
    return str(storage.default_tenant_id or "")


def public_api_base_url(request: Optional[Request] = None) -> str:
    configured = os.getenv("RULEMIND_PUBLIC_API_BASE_URL")
    if configured:
        return configured.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return storage.get_settings().get("api_base_url", "http://localhost:8080").rstrip("/")


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def normalize_sdk_rule_result(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized = copy.deepcopy(item)
    rule_id = normalized.get("ruleId") or normalized.get("rule_id")
    if rule_id is not None:
        normalized["ruleId"] = rule_id
        normalized["rule_id"] = rule_id
    return normalized


def build_sdk_response(
    ctx: ExecutionContext,
    *,
    request_id: Optional[str] = None,
    source: str = "sdk_server",
    latency_ms: Optional[int] = None,
) -> Dict[str, Any]:
    scorecard_results = copy.deepcopy(ctx.scorecard_results)
    rule_results = [normalize_sdk_rule_result(item) for item in ctx.rule_results]
    review_task = storage.get_review_task(ctx.review_task_id, tenant_id=ctx.tenant_id) if ctx.review_task_id else None
    pending_operations = copy.deepcopy(ctx.pending_operations)
    resolved_latency = latency_ms if latency_ms is not None else ctx.total_latency_ms
    callbacks = []
    blockers = []
    threshold_hits = []
    timeline = []
    for item in copy.deepcopy(ctx.step_trace):
        step = item.get("step", {})
        result = item.get("result") or {}
        step_type = step.get("type")
        step_id = step.get("id") or step.get("ref_id") or step.get("ref") or step.get("label") or step_type
        timeline.append(
            {
                "stepId": step_id,
                "label": step.get("label") or step.get("name") or step_id,
                "type": step_type,
                "status": "skipped" if item.get("skipped") else ("error" if item.get("error") else ("paused" if result.get("paused") else "completed")),
                "durationMs": item.get("duration_ms") or item.get("durationMs") or 0,
                "result": copy.deepcopy(result),
                "error": item.get("error"),
            }
        )
        if step_type == "action":
            callbacks.append(
                {
                    "stepId": step_id,
                    "label": step.get("label") or step.get("name") or step_id,
                    "queued": result.get("queued", False),
                    "blocking": result.get("blocking", False),
                    "status": result.get("status") or ("queued" if result.get("queued") else ("ok" if result.get("success") else "failed")),
                    "url": result.get("url"),
                    "method": result.get("method"),
                }
            )
            if result.get("blocking") and result.get("queued"):
                blockers.append({"kind": "callback", "label": step.get("label") or step_id, "detail": "Blocking callback is waiting for delivery."})
        if step_type == "review_gate" and result.get("paused"):
            blockers.append({"kind": "review", "label": step.get("label") or step_id, "detail": "Execution is waiting for reviewer input."})
    for rule_result in rule_results:
        for condition in rule_result.get("conditions", []):
            if not condition.get("passed"):
                threshold_hits.append(
                    {
                        "kind": "rule_condition",
                        "variableId": condition.get("variable_id"),
                        "label": condition.get("variable_name") or condition.get("variable_id"),
                        "value": condition.get("value"),
                        "threshold": condition.get("threshold"),
                        "operator": condition.get("operator"),
                    }
                )
        if rule_result.get("passed") is False:
            blockers.append(
                {
                    "kind": "rule_failure",
                    "label": rule_result.get("ruleId") or rule_result.get("rule_id"),
                    "detail": "Rule outcome was {0}.".format(rule_result.get("outcome") or "reject"),
                }
            )
    for scorecard_id, payload in scorecard_results.items():
        for breakdown in payload.get("breakdown", []):
            if float(breakdown.get("points", 0)) < 0:
                threshold_hits.append(
                    {
                        "kind": "scorecard_penalty",
                        "scorecardId": scorecard_id,
                        "label": breakdown.get("variable_name") or breakdown.get("variable_id"),
                        "value": breakdown.get("value"),
                        "points": breakdown.get("points"),
                    }
                )
    experiment = None
    if ctx.experiment_id or ctx.experiment_variant:
        experiment = {"id": ctx.experiment_id, "variant": ctx.experiment_variant}
    return {
        "outcome": ctx.outcome,
        "score": next(iter(scorecard_results.values()), {}).get("score") if scorecard_results else None,
        "variables": copy.deepcopy(ctx.variables),
        "ruleResults": rule_results,
        "experiment": experiment,
        "experimentId": ctx.experiment_id,
        "experimentVariant": ctx.experiment_variant,
        "latencyMs": resolved_latency,
        "requestId": request_id,
        "executionId": ctx.execution_id,
        "status": ctx.status,
        "trace": copy.deepcopy(ctx.step_trace),
        "scorecardResults": scorecard_results,
        "actionResults": copy.deepcopy(ctx.action_results),
        "reviewTask": review_task,
        "pendingOperations": pending_operations,
        "transformOutputs": copy.deepcopy(ctx.transform_outputs),
        "reviewResponse": copy.deepcopy(ctx.review_response),
        "startedAt": ctx.started_at.replace(microsecond=0).isoformat() + "Z",
        "completedAt": ctx.completed_at.replace(microsecond=0).isoformat() + "Z" if ctx.completed_at else None,
        "explainability": {
            "rules": rule_results,
            "scorecards": scorecard_results,
            "trace": copy.deepcopy(ctx.step_trace),
            "callbacks": callbacks,
            "thresholdHits": threshold_hits,
            "blockers": blockers,
            "timeline": timeline,
        },
        "auditSummary": {
            "source": source,
            "traceSteps": len(ctx.step_trace),
            "pendingOperationCount": len(pending_operations),
            "actionCount": len(ctx.action_results),
            "reviewTaskId": ctx.review_task_id,
            "startedAt": ctx.started_at.replace(microsecond=0).isoformat() + "Z",
            "completedAt": ctx.completed_at.replace(microsecond=0).isoformat() + "Z" if ctx.completed_at else None,
            "currentStepIndex": ctx.current_step_index,
        },
        "serverOnlyStepsSkipped": [],
    }


def persist_sdk_action_logs(execution_id: str, action_results: List[Dict[str, Any]], tenant_id: str) -> None:
    existing_logs = storage.list_action_logs(execution_id=execution_id, tenant_id=tenant_id)
    for item in action_results[len(existing_logs):]:
        if not item.get("url"):
            continue
        storage.add_action_log(
            {
                "tenant_id": tenant_id,
                "execution_id": execution_id,
                "step_id": item.get("step_id") or item.get("stepId"),
                "action_name": item.get("action_name") or item.get("actionName"),
                "url": item.get("url"),
                "method": item.get("method", "POST"),
                "request_body": item.get("request_body") or item.get("requestBody"),
                "response_status": item.get("response_status") or item.get("responseStatus"),
                "response_body": item.get("response_body") or item.get("responseBody"),
                "latency_ms": item.get("latency_ms") or item.get("latencyMs"),
                "success": item.get("success", False),
                "retry_count": item.get("retry_count") or item.get("retryCount") or 0,
                "error": item.get("error"),
            },
            tenant_id=tenant_id,
        )


def maybe_create_sdk_decision(request: SdkExecutionSyncRequest, tenant_id: str, existing: Optional[Dict[str, Any]]) -> None:
    if request.status not in {"completed", "failed"}:
        return
    if existing and existing.get("status") in {"completed", "failed"}:
        return
    storage.add_decision(
        {
            "tenant_id": tenant_id,
            "id": str(uuid.uuid4()),
            "policy_id": request.policyId,
            "payload": redact_payload(request.payload),
            "computed_variables": copy.deepcopy(request.variables),
            "rule_results": [normalize_sdk_rule_result(item) for item in request.ruleResults],
            "scorecard_result": next(iter(request.scorecardResults.values()), None) if request.scorecardResults else None,
            "trace": copy.deepcopy(request.trace),
            "outcome": request.outcome if request.outcome != "pending" else "review",
            "latency_ms": request.latencyMs,
            "source": request.source,
            "sdk_version": request.sdkVersion,
            "experiment_variant": request.experimentVariant,
        },
        tenant_id=tenant_id,
    )


def sync_sdk_review_task(request: SdkExecutionSyncRequest, tenant_id: str) -> Optional[Dict[str, Any]]:
    if request.status != "paused":
        return None
    task_payload = copy.deepcopy(request.reviewTask or {})
    task_id = str(task_payload.get("id") or "sdk_review_" + uuid.uuid4().hex[:12])
    existing_task = storage.get_review_task(task_id, tenant_id=tenant_id)
    patch = {
        "id": task_id,
        "tenant_id": tenant_id,
        "execution_id": request.executionId,
        "policy_id": request.policyId,
        "step_id": task_payload.get("step_id") or task_payload.get("stepId"),
        "queue": task_payload.get("queue", "mobile_review"),
        "status": task_payload.get("status", "pending"),
        "required_fields": task_payload.get("required_fields") or task_payload.get("requiredFields") or [],
        "context_snapshot": task_payload.get("context_snapshot") or task_payload.get("contextSnapshot") or {},
        "reviewer_response": task_payload.get("reviewer_response") or task_payload.get("reviewerResponse"),
        "reviewed_by": task_payload.get("reviewed_by") or task_payload.get("reviewedBy"),
        "timeout_at": parse_iso_datetime(task_payload.get("timeout_at") or task_payload.get("timeoutAt")),
        "reviewed_at": parse_iso_datetime(task_payload.get("reviewed_at") or task_payload.get("reviewedAt")),
    }
    if existing_task:
        return storage.update_review_task(task_id, patch, tenant_id=tenant_id)
    return storage.create_review_task(patch, tenant_id=tenant_id)


@app.on_event("startup")
async def startup() -> None:
    if not (is_local_dev() or os.getenv("RULEMIND_RUN_API_SCHEDULER") == "1"):
        return
    try:
        init_scheduler(storage)
    except Exception:
        # Scheduler failures should not block local development or tests.
        return


@app.get("/health")
@app.get("/api/v1/health")
def health() -> Dict[str, str]:
    try:
        with storage.connect():
            db_status = "ok"
    except Exception:
        db_status = "error"
    try:
        client = redis_client()
        redis_status = "ok" if client is not None and client.ping() else "error"
    except Exception:
        redis_status = "error"
    overall = "ok" if db_status == "ok" and redis_status == "ok" else "degraded"
    return {"status": overall, "db": db_status, "redis": redis_status, "version": "4.0.0"}


@app.get("/ready")
def ready() -> Dict[str, bool]:
    try:
        with storage.connect():
            client = redis_client()
            return {"ready": bool(client is not None and client.ping())}
    except Exception:
        return {"ready": False}


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/mobile/v1/auth/demo")
def mobile_demo_access(request: Request) -> Dict[str, Any]:
    tenant_id = str(storage.default_tenant_id or "")
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Default tenant is unavailable.")
    return mobile_session_payload(
        base_url=public_api_base_url(request),
        user=None,
        tenant_id=tenant_id,
        mode="demo",
    )


@app.post("/api/mobile/v1/auth/login")
def mobile_admin_login(request: MobileAdminLoginRequest, http_request: Request) -> Dict[str, Any]:
    user = storage.get_platform_admin_user_by_email(request.email)
    if not user or not user.get("is_active") or not bcrypt_verify(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    tenant_id = request.tenantId or str(storage.default_tenant_id or "")
    token = create_admin_jwt(user["id"], user["email"])
    return mobile_session_payload(
        base_url=public_api_base_url(http_request),
        user=user,
        tenant_id=tenant_id,
        mode="admin",
        access_token=token,
    )


@app.get("/api/mobile/v1/auth/me")
def mobile_admin_me(request: Request) -> Dict[str, Any]:
    user = require_platform_admin_request(request)
    return {"user": sanitize_admin_user(user), "availableTenants": storage.list_tenants()}


@app.post("/api/mobile/v1/tenants/{tenant_id}/session")
def mobile_switch_tenant(tenant_id: str, request: Request) -> Dict[str, Any]:
    user = require_platform_admin_request(request)
    return mobile_session_payload(
        base_url=public_api_base_url(request),
        user=user,
        tenant_id=tenant_id,
        mode="admin",
        access_token=bearer_token(request),
    )


@app.post("/api/admin/v1/auth/login")
def admin_login(request: AdminLoginRequest, response: Response) -> Dict[str, Any]:
    user = storage.get_platform_admin_user_by_email(request.email)
    if not user or not user.get("is_active") or not bcrypt_verify(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_admin_jwt(user["id"], user["email"])
    response.set_cookie(
        JWT_COOKIE_NAME,
        token,
        httponly=True,
        secure=admin_cookie_secure(),
        samesite="lax",
        path="/",
        max_age=60 * 60 * 12,
    )
    return {"user": {key: value for key, value in user.items() if key != "password_hash"}}


@app.get("/api/admin/v1/auth/me")
def admin_me(admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    return {"user": sanitize_admin_user(require_platform_admin(admin_token))}


@app.post("/api/admin/v1/auth/logout")
def admin_logout(response: Response) -> Dict[str, bool]:
    response.delete_cookie(JWT_COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/admin/v1/tenants")
def admin_list_tenants(admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> List[Dict[str, Any]]:
    require_platform_admin(admin_token)
    return storage.list_tenants()


@app.post("/api/admin/v1/tenants")
def admin_create_tenant(request: TenantCreateRequest, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    require_platform_admin(admin_token)
    return storage.create_tenant(request.name, plan=request.plan, config=request.config, is_active=request.is_active)


@app.get("/api/admin/v1/tenants/{tenant_id}")
def admin_get_tenant(tenant_id: str, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    require_platform_admin(admin_token)
    tenant = storage.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return tenant


@app.patch("/api/admin/v1/tenants/{tenant_id}")
def admin_update_tenant(tenant_id: str, request: TenantUpdateRequest, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    require_platform_admin(admin_token)
    tenant = storage.update_tenant(tenant_id, request.model_dump(exclude_none=True))
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return tenant


@app.post("/api/admin/v1/tenants/{tenant_id}/keys")
def admin_create_tenant_key(tenant_id: str, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, Any]:
    require_platform_admin(admin_token)
    try:
        return storage.generate_api_key_for_tenant(tenant_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/admin/v1/tenants/{tenant_id}/keys")
def admin_list_tenant_keys(tenant_id: str, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> List[Dict[str, Any]]:
    require_platform_admin(admin_token)
    return storage.list_api_keys(tenant_id)


@app.delete("/api/admin/v1/tenants/{tenant_id}/keys/{kid}")
def admin_revoke_tenant_key(tenant_id: str, kid: str, admin_token: Optional[str] = Cookie(default=None, alias=JWT_COOKIE_NAME)) -> Dict[str, bool]:
    require_platform_admin(admin_token)
    if not storage.revoke_api_key(tenant_id, kid):
        raise HTTPException(status_code=404, detail="API key not found.")
    return {"revoked": True}


@app.get("/api/v1/connectors")
def list_connectors() -> List[Dict[str, Any]]:
    return storage.list_connectors()


@app.post("/api/v1/connectors")
def create_connector(request: ConnectorCreateRequest) -> Dict[str, Any]:
    connector_id = make_id(request.name, current_connectors())
    return storage.create_connector(
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


@app.get("/api/v1/connectors/{connector_id}")
def get_connector(connector_id: str) -> Dict[str, Any]:
    return ensure_exists(storage.get_connector(connector_id), "connector", connector_id)


@app.put("/api/v1/connectors/{connector_id}")
def update_connector(connector_id: str, request: ConnectorUpdateRequest) -> Dict[str, Any]:
    connector = ensure_exists(storage.get_connector(connector_id), "connector", connector_id)
    updated = storage.update_connector(
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


@app.delete("/api/v1/connectors/{connector_id}")
def delete_connector(connector_id: str) -> Dict[str, Any]:
    connector = ensure_exists(storage.get_connector(connector_id), "connector", connector_id)
    if connector.get("is_active"):
        raise HTTPException(status_code=409, detail="Deactivate the connector before deleting it.")
    return storage.delete_connector(connector_id) or connector


@app.post("/api/v1/connectors/{connector_id}/test")
def test_connector(connector_id: str) -> Dict[str, Any]:
    connector = ensure_exists(storage.get_connector(connector_id), "connector", connector_id)
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


@app.get("/api/v1/variables")
def list_variables(
    source: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
    return storage.list_variables(source_id=source, status=status, category=category)


@app.post("/api/v1/variables")
def create_variable(request: VariableUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:
    connectors = current_connectors()
    if request.source_id not in connectors:
        raise HTTPException(status_code=422, detail="Unknown source connector.")
    variable_id = make_id(request.name, current_variable_map())
    created = storage.create_variable(
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


@app.put("/api/v1/variables/{variable_id}")
def update_variable(
    variable_id: str,
    request: VariableUpsertRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    existing = ensure_exists(storage.get_variable(variable_id), "variable", variable_id)
    updated = storage.update_variable(
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


@app.delete("/api/v1/variables/{variable_id}")
def delete_variable(variable_id: str) -> Dict[str, Any]:
    variable = ensure_exists(storage.get_variable(variable_id), "variable", variable_id)
    if variable["status"] != "dev":
        raise HTTPException(status_code=409, detail="Only DEV variables can be deleted.")
    return storage.delete_variable(variable_id) or variable


@app.post("/api/v1/variables/{variable_id}/test")
def test_variable(variable_id: str, request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    variable = ensure_exists(storage.get_variable(variable_id), "variable", variable_id)
    result = test_variable_entity(variable, request.payload)
    if result["result"].get("error"):
        record_error("variables", "test", result["result"]["error"], "variable", variable_id, {"payload": request.payload})
    return result


@app.post("/api/v1/variables/test-draft")
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


@app.post("/api/v1/variables/{variable_id}/promote")
def promote_variable(
    variable_id: str,
    request: PromoteRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    promoted = promote_entity("variable", variable_id, request.promoted_by, request.reason)
    if promoted["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return promoted


@app.get("/api/v1/variables/{variable_id}/history")
def variable_history(variable_id: str) -> List[Dict[str, Any]]:
    return storage.get_history("variable", variable_id)


@app.get("/api/v1/variables/graph")
def variable_graph() -> Dict[str, Any]:
    variables = storage.list_variables()
    rules = storage.list_rules()
    scorecards = storage.list_scorecards()
    policies = storage.list_policies()
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


@app.get("/api/v1/variables/{variable_id}")
def get_variable(variable_id: str) -> Dict[str, Any]:
    return ensure_exists(storage.get_variable(variable_id), "variable", variable_id)


@app.get("/api/v1/rules")
def list_rules(status: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    return storage.list_rules(status=status)


@app.get("/api/v1/rules/{rule_id}")
def get_rule(rule_id: str) -> Dict[str, Any]:
    return ensure_exists(storage.get_rule(rule_id), "rule", rule_id)


@app.post("/api/v1/rules")
def create_rule(request: RuleUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:
    normalized = normalize_rule_payload(request)
    rule_id = make_id(request.name, current_rule_map())
    created = storage.create_rule(
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


@app.put("/api/v1/rules/{rule_id}")
def update_rule(
    rule_id: str,
    request: RuleUpsertRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    normalized = normalize_rule_payload(request)
    existing = ensure_exists(storage.get_rule(rule_id), "rule", rule_id)
    updated = storage.update_rule(
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


@app.delete("/api/v1/rules/{rule_id}")
def delete_rule(rule_id: str) -> Dict[str, Any]:
    rule = ensure_exists(storage.get_rule(rule_id), "rule", rule_id)
    if rule["status"] != "dev":
        raise HTTPException(status_code=409, detail="Only DEV rules can be deleted.")
    return storage.delete_rule(rule_id) or rule


@app.post("/api/v1/rules/{rule_id}/test")
@app.post("/api/v1/test/rule/{rule_id}")
def test_rule(rule_id: str, request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    rule = ensure_exists(storage.get_rule(rule_id), "rule", rule_id)
    result = test_rule_entity(rule, request.payload)
    if not result["result"].get("passed"):
        record_error("rules", "test", "Rule test did not pass.", "rule", rule_id, {"outcome": result["result"]})
    return result


@app.post("/api/v1/rules/{rule_id}/promote")
def promote_rule(
    rule_id: str,
    request: PromoteRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    promoted = promote_entity("rule", rule_id, request.promoted_by, request.reason)
    if promoted["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return promoted


@app.get("/api/v1/scorecards")
def list_scorecards(status: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    return storage.list_scorecards(status=status)


@app.get("/api/v1/scorecards/{scorecard_id}")
def get_scorecard(scorecard_id: str) -> Dict[str, Any]:
    return ensure_exists(storage.get_scorecard(scorecard_id), "scorecard", scorecard_id)


@app.post("/api/v1/scorecards")
def create_scorecard(request: ScorecardUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:
    bins = [item.model_dump() for item in request.bins]
    validate_scorecard_bins(bins)
    scorecard_id = make_id(request.name, current_scorecard_map())
    created = storage.create_scorecard(
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


@app.put("/api/v1/scorecards/{scorecard_id}")
def update_scorecard(
    scorecard_id: str,
    request: ScorecardUpsertRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    bins = [item.model_dump() for item in request.bins]
    validate_scorecard_bins(bins)
    existing = ensure_exists(storage.get_scorecard(scorecard_id), "scorecard", scorecard_id)
    updated = storage.update_scorecard(
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


@app.delete("/api/v1/scorecards/{scorecard_id}")
def delete_scorecard(scorecard_id: str) -> Dict[str, Any]:
    scorecard = ensure_exists(storage.get_scorecard(scorecard_id), "scorecard", scorecard_id)
    if scorecard["status"] != "dev":
        raise HTTPException(status_code=409, detail="Only DEV scorecards can be deleted.")
    return storage.delete_scorecard(scorecard_id) or scorecard


@app.post("/api/v1/scorecards/{scorecard_id}/test")
def test_scorecard(scorecard_id: str, request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    scorecard = ensure_exists(storage.get_scorecard(scorecard_id), "scorecard", scorecard_id)
    return test_scorecard_entity(scorecard, request.payload)


@app.post("/api/v1/scorecards/{scorecard_id}/promote")
def promote_scorecard(
    scorecard_id: str,
    request: PromoteRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    promoted = promote_entity("scorecard", scorecard_id, request.promoted_by, request.reason)
    if promoted["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return promoted


@app.get("/api/v1/policies")
def list_policies(status: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    return storage.list_policies(status=status)


@app.get("/api/v1/policies/{policy_id}")
def get_policy(policy_id: str) -> Dict[str, Any]:
    return ensure_exists(storage.get_policy(policy_id), "policy", policy_id)


@app.post("/api/v1/policies")
def create_policy(request: PolicyUpsertRequest, background_tasks: BackgroundTasks = None) -> Dict[str, Any]:
    steps = [item.model_dump() for item in request.steps]
    validate_policy_steps(steps)
    policy_id = make_id(request.name, current_policy_map())
    created = storage.create_policy(
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


@app.put("/api/v1/policies/{policy_id}")
def update_policy(
    policy_id: str,
    request: PolicyUpsertRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    steps = [item.model_dump() for item in request.steps]
    validate_policy_steps(steps)
    existing = ensure_exists(storage.get_policy(policy_id), "policy", policy_id)
    updated = storage.update_policy(
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


@app.delete("/api/v1/policies/{policy_id}")
def delete_policy(policy_id: str) -> Dict[str, Any]:
    policy = ensure_exists(storage.get_policy(policy_id), "policy", policy_id)
    if policy["status"] != "dev":
        raise HTTPException(status_code=409, detail="Only DEV policies can be deleted.")
    return storage.delete_policy(policy_id) or policy


@app.post("/api/v1/policies/{policy_id}/execute")
@app.post("/api/v1/test/policy/{policy_id}")
def execute_policy_endpoint(policy_id: str, request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    policy = ensure_exists(storage.get_policy(policy_id), "policy", policy_id)
    result = test_policy_entity(policy, request.payload)
    if result["result"].get("outcome") == "reject":
        record_error("policies", "execute", "Policy execution rejected on sample payload.", "policy", policy_id, {"trace": result["result"].get("trace", [])})
    return result


@app.post("/api/v1/policies/{policy_id}/promote")
def promote_policy(
    policy_id: str,
    request: PromoteRequest,
    background_tasks: BackgroundTasks = None,
) -> Dict[str, Any]:
    promoted = promote_entity("policy", policy_id, request.promoted_by, request.reason)
    if promoted["status"] == "prod":
        maybe_compile_bundle(active_tenant_id(), background_tasks=background_tasks)
    return promoted


@app.post("/api/v1/test/variables")
def batch_test_variables(request: TestPayloadRequest = Body(default=TestPayloadRequest())) -> Dict[str, Any]:
    payloads = payload_map(request.payload)
    results = []
    pass_count = 0
    for variable in storage.list_variables():
        outcome = test_variable_entity(variable, payloads)
        result = {
            "id": outcome["variable"]["id"],
            "name": outcome["variable"]["name"],
            "status": outcome["variable"]["status"],
            "category": outcome["variable"]["category"],
            "source_id": outcome["variable"]["source_id"],
            "source_icon": current_connectors().get(outcome["variable"]["source_id"], {}).get("icon"),
            "computed_value": outcome["result"]["value"],
            "latency_ms": outcome["result"]["latency_ms"],
            "passed": outcome["result"]["passed"],
            "badge": "PASS" if outcome["result"]["passed"] else "SIM",
            "error": outcome["result"]["error"],
        }
        pass_count += 1 if result["passed"] else 0
        results.append(result)
    return {"results": results, "summary": "{0}/{1} passed".format(pass_count, len(results))}


@app.post("/api/v1/test/batch")
def batch_simulation(request: BatchSimulationRequest) -> Dict[str, Any]:
    rows = []
    for index, payload in enumerate(request.payloads):
        if request.targetType == "variables":
            result = batch_test_variables(TestPayloadRequest(payload=payload))
        elif request.targetType == "rule":
            if not request.targetId:
                raise HTTPException(status_code=422, detail="targetId is required for rule batch simulation.")
            result = test_rule(request.targetId, TestPayloadRequest(payload=payload))
        elif request.targetType == "policy":
            if not request.targetId:
                raise HTTPException(status_code=422, detail="targetId is required for policy batch simulation.")
            result = execute_policy_endpoint(request.targetId, TestPayloadRequest(payload=payload))
        elif request.targetType == "decide":
            if not request.targetId:
                raise HTTPException(status_code=422, detail="targetId is required for decision batch simulation.")
            result = decide(DecideRequest(policy_id=request.targetId, payload=payload))
        else:
            raise HTTPException(status_code=422, detail="Unsupported batch targetType.")
        rows.append({"index": index, "payload": payload, "result": result})
    return {"targetType": request.targetType, "targetId": request.targetId, "rows": rows, "count": len(rows)}


@app.post("/api/v1/decide")
def decide(request: DecideRequest) -> Dict[str, Any]:
    policy = ensure_exists(storage.get_policy(request.policy_id), "policy", request.policy_id)
    outcome = test_policy_entity(policy, request.payload, log_decision=True)
    if outcome["result"]["outcome"] == "reject":
        record_error("decisions", "decide", "Decision outcome rejected.", "policy", request.policy_id, {"trace": outcome["result"].get("trace", [])})
    return {
        "policy_id": policy["id"],
        "outcome": outcome["result"]["outcome"],
        "score": outcome["result"].get("scorecard_result", {}).get("score") if outcome["result"].get("scorecard_result") else None,
        "variables": outcome["values"],
        "rule_results": [item for item in outcome["result"]["trace"] if item.get("step", {}).get("type") == "rule"],
        "scorecard_result": outcome["result"].get("scorecard_result"),
        "trace": outcome["result"]["trace"],
        "latency_ms": outcome["latency_ms"],
    }


@app.post("/api/v1/decide/batch")
def batch_decide(request: BatchSimulationRequest) -> Dict[str, Any]:
    if not request.targetId:
        raise HTTPException(status_code=422, detail="targetId is required for decision batches.")
    rows = []
    for index, payload in enumerate(request.payloads):
        rows.append({"index": index, "result": decide(DecideRequest(policy_id=request.targetId, payload=payload))})
    return {"targetType": "decide", "targetId": request.targetId, "rows": rows, "count": len(rows)}


@app.get("/api/v1/deploy/status")
def deploy_status() -> Dict[str, Any]:
    return {
        "dev": {
            "variables": storage.list_variables(status="dev"),
            "rules": storage.list_rules(status="dev"),
            "scorecards": storage.list_scorecards(status="dev"),
            "policies": storage.list_policies(status="dev"),
        },
        "uat": {
            "variables": storage.list_variables(status="uat"),
            "rules": storage.list_rules(status="uat"),
            "scorecards": storage.list_scorecards(status="uat"),
            "policies": storage.list_policies(status="uat"),
        },
        "prod": {
            "variables": storage.list_variables(status="prod"),
            "rules": storage.list_rules(status="prod"),
            "scorecards": storage.list_scorecards(status="prod"),
            "policies": storage.list_policies(status="prod"),
        },
    }


@app.post("/api/v1/deploy/promote")
def deploy_promote(request: DeployPromoteRequest) -> Dict[str, Any]:
    promoted = []
    for item in request.items:
        promoted.append(promote_entity(item.entity_type, item.entity_id, item.promoted_by, item.reason))
    return {"promoted": promoted, "history": storage.list_promotions()[: len(promoted)]}


@app.get("/api/v1/export")
def export_config(format: str = Query(default="json")) -> PlainTextResponse:
    config = storage.export_config()
    content = export_bundle(config, format)
    extension = {"python": "py", "csv": "csv"}.get(format, format)
    media_type = {
        "json": "application/json",
        "yaml": "application/x-yaml",
        "python": "text/x-python",
        "csv": "text/csv",
    }.get(format, "text/plain")
    return PlainTextResponse(
        content,
        media_type=media_type,
        headers={"Content-Disposition": 'attachment; filename="rulemind-export.{0}"'.format(extension)},
    )


def _validate_import_entity(entity_type: str, entity: Dict[str, Any], existing_connectors: List[str], existing_variables: List[str]) -> Dict[str, Any]:
    """Validate a single entity and return a validation report."""
    issues: List[str] = []
    entity_id = entity.get("id", entity.get("name", "unknown"))

    if entity_type == "variable":
        if not entity.get("code"):
            issues.append("Missing 'code' field")
        else:
            import ast as _ast
            try:
                _ast.parse(entity["code"])
            except SyntaxError as exc:
                issues.append(f"Python syntax error: {exc}")
        if entity.get("source_id") and entity["source_id"] not in existing_connectors:
            issues.append(f"Referenced connector '{entity['source_id']}' not found")

    elif entity_type == "rule":
        nodes = entity.get("nodes", [])
        tree = entity.get("tree")
        if not nodes and not tree:
            issues.append("Rule has no nodes and no tree")
        for node in nodes:
            node_type = node.get("type", "")
            if node_type not in ALLOWED_NODE_TYPES:
                issues.append(f"Invalid node type: '{node_type}'")
            if node_type == "condition":
                operator = node.get("operator", "")
                if operator and operator not in ALLOWED_OPERATORS:
                    issues.append(f"Unsupported operator '{operator}' on node '{node.get('id', '?')}'")
                if node.get("variable") and node["variable"] not in existing_variables:
                    issues.append(f"Referenced variable '{node['variable']}' not found")
        has_outcome = any(n.get("type") in {"approve", "review", "reject"} for n in nodes)
        if nodes and not has_outcome and not tree:
            issues.append("Rule has no outcome node (approve/review/reject)")
        if tree:
            _validate_tree_depth(tree, issues, depth=0, max_depth=10)

    elif entity_type == "scorecard":
        if not entity.get("bins"):
            issues.append("Scorecard has no bins")
        for b in entity.get("bins", []):
            if b.get("variable_id") and b["variable_id"] not in existing_variables:
                issues.append(f"Referenced variable '{b['variable_id']}' not found in scorecard bin")
            for r in b.get("ranges", []):
                try:
                    if float(r.get("min", 0)) > float(r.get("max", 0)):
                        issues.append(f"Invalid range: min ({r.get('min')}) > max ({r.get('max')}) in bin '{b.get('variable_id')}'")
                except (TypeError, ValueError):
                    issues.append(f"Non-numeric range values in bin '{b.get('variable_id')}'")

    elif entity_type == "policy":
        if not entity.get("steps"):
            issues.append("Policy has no steps")
        valid_step_types = {"connector", "rule", "scorecard", "outcome", "action", "review_gate", "transform", "model"}
        for step in entity.get("steps", []):
            st = step.get("type", "")
            if st not in valid_step_types:
                issues.append(f"Invalid step type: '{st}'")

    return {
        "id": entity_id,
        "valid": len(issues) == 0,
        "issues": issues,
    }


def _validate_tree_depth(tree: Dict[str, Any], issues: List[str], depth: int, max_depth: int) -> None:
    if depth > max_depth:
        issues.append(f"Tree depth exceeds maximum ({max_depth})")
        return
    for child in tree.get("children", []):
        _validate_tree_depth(child, issues, depth + 1, max_depth)
    child_node = tree.get("child")
    if isinstance(child_node, dict):
        _validate_tree_depth(child_node, issues, depth + 1, max_depth)


@app.post("/api/v1/import/validate")
def validate_import(request: ImportRequest) -> Dict[str, Any]:
    """Validate an import payload and return a detailed report of which entities are valid/invalid."""
    data = request.model_dump()
    connector_ids = {c.get("id") for c in data.get("connectors", []) if c.get("id")}
    connector_ids.update(c["id"] for c in storage.list_connectors())
    variable_ids = {v.get("id") for v in data.get("variables", []) if v.get("id")}
    variable_ids.update(v["id"] for v in storage.list_variables())

    report: Dict[str, Any] = {}
    total, valid_count, invalid_count = 0, 0, 0

    for entity_type in ("connectors", "variables", "rules", "scorecards", "policies"):
        singular = entity_type.rstrip("s") if entity_type != "policies" else "policy"
        entity_reports = []
        for entity in data.get(entity_type, []):
            r = _validate_import_entity(singular, entity, list(connector_ids), list(variable_ids))
            entity_reports.append(r)
            total += 1
            if r["valid"]:
                valid_count += 1
            else:
                invalid_count += 1
        report[entity_type] = entity_reports

    report["summary"] = {"total": total, "valid": valid_count, "invalid": invalid_count}
    return report


@app.post("/api/v1/import")
def import_config(request: ImportRequest) -> Dict[str, Any]:
    """Import configuration with validation report. All entities are imported; invalid ones are flagged."""
    validation = validate_import(request)
    try:
        storage.replace_all(request.model_dump())
        return {
            "imported": True,
            "counts": {key: len(value) if isinstance(value, list) else 1 for key, value in request.model_dump().items()},
            "validation": validation,
        }
    except Exception as error:
        record_error("imports", "replace_all", str(error), None, None, {"keys": sorted(request.model_dump().keys())})
        raise


@app.get("/api/v1/audit/decisions")
def audit_decisions() -> List[Dict[str, Any]]:
    return storage.list_decisions()


@app.get("/api/v1/audit/promotions")
def audit_promotions() -> List[Dict[str, Any]]:
    return storage.list_promotions()


@app.get("/api/v1/audit/errors")
def audit_errors() -> List[Dict[str, Any]]:
    return storage.list_error_events()


@app.get("/api/v1/settings")
def get_settings() -> Dict[str, Any]:
    return storage.get_settings()


@app.put("/api/v1/settings")
def update_settings(request: SettingsRequest) -> Dict[str, Any]:
    return storage.update_settings(request.model_dump())


@app.get("/api/v1/bootstrap")
def bootstrap() -> Dict[str, Any]:
    return {
        "connectors": storage.list_connectors(),
        "variables": storage.list_variables(),
        "rules": storage.list_rules(),
        "scorecards": storage.list_scorecards(),
        "policies": storage.list_policies(),
        "settings": storage.get_settings(),
        "promotions": storage.list_promotions(),
    }


@app.get("/api/v1/experiments")
def list_experiments() -> List[Dict[str, Any]]:
    return storage.list_experiments()


@app.get("/api/v1/experiments/{experiment_id}")
def get_experiment(experiment_id: str) -> Dict[str, Any]:
    experiment = storage.get_experiment(experiment_id)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    return experiment


@app.post("/api/v1/experiments")
def create_experiment(request: ExperimentUpsertRequest) -> Dict[str, Any]:
    experiment_id = request.id or make_id(request.name, {item["id"]: item for item in storage.list_experiments()})
    return storage.create_or_update_experiment({**request.model_dump(), "id": experiment_id})


@app.put("/api/v1/experiments/{experiment_id}")
def update_experiment(experiment_id: str, request: ExperimentUpsertRequest) -> Dict[str, Any]:
    existing = storage.get_experiment(experiment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    return storage.create_or_update_experiment({**existing, **request.model_dump(exclude_none=True), "id": experiment_id})


@app.patch("/api/v1/experiments/{experiment_id}/status")
def update_experiment_status(experiment_id: str, request: ExperimentStatusRequest) -> Dict[str, Any]:
    existing = storage.get_experiment(experiment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    return storage.create_or_update_experiment({**existing, "status": request.status, "id": experiment_id})


@app.delete("/api/v1/experiments/{experiment_id}")
def delete_experiment(experiment_id: str) -> Dict[str, bool]:
    existing = storage.get_experiment(experiment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    if existing["status"] != "draft":
        raise HTTPException(status_code=409, detail="Only draft experiments can be deleted.")
    storage.delete_experiment(experiment_id)
    return {"deleted": True}


@app.get("/api/v1/experiments/{experiment_id}/results")
@app.get("/api/v1/analytics/experiments/{experiment_id}")
def experiment_results(experiment_id: str) -> Dict[str, Any]:
    try:
        return experiment_analytics(storage, active_tenant_id(), experiment_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/v1/analytics/decisions")
def analytics_decisions() -> Dict[str, Any]:
    return decision_analytics(storage, active_tenant_id())


@app.get("/api/v1/analytics/latency")
def analytics_latency() -> Dict[str, Any]:
    return latency_analytics(storage, active_tenant_id())


@app.get("/api/v1/analytics/sdk")
def analytics_sdk() -> Dict[str, Any]:
    return sdk_analytics(storage, active_tenant_id())


@app.get("/sdk/v1/experience-manifest")
def sdk_experience_manifest(request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    latest = storage.latest_bundle(tenant_id=tenant_id)
    return build_experience_manifest(tenant_id, public_api_base_url(request), latest_bundle_version=int(latest["version"]) if latest else 0)


@app.get("/sdk/v1/health")
def sdk_health(request: Request) -> Dict[str, Any]:
    latest = storage.latest_bundle(tenant_id=active_tenant_id(request))
    return {"status": "ok", "latestBundleVersion": latest["version"] if latest else 0}


@app.get("/sdk/v1/bundle")
def sdk_bundle(request: Request) -> Response:
    tenant_id = active_tenant_id(request)
    latest = storage.latest_bundle(tenant_id=tenant_id)
    if latest is None:
        try:
            latest = compile_bundle(storage, tenant_id, client_public_key=request.headers.get("x-client-public-key"), force=True)
        except NoProductionAssetsError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except BundleCompilationError as error:
            storage.add_error_event(
                {
                    "tenant_id": tenant_id,
                    "scope": "bundle",
                    "entity_type": "bundle",
                    "entity_id": error.entity_id,
                    "stage": "compile",
                    "message": str(error),
                    "details": {"entity_type": error.entity_type, "entity_id": error.entity_id},
                },
                tenant_id=tenant_id,
            )
            raise HTTPException(status_code=500, detail=str(error)) from error
        except Exception as error:
            storage.add_error_event(
                {
                    "tenant_id": tenant_id,
                    "scope": "bundle",
                    "entity_type": "bundle",
                    "stage": "compile",
                    "message": str(error),
                    "details": {},
                },
                tenant_id=tenant_id,
            )
            raise
    current_version = int(request.headers.get("x-bundle-version", "0") or "0")
    if int(latest["version"]) == current_version:
        BUNDLE_SYNCS_TOTAL.labels(status="not_modified").inc()
        return Response(status_code=304)
    content = latest["content"]
    response_payload = render_bundle_response(content, request.headers.get("x-client-public-key"))
    BUNDLE_SYNCS_TOTAL.labels(status="success").inc()
    return Response(content=json_dumps(response_payload), media_type="application/json")


@app.post("/sdk/v1/decide")
def sdk_decide(request: SdkDecideRequest) -> Dict[str, Any]:
    policy = ensure_exists(storage.get_policy(request.policyId), "policy", request.policyId)
    started = time.perf_counter()
    ctx = asyncio.run(
        workflow_executor().execute(
            policy=policy,
            payload=request.payload,
            tenant_id=active_tenant_id(),
            user_id=request.userId,
            source="sdk_server",
            sdk_version=request.sdkVersion,
        )
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    ctx.total_latency_ms = latency_ms
    DECISIONS_TOTAL.labels(outcome=ctx.outcome, source="sdk_server").inc()
    DECISION_LATENCY.labels(source="sdk_server").observe(max(latency_ms, 1) / 1000)
    return build_sdk_response(ctx, request_id=request.requestId, source="sdk_server", latency_ms=latency_ms)


@app.post("/sdk/v1/executions/sync")
def sdk_sync_execution(request: SdkExecutionSyncRequest) -> Dict[str, Any]:
    tenant_id = active_tenant_id()
    existing = storage.get_workflow_execution(request.executionId, tenant_id=tenant_id)
    synced_review_task = sync_sdk_review_task(request, tenant_id)
    ctx = ExecutionContext(
        payload=copy.deepcopy(request.payload),
        tenant_id=tenant_id,
        policy_id=request.policyId,
        execution_id=request.executionId,
        user_id=request.userId,
        variables=copy.deepcopy(request.variables),
        rule_results=[normalize_sdk_rule_result(item) for item in request.ruleResults],
        scorecard_results=copy.deepcopy(request.scorecardResults),
        action_results=copy.deepcopy(request.actionResults),
        pending_operations=copy.deepcopy(request.pendingOperations),
        outcome=request.outcome,
        status=request.status,
        review_task_id=(synced_review_task or request.reviewTask or {}).get("id"),
        experiment_id=request.experimentId,
        experiment_variant=request.experimentVariant,
        step_trace=copy.deepcopy(request.trace),
        started_at=parse_iso_datetime(request.startedAt) or datetime.utcnow(),
        completed_at=parse_iso_datetime(request.completedAt),
        total_latency_ms=request.latencyMs,
    )
    payload = {
        "id": request.executionId,
        "tenant_id": tenant_id,
        "policy_id": request.policyId,
        "status": request.status,
        "context": ctx.to_dict(),
        "current_step_index": max(len(request.trace) - 1, 0),
        "trigger_type": request.source,
        "trigger_metadata": {"request_id": request.requestId},
        "started_at": ctx.started_at,
        "paused_at": datetime.utcnow() if request.status == "paused" else None,
        "completed_at": ctx.completed_at,
    }
    if existing:
        storage.update_workflow_execution(request.executionId, payload, tenant_id=tenant_id)
    else:
        storage.create_workflow_execution(payload, tenant_id=tenant_id)
    persist_sdk_action_logs(request.executionId, request.actionResults, tenant_id)
    maybe_create_sdk_decision(request, tenant_id, existing)
    storage.add_audit_event(
        {
            "tenant_id": tenant_id,
            "event_type": "sdk_execution_synced",
            "entity_type": "workflow_execution",
            "entity_id": request.executionId,
            "detail": "SDK execution synchronized from device.",
            "metadata": {"status": request.status, "policy_id": request.policyId, "source": request.source},
        },
        tenant_id=tenant_id,
    )
    return build_sdk_response(ctx, request_id=request.requestId, source=request.source, latency_ms=request.latencyMs)


@app.get("/sdk/v1/executions/{execution_id}")
def sdk_get_execution(execution_id: str) -> Dict[str, Any]:
    execution = storage.get_workflow_execution(execution_id, tenant_id=active_tenant_id())
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found.")
    ctx = ExecutionContext.from_dict(execution["context"])
    return build_sdk_response(
        ctx,
        request_id=execution.get("trigger_metadata", {}).get("request_id"),
        source=execution.get("trigger_type") or "sdk_edge",
        latency_ms=ctx.total_latency_ms,
    )


@app.post("/sdk/v1/executions/{execution_id}/resume")
def sdk_resume_execution(execution_id: str, request: SdkResumeExecutionRequest) -> Dict[str, Any]:
    execution = storage.get_workflow_execution(execution_id, tenant_id=active_tenant_id())
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found.")
    ctx = ExecutionContext.from_dict(execution["context"])
    if request.decision and ctx.review_task_id:
        result = submit_review_decision(
            storage,
            ctx.review_task_id,
            request.response,
            request.reviewerId or "sdk",
            request.decision,
        )
        stored = storage.get_workflow_execution(execution_id, tenant_id=ctx.tenant_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Execution not found after resume.")
        resumed_ctx = ExecutionContext.from_dict(stored["context"])
        return build_sdk_response(resumed_ctx, source="sdk_edge_resume", latency_ms=resumed_ctx.total_latency_ms)
    policy = storage.get_policy(ctx.policy_id, tenant_id=ctx.tenant_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found.")
    resumed = asyncio.run(
        workflow_executor().execute(policy=policy, payload=ctx.payload, tenant_id=ctx.tenant_id, resume_from=ctx, source="sdk_edge_resume")
    )
    return build_sdk_response(resumed, source="sdk_edge_resume", latency_ms=resumed.total_latency_ms)


@app.post("/sdk/v1/events")
def sdk_events(request: SdkEventsRequest) -> Dict[str, int]:
    count = storage.add_sdk_events(request.events)
    EVENTS_INGESTED_TOTAL.inc(count)
    return {"received": count, "processed": count}


@app.get("/api/v1/webhooks")
def list_webhooks() -> List[Dict[str, Any]]:
    return storage.list_webhooks()


@app.get("/api/v1/webhooks/{webhook_id}")
def get_webhook(webhook_id: str) -> Dict[str, Any]:
    webhook = storage.get_webhook(webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return webhook


@app.post("/api/v1/webhooks")
def create_webhook(request: WebhookUpsertRequest) -> Dict[str, Any]:
    endpoint_id = "wh_" + uuid.uuid4().hex[:12]
    return storage.create_webhook(
        {
            "id": endpoint_id,
            "policy_id": request.policy_id,
            "endpoint_path": "/api/v1/webhooks/{0}".format(endpoint_id),
            "is_active": request.is_active,
            "secret_hash": request.secret,
            "payload_mapping": request.payload_mapping,
        }
    )


@app.put("/api/v1/webhooks/{webhook_id}")
def update_webhook(webhook_id: str, request: WebhookUpsertRequest) -> Dict[str, Any]:
    updated = storage.update_webhook(
        webhook_id,
        {
            "policy_id": request.policy_id,
            "is_active": request.is_active,
            "secret_hash": request.secret,
            "payload_mapping": request.payload_mapping,
        },
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return updated


@app.delete("/api/v1/webhooks/{webhook_id}")
def delete_webhook(webhook_id: str) -> Dict[str, bool]:
    updated = storage.update_webhook(webhook_id, {"is_active": False})
    if not updated:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return {"deactivated": True}


@app.get("/api/v1/webhooks/{webhook_id}/test")
def test_webhook(webhook_id: str) -> Dict[str, str]:
    webhook = storage.get_webhook(webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return {
        "url": webhook["endpoint_path"],
        "curl": "curl -X POST http://localhost:8080{0} -H 'Content-Type: application/json' -d '{{}}'".format(webhook["endpoint_path"]),
    }


@app.post("/api/v1/webhooks/{webhook_id}")
async def webhook_trigger(webhook_id: str, request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from error
    try:
        return await trigger_webhook(storage, webhook_id, body, signature=request.headers.get("x-webhook-signature"))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WebhookAuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except PermissionError as error:
        storage.add_error_event(
            {
                "tenant_id": active_tenant_id(),
                "scope": "webhook",
                "entity_type": "workflow_execution",
                "entity_id": webhook_id,
                "stage": "execute",
                "message": str(error),
                "details": {"trigger": "webhook"},
            },
            tenant_id=active_tenant_id(),
        )
        raise HTTPException(status_code=500, detail="Webhook execution failed.") from error


@app.get("/api/v1/schedules")
def list_schedules() -> List[Dict[str, Any]]:
    return storage.list_schedules()


@app.get("/api/v1/schedules/{schedule_id}")
def get_schedule(schedule_id: str) -> Dict[str, Any]:
    schedule = storage.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return schedule


@app.post("/api/v1/schedules")
def create_schedule(request: ScheduleUpsertRequest) -> Dict[str, Any]:
    return storage.create_schedule(request.model_dump())


@app.put("/api/v1/schedules/{schedule_id}")
def update_schedule(schedule_id: str, request: ScheduleUpsertRequest) -> Dict[str, Any]:
    updated = storage.update_schedule(schedule_id, request.model_dump())
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return updated


@app.delete("/api/v1/schedules/{schedule_id}")
def delete_schedule(schedule_id: str) -> Dict[str, bool]:
    updated = storage.update_schedule(schedule_id, {"is_active": False})
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return {"deactivated": True}


@app.post("/api/v1/schedules/{schedule_id}/run-now")
def run_schedule_now(schedule_id: str) -> Dict[str, Any]:
    schedule = storage.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    result = asyncio.run(execute_cron_policy(storage, schedule))
    return result


@app.get("/api/v1/schedules/{schedule_id}/history")
def schedule_history(schedule_id: str) -> List[Dict[str, Any]]:
    return [
        event
        for event in storage.list_audit_events(event_type="cron_executed")
        if event.get("metadata", {}).get("schedule_id") == schedule_id
    ]


@app.get("/api/v1/reviews")
def list_reviews(queue: Optional[str] = Query(default=None), status: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    return storage.list_review_tasks(queue=queue, status=status)


@app.get("/api/v1/reviews/stats")
def review_stats() -> Dict[str, Any]:
    tasks = storage.list_review_tasks()
    pending = [task for task in tasks if task["status"] == "pending"]
    reviewed = [task for task in tasks if task["status"] in {"approved", "rejected", "timed_out"}]
    return {"pending": len(pending), "reviewed": len(reviewed), "queues": {queue: len([task for task in tasks if task["queue"] == queue]) for queue in {task["queue"] for task in tasks}}}


@app.get("/api/v1/reviews/{task_id}")
def get_review(task_id: str) -> Dict[str, Any]:
    task = storage.get_review_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found.")
    return task


@app.post("/api/v1/reviews/{task_id}/decide")
def decide_review(task_id: str, request: ReviewDecisionRequest) -> Dict[str, Any]:
    try:
        result = submit_review_decision(storage, task_id, request.response, request.reviewer_id, request.decision)
    except ValueError as error:
        detail = str(error)
        status_code = 422 if detail.startswith("Missing required field") else 404
        raise HTTPException(status_code=status_code, detail=detail) from error
    return {"executionId": result["execution"]["execution_id"], "outcome": result["execution"]["outcome"], "status": result["execution"]["status"]}


@app.post("/api/v1/reviews/{task_id}/escalate")
def escalate_review(task_id: str, request: ReviewDecisionRequest) -> Dict[str, Any]:
    task = storage.get_review_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found.")
    updated = storage.update_review_task(task_id, {"status": "escalated", "reviewer_response": request.response, "reviewed_by": request.reviewer_id})
    return updated or task


@app.post("/api/v1/executions/{execution_id}/resume")
def resume_execution(execution_id: str) -> Dict[str, Any]:
    execution = storage.get_workflow_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found.")
    ctx = ExecutionContext.from_dict(execution["context"])
    policy = storage.get_policy(ctx.policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found.")
    result = asyncio.run(workflow_executor().execute(policy=policy, payload=ctx.payload, tenant_id=ctx.tenant_id, resume_from=ctx, source="api"))
    return {"executionId": result.execution_id, "outcome": result.outcome, "status": result.status}


@app.get("/api/v1/executions/{execution_id}")
def get_execution_detail(execution_id: str) -> Dict[str, Any]:
    execution = storage.get_workflow_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found.")
    ctx = ExecutionContext.from_dict(execution["context"])
    detail = build_sdk_response(
        ctx,
        request_id=execution.get("trigger_metadata", {}).get("request_id"),
        source=execution.get("trigger_type") or "api",
        latency_ms=ctx.total_latency_ms,
    )
    detail["actionLogs"] = storage.list_action_logs(execution_id=execution_id)
    detail["auditEvents"] = [event for event in storage.list_audit_events() if event.get("entity_id") == execution_id]
    detail["entitySchemas"] = ADMIN_ENTITY_SCHEMAS
    return detail


# ═══════════════════════════════════════════════════════════════════════════
# MODEL HOSTING ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

class ModelCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    model_type: str = "sklearn"
    model_base64: str
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None
    status: str = "dev"


class ModelPredictRequest(BaseModel):
    input_data: Dict[str, Any]
    features: Optional[List[str]] = None


@app.get("/api/v1/models")
def list_models() -> List[Dict[str, Any]]:
    return storage.list_models()


@app.get("/api/v1/models/{model_id}")
def get_model(model_id: str) -> Dict[str, Any]:
    model = storage.get_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    return model


@app.post("/api/v1/models")
def create_model(request: ModelCreateRequest) -> Dict[str, Any]:
    from .model_executor import base64_to_model, validate_model

    model_blob = base64_to_model(request.model_base64)
    validation = validate_model(model_blob)
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail=f"Invalid model: {validation['error']}")

    model_id = slugify(request.name)
    existing_ids = {m["id"] for m in storage.list_models()}
    if model_id in existing_ids:
        model_id = f"{model_id}_{uuid.uuid4().hex[:6]}"

    model_data = {
        "id": model_id,
        "name": request.name,
        "description": request.description,
        "model_type": validation.get("model_type", request.model_type),
        "model_blob": model_blob,
        "input_schema": request.input_schema,
        "output_schema": request.output_schema,
        "metrics": request.metrics,
        "status": request.status,
        "version": 1,
        "has_predict": validation["has_predict"],
        "has_predict_proba": validation["has_predict_proba"],
    }
    return storage.create_model(model_data)


@app.post("/api/v1/models/{model_id}/predict")
def predict_model(model_id: str, request: ModelPredictRequest) -> Dict[str, Any]:
    from .model_executor import execute_model

    model = storage.get_model(model_id, include_blob=True)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    model_blob = model.get("model_blob")
    if not model_blob:
        raise HTTPException(status_code=422, detail="Model has no binary data.")

    result = execute_model(model_blob, request.input_data, features=request.features)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.post("/api/v1/models/{model_id}/test")
def test_model(model_id: str, request: ModelPredictRequest) -> Dict[str, Any]:
    from .model_executor import execute_model

    model = storage.get_model(model_id, include_blob=True)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    model_blob = model.get("model_blob")
    if not model_blob:
        raise HTTPException(status_code=422, detail="Model has no binary data.")

    result = execute_model(model_blob, request.input_data, features=request.features)
    storage.update_model(model_id, {"last_test_result": {
        "prediction": result.get("prediction"),
        "latency_ms": result.get("latency_ms"),
        "tested_at": now_iso(),
        "error": result.get("error"),
    }})
    return {"model": storage.get_model(model_id), "result": result}


@app.delete("/api/v1/models/{model_id}")
def delete_model(model_id: str) -> Dict[str, Any]:
    model = storage.get_model(model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found.")
    storage.delete_model(model_id)
    return {"deleted": True, "id": model_id}


# ═══════════════════════════════════════════════════════════════════════════
# EXCEL FUNCTIONS INFO ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/v1/excel-functions")
def list_excel_functions() -> Dict[str, Any]:
    """List all available Excel functions in the sandbox."""
    from .excel_functions import EXCEL_FUNCTIONS
    categories: Dict[str, List[str]] = {
        "math_trig": [], "statistical": [], "financial": [], "logical": [],
        "text": [], "lookup_reference": [], "date_time": [], "information": [],
        "engineering": [], "database": [],
    }
    for name, func in sorted(EXCEL_FUNCTIONS.items()):
        doc = (func.__doc__ or "").strip()
        module = getattr(func, "__module__", "")
        # Categorize by function name patterns
        if name in {"SUM", "SUMIF", "SUMIFS", "SUMPRODUCT", "SUMSQ", "ABS", "ACOS", "ACOSH", "ASIN", "ASINH", "ATAN", "ATAN2", "ATANH", "CEILING", "COMBIN", "COMBINA", "COS", "COSH", "COT", "CSC", "DEGREES", "EVEN", "EXP", "FACT", "FACTDOUBLE", "FLOOR", "FLOOR_MATH", "GCD", "INT", "LCM", "LN", "LOG", "LOG10", "MOD", "MROUND", "MULTINOMIAL", "ODD", "PI", "POWER", "PRODUCT", "QUOTIENT", "RADIANS", "RAND", "RANDBETWEEN", "ROUND", "ROUNDDOWN", "ROUNDUP", "SEC", "SIGN", "SIN", "SINH", "SQRT", "SQRTPI", "SUBTOTAL", "TAN", "TANH", "TRUNC"}:
            categories["math_trig"].append(name)
        elif name in {"AVERAGE", "AVERAGEA", "AVERAGEIF", "AVERAGEIFS", "COUNT", "COUNTA", "COUNTBLANK", "COUNTIF", "COUNTIFS", "LARGE", "MAX", "MAXA", "MEDIAN", "MIN", "MINA", "MODE", "PERCENTILE", "PERCENTRANK", "QUARTILE", "RANK", "SMALL", "STDEV", "STDEVA", "STDEVP", "STDEVPA", "VAR", "VARA", "VARP", "VARPA", "CORREL", "COVAR", "FORECAST", "INTERCEPT", "SLOPE", "RSQ"}:
            categories["statistical"].append(name)
        elif name in {"PMT", "IPMT", "PPMT", "FV", "PV", "NPV", "IRR", "MIRR", "NPER", "RATE", "XNPV", "XIRR", "SLN", "SYD", "DB", "DDB", "VDB", "EFFECT", "NOMINAL", "CUMIPMT", "CUMPRINC", "FVSCHEDULE", "ISPMT", "DISC", "INTRATE", "RECEIVED", "DOLLARDE", "DOLLARFR", "DURATION", "MDURATION"}:
            categories["financial"].append(name)
        elif name in {"AND", "OR", "NOT", "XOR", "IF", "IFS", "IFERROR", "IFNA", "SWITCH", "TRUE", "FALSE"}:
            categories["logical"].append(name)
        elif name in {"CHAR", "CLEAN", "CODE", "CONCATENATE", "CONCAT", "EXACT", "FIND", "FIXED", "LEFT", "LEN", "LOWER", "MID", "PROPER", "REPLACE", "REPT", "RIGHT", "SEARCH", "SUBSTITUTE", "T", "TEXT", "TEXTJOIN", "TRIM", "UPPER", "VALUE", "UNICODE", "UNICHAR", "NUMBERVALUE"}:
            categories["text"].append(name)
        elif name in {"VLOOKUP", "HLOOKUP", "INDEX", "MATCH", "XLOOKUP", "CHOOSE", "LOOKUP", "COLUMNS", "ROWS", "ROW", "TRANSPOSE", "SORT", "FILTER", "UNIQUE", "SEQUENCE"}:
            categories["lookup_reference"].append(name)
        elif name in {"DATE", "DATEVALUE", "DAY", "DAYS", "DAYS360", "EDATE", "EOMONTH", "HOUR", "ISOWEEKNUM", "MINUTE", "MONTH", "NETWORKDAYS", "NOW", "SECOND", "TIME", "TIMEVALUE", "TODAY", "WEEKDAY", "WEEKNUM", "WORKDAY", "YEAR", "YEARFRAC", "DATEDIF"}:
            categories["date_time"].append(name)
        elif name in {"ISBLANK", "ISERR", "ISERROR", "ISEVEN", "ISLOGICAL", "ISNA", "ISNONTEXT", "ISNUMBER", "ISODD", "ISTEXT", "N", "NA", "TYPE", "ERROR.TYPE"}:
            categories["information"].append(name)
        elif name.startswith(("BIN2", "DEC2", "HEX2", "OCT2")) or name == "CONVERT":
            categories["engineering"].append(name)
        elif name.startswith("D") and name in {"DAVERAGE", "DCOUNT", "DCOUNTA", "DGET", "DMAX", "DMIN", "DSUM"}:
            categories["database"].append(name)

    return {
        "total_functions": len(EXCEL_FUNCTIONS),
        "categories": categories,
        "all_functions": sorted(EXCEL_FUNCTIONS.keys()),
    }
