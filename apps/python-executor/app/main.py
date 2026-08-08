import copy
import asyncio
import os
import secrets
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from .auth import decode_admin_jwt
from .compiler import (
    BundleCompilationError,
    NoProductionAssetsError,
    compile_bundle,
)
from .context import get_current_api_key_id, get_current_role, get_current_tenant_id
from .experience_studio import build_experience_manifest
from .executor import ExecutionContext, PolicyExecutor
from .logic import (
    execute_policy,
    flatten_tree_to_nodes,
    find_by_id,
    generate_rule_expression_definition,
    now_iso,
    redact_payload,
    evaluate_rule_definition,
    evaluate_rule_nodes,
    evaluate_scorecard,
    nodes_to_tree,
    slugify,
)
from .middleware import TenantContextMiddleware
from .reviews import submit_review_decision
from .runtime import is_local_dev, redis_client
from .security_config import verify_production_secrets
from .sandbox import execute_variable
from .scheduler import init_scheduler
from .storage import Storage
from .webhooks import trigger_webhook  # re-exported: app/routers/operations.py webhook_trigger reads main.trigger_webhook live (patch target)


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


def _record_ai_usage(provider: str, model: str, usage: Dict[str, Any]) -> None:
    """Recorder hook for ai.complete() — accrues per-workspace token + cost counters
    for the current tenant. Best-effort; usage accounting never fails a call."""
    from . import ai

    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    cost = ai.estimate_cost(provider, model, input_tokens, output_tokens)
    storage.record_ai_usage(provider, model, input_tokens, output_tokens, cost, tenant_id=get_current_tenant_id())


def _install_ai_usage_recorder() -> None:
    from . import ai

    ai.set_usage_recorder(_record_ai_usage)


_install_ai_usage_recorder()


app = FastAPI(title="RuleMind V4 API", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TenantContextMiddleware, storage=current_storage)

# Optional OpenTelemetry tracing (no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set).
try:
    from .observability import setup_telemetry

    setup_telemetry(app, engine=getattr(storage, "engine", None))
except Exception:  # pragma: no cover - never let telemetry break startup
    pass


DECISIONS_TOTAL = Counter("rulemind_decisions_total", "RuleMind decisions", ["outcome", "source"])
DECISION_LATENCY = Histogram("rulemind_decision_latency_seconds", "RuleMind decision latency", ["source"])
BUNDLE_SYNCS_TOTAL = Counter("rulemind_bundle_syncs_total", "RuleMind bundle syncs", ["status"])
EVENTS_INGESTED_TOTAL = Counter("rulemind_events_ingested_total", "RuleMind SDK events ingested")
DECISIONS_INGESTED_TOTAL = Counter("rulemind_sdk_decisions_ingested_total", "On-device decisions ingested", ["result"])

# Max decisions accepted per /sdk/v1/decisions batch (config so limits move without code).
SDK_DECISIONS_BATCH_MAX = int(os.getenv("SDK_DECISIONS_BATCH_MAX", "1000"))


ALLOWED_NODE_TYPES = {"condition", "and", "or", "approve", "review", "reject"}
# Outcomes a rule group may resolve to. Includes the gate markers "pass"/"fail"
# (continue / stop) used by gate rules, which the executor evaluates natively.
RULE_TREE_OUTCOMES = {"approve", "review", "reject", "pass", "fail"}
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
    # Stable per-subject key used for A/B experiment assignment (hashed to a variant).
    # Without it, a running experiment can never assign a variant to this decision.
    user_id: Optional[str] = Field(default=None, alias="userId")


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


class LifecycleTransitionRequest(BaseModel):
    target: str
    actor: Optional[str] = None
    note: Optional[str] = None


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
    branding: Optional[Dict[str, Any]] = None


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


class ExperimentPromoteRequest(BaseModel):
    variant_id: str
    promoted_by: Optional[str] = None
    force: bool = False


class SdkDecideRequest(BaseModel):
    policyId: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    userId: Optional[str] = None
    requestId: Optional[str] = None
    sdkVersion: Optional[str] = None


class SdkEventsRequest(BaseModel):
    events: List[Dict[str, Any]] = Field(default_factory=list)


class SdkDecisionsBatchRequest(BaseModel):
    """A batch of on-device decisions drained from a device's local outbox. Each
    decision carries a client-stable `id` for idempotent, retry-safe ingestion."""
    decisions: List[Dict[str, Any]] = Field(default_factory=list)


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
        # Gate rules (the majority — Bureau Threshold, KYC Gate, Affordability…) use
        # the continue-marker outcomes "pass"/"fail", which the runtime supports
        # (see the outcome precedence map in executor._merge_outcome). Accepting only
        # approve/review/reject made those rules un-editable: GET → PUT round-tripped
        # to a 422. Allow the full outcome vocabulary the engine actually evaluates.
        if tree.get("onPass") not in RULE_TREE_OUTCOMES:
            raise HTTPException(status_code=422, detail="Rule tree onPass must be one of: {0}.".format(", ".join(sorted(RULE_TREE_OUTCOMES))))
        if tree.get("onFail") not in RULE_TREE_OUTCOMES:
            raise HTTPException(status_code=422, detail="Rule tree onFail must be one of: {0}.".format(", ".join(sorted(RULE_TREE_OUTCOMES))))
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
        if step_type == "workflow" and not ref_id:
            raise HTTPException(status_code=422, detail="Sub-workflow step requires a ref_id (target policy).")
        if step_type not in {"connector", "rule", "scorecard", "decision_table", "outcome", "transform", "action", "review_gate", "model", "branch", "loop", "workflow", "monitor"}:
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


def test_policy_entity(policy: Dict[str, Any], payload: Optional[Dict[str, Any]], source: str = "test_console", user_id: Optional[str] = None) -> Dict[str, Any]:
    started = time.perf_counter()
    executor = workflow_executor()
    # The executor is the single canonical decision logger — it writes exactly one
    # Decision row (source-tagged) at the end of execute(). We must NOT also write one
    # here, or every /decide would double-log (inflating reports + usage metering).
    # user_id is threaded through so a running A/B experiment can assign a variant.
    ctx = asyncio.run(executor.execute(policy=policy, payload=payload or {}, tenant_id=active_tenant_id(), source=source, user_id=user_id))
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
        "outcome": ctx.outcome if ctx.outcome != "pending" else (policy.get("defaultOutcome") or "review"),
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
    tenant_id = active_tenant_id()
    # The ledger records the AUTHENTICATED actor, not the client-supplied label — so dual-control
    # can trust it. (promoted_by is kept as the human-readable reason/attribution.)
    actor = active_actor()
    # Dual control (maker != checker): when enabled, promoting to production must be done by a
    # different member than the one who promoted it to UAT. This is a genuine two-person control
    # over the dev -> uat -> prod path, enforced without any schema change.
    if target_status == "prod" and storage.dual_control_enabled(tenant_id):
        maker = storage.last_promotion_actor(entity_type, entity_id, "uat", tenant_id=tenant_id)
        if maker is not None and maker == actor:
            raise HTTPException(
                status_code=403,
                detail="Dual control: promotion to production must be approved by a different member "
                       "than the one who promoted it to UAT.",
            )
    updated = updater(entity_id, {"status": target_status}, bump_version=False)
    # Snapshot the policy's decision definition so the next promotion can be diffed against this
    # one (and this change is recorded on the approval for audit).
    snapshot = None
    if entity_type == "policy":
        from .policy_diff import policy_snapshot

        snapshot = policy_snapshot(storage, tenant_id, entity)
    # Store the authenticated actor as promoted_by so the ledger (and the dual-control check on the
    # next stage) reflects who really acted; fall back to the supplied label when unauthenticated.
    ledger_actor = actor if actor != "system" else (promoted_by or "system")
    storage.add_promotion(entity_type, entity_id, entity["status"], target_status, ledger_actor, reason, snapshot=snapshot)
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
    # Any publish/update invalidates the fast-path serving cache for this tenant.
    from .fast_decide import invalidate as invalidate_fast_cache

    invalidate_fast_cache(tenant_id)
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


def active_role(request: Optional[Request] = None) -> str:
    if request is not None and getattr(request.state, "role", None):
        return str(request.state.role)
    return str(get_current_role() or "owner")


def active_actor(request: Optional[Request] = None) -> str:
    """The authenticated actor's stable id (member session -> "member:<id>", else the API key id).
    This is the trustworthy identity used for dual-control and the promotion ledger — it is derived
    from the authenticated session, never from a client-supplied field."""
    if request is not None and getattr(request.state, "api_key_id", None):
        return str(request.state.api_key_id)
    return str(get_current_api_key_id() or "system")


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
    # Fail closed: refuse to serve in production with default/unset critical secrets.
    verify_production_secrets()
    # Durable decision outbox: replay any WAL entries a previous process appended but was killed
    # (SIGKILL/OOM) before their DB write landed. Idempotent — ids already in the DB are skipped.
    try:
        from . import decision_wal

        if decision_wal.enabled():
            result = decision_wal.recover(storage)
            if result.get("replayed"):
                import logging

                logging.getLogger("rulemind.decision_wal").warning(
                    "decision WAL recovery: replayed %s orphaned decision(s) %s", result["replayed"], result)
    except Exception:  # pragma: no cover - recovery must never block startup
        pass
    if not (is_local_dev() or os.getenv("RULEMIND_RUN_API_SCHEDULER") == "1"):
        return
    try:
        init_scheduler(storage)
    except Exception:
        # Scheduler failures should not block local development or tests.
        return


@app.on_event("shutdown")
async def shutdown() -> None:
    # Flush any in-flight async decision-log writes before the process exits, so a graceful
    # stop / rolling deploy (SIGTERM) never loses a queued decision.
    try:
        from . import decision_log

        decision_log.shutdown()
    except Exception:  # pragma: no cover - best effort
        pass
    # Relinquish scheduler leadership on graceful shutdown so a surviving replica
    # takes over immediately instead of waiting for the lease to expire.
    try:
        from .scheduler import _OWNER_ID, is_leader

        if is_leader():
            storage.release_scheduler_lease(_OWNER_ID)
    except Exception:  # pragma: no cover - best effort
        pass


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


# ── AI Copilot (BYO key, server-side, drafts only) ─────────────────────────────

# ── Access & Roles (RBAC) ──────────────────────────────────────────────────────

class AccessKeyRequest(BaseModel):
    role: str
    label: Optional[str] = None
    environment: str = "prod"


class MemberCreateRequest(BaseModel):
    email: str
    name: Optional[str] = None
    role: str = "viewer"
    password: Optional[str] = None


class MemberUpdateRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class KeyRoleRequest(BaseModel):
    role: str


class LoginRequest(BaseModel):
    email: str
    password: str
    tenant_id: Optional[str] = None


class OtpRequestRequest(BaseModel):
    email: str
    tenant_id: Optional[str] = None


class OtpVerifyRequest(BaseModel):
    email: str
    code: str
    tenant_id: Optional[str] = None


class SsoConfigRequest(BaseModel):
    provider: Optional[str] = None  # "oidc" | "saml"
    enabled: Optional[bool] = None
    # OIDC
    issuer: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None  # "__CLEAR__" to remove
    redirect_uri: Optional[str] = None
    scope: Optional[str] = None
    authorization_endpoint: Optional[str] = None
    token_endpoint: Optional[str] = None
    jwks_uri: Optional[str] = None
    # SAML
    sp_entity_id: Optional[str] = None
    sso_url: Optional[str] = None
    acs_url: Optional[str] = None
    idp_entity_id: Optional[str] = None
    idp_cert: Optional[str] = None
    # Provisioning policy
    allowed_domains: Optional[List[str]] = None
    default_role: Optional[str] = None
    jit_provisioning: Optional[bool] = None


class OidcCallbackRequest(BaseModel):
    code: str
    state: str


class SamlAcsRequest(BaseModel):
    saml_response: Optional[str] = None
    relay_state: Optional[str] = None




# ── Onboarding journey (self-serve: details → dev key → verify → prod key) ─────

class OnboardingSignupRequest(BaseModel):
    company: str
    contact_email: str
    use_case: Optional[str] = None
    plan: str = "standard"


class OnboardingAIRequest(BaseModel):
    opted_in: bool






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




# ═══════════════════════════════════════════════════════════════════════════
# DECISION TABLES (grid authoring + optimiser)
# ═══════════════════════════════════════════════════════════════════════════

class DecisionTableRequest(BaseModel):
    name: str
    description: Optional[str] = None
    hit_policy: str = "first"
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    default_row: Optional[Dict[str, Any]] = None
    status: str = "dev"


class DecisionTableDraft(BaseModel):
    """A full (possibly unsaved) table for analyze/evaluate without persisting."""
    hit_policy: str = "first"
    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    default_row: Optional[Dict[str, Any]] = None


class DecisionTableEvaluateRequest(BaseModel):
    variable_values: Dict[str, Any] = {}


# ═══════════════════════════════════════════════════════════════════════════
# REPORTS BUILDER (dynamic columns, filters, timezone, scheduled email delivery)
# ═══════════════════════════════════════════════════════════════════════════

class ReportRequest(BaseModel):
    name: str
    description: Optional[str] = None
    columns: List[Dict[str, Any]] = []
    filters: Dict[str, Any] = {}
    timezone: str = "UTC"
    schedule: Optional[Dict[str, Any]] = None


class ReportDraft(BaseModel):
    columns: List[Dict[str, Any]] = []
    filters: Dict[str, Any] = {}
    timezone: str = "UTC"


class EmailConfigRequest(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None  # "__CLEAR__" to remove
    from_addr: Optional[str] = None
    use_tls: Optional[bool] = None
    use_ssl: Optional[bool] = None




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


# ── Extracted routers ──────────────────────────────────────────────────────
# Included last, after the shared singletons above (storage, active_tenant_id,
# ensure_exists) are defined. Router handlers read `main.storage` etc. live at
# call time (see app/routers/__init__.py) so they honour the test harness's
# per-test storage swaps, exactly as these in-line endpoints did.
from .routers.governance import router as governance_router  # noqa: E402
from .routers import insights as _insights  # noqa: E402
from .routers.insights import router as insights_router  # noqa: E402
from .routers import authoring as _authoring  # noqa: E402
from .routers.authoring import router as authoring_router  # noqa: E402
from .routers import rules as _rules  # noqa: E402
from .routers.rules import router as rules_router  # noqa: E402
from .routers import policies as _policies  # noqa: E402
from .routers.policies import router as policies_router  # noqa: E402
from .routers.operations import router as operations_router  # noqa: E402
from .routers.reports import router as reports_router  # noqa: E402
from .routers.onboarding import router as onboarding_router  # noqa: E402
from .routers.identity import router as identity_router  # noqa: E402
from .routers.ai import router as ai_router  # noqa: E402
from .routers.experiments import router as experiments_router  # noqa: E402
from .routers.models import router as models_router  # noqa: E402
from .routers.platform import router as platform_router  # noqa: E402
from .routers.sdk import router as sdk_router  # noqa: E402
from .routers import runtime as _runtime  # noqa: E402
from .routers.runtime import router as runtime_router  # noqa: E402

app.include_router(governance_router)
app.include_router(insights_router)
app.include_router(authoring_router)
app.include_router(rules_router)
app.include_router(policies_router)
app.include_router(operations_router)
app.include_router(reports_router)
app.include_router(onboarding_router)
app.include_router(identity_router)
app.include_router(ai_router)
app.include_router(experiments_router)
app.include_router(models_router)
app.include_router(platform_router)
app.include_router(sdk_router)
app.include_router(runtime_router)

# Back-compat: a couple of tests call these handlers as module attributes.
batch_decide = _runtime.batch_decide

# Back-compat: a few tests call these handlers as module attributes (app.main.audit_errors()).
# Re-export the moved handlers so those direct references keep resolving after the extraction.
audit_decisions = _insights.audit_decisions
audit_decisions_count = _insights.audit_decisions_count
audit_promotions = _insights.audit_promotions
audit_errors = _insights.audit_errors
update_connector = _authoring.update_connector
create_variable = _authoring.create_variable
variable_graph = _authoring.variable_graph
create_rule = _rules.create_rule
promote_rule = _rules.promote_rule
test_rule = _rules.test_rule
create_scorecard = _rules.create_scorecard
create_policy = _policies.create_policy
