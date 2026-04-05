from __future__ import annotations

import asyncio
import ast
import copy
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from .experiments import apply_experiment_overrides, resolve_experiment_assignment
from .jsonpath import evaluate_math_expr, resolve_jsonpath
from .logic import evaluate_rule_definition, evaluate_scorecard, json_dumps, now_iso, redact_payload
from .sandbox import execute_variable
from .storage import Storage, mask_secret_values
from .templates import resolve_template


def _datetime_from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _ms_since(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _coerce_number(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            return value
    return value


def _merge_outcome(current: Optional[str], candidate: Optional[str]) -> str:
    current_value = str(current or "pending")
    candidate_value = str(candidate or current_value)
    precedence = {
        "pending": 0,
        "pass": 1,
        "approve": 2,
        "review": 3,
        "reject": 4,
    }
    current_rank = precedence.get(current_value, 0)
    candidate_rank = precedence.get(candidate_value, 0)
    if candidate_rank > current_rank:
        return candidate_value
    if candidate_rank < current_rank:
        return current_value
    if current_value == "pass" and candidate_value == "approve":
        return candidate_value
    return current_value


def _redact_resolved_secrets(value: Any, secret_values: set[str]) -> Any:
    if not secret_values:
        return value
    if isinstance(value, dict):
        return {key: _redact_resolved_secrets(item, secret_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_resolved_secrets(item, secret_values) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in sorted((item for item in secret_values if item), key=len, reverse=True):
            redacted = redacted.replace(secret, "***REDACTED***")
        return redacted
    return value


def evaluate_condition(condition: str, context: Dict[str, Any]) -> bool:
    tree = ast.parse(condition, mode="eval")

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.BoolOp):
            values = [bool(visit(item)) for item in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
            raise ValueError("Unsupported boolean operator")
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            for operator, comparator in zip(node.ops, node.comparators):
                right = visit(comparator)
                if isinstance(operator, ast.Eq):
                    if left != right:
                        return False
                elif isinstance(operator, ast.NotEq):
                    if left == right:
                        return False
                elif isinstance(operator, ast.Gt):
                    if _coerce_number(left) <= _coerce_number(right):
                        return False
                elif isinstance(operator, ast.GtE):
                    if _coerce_number(left) < _coerce_number(right):
                        return False
                elif isinstance(operator, ast.Lt):
                    if _coerce_number(left) >= _coerce_number(right):
                        return False
                elif isinstance(operator, ast.LtE):
                    if _coerce_number(left) > _coerce_number(right):
                        return False
                else:
                    raise ValueError("Unsupported comparator")
                left = right
            return True
        if isinstance(node, ast.Name):
            return context.get(node.id)
        if isinstance(node, ast.Attribute):
            base = visit(node.value)
            if isinstance(base, dict):
                return base.get(node.attr)
            return getattr(base, node.attr, None)
        if isinstance(node, ast.Subscript):
            base = visit(node.value)
            key = visit(node.slice)
            if isinstance(base, dict):
                return base.get(key)
            if isinstance(base, list) and isinstance(key, int) and 0 <= key < len(base):
                return base[key]
            return None
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(visit(node.operand))
        raise ValueError("Unsupported condition expression")

    return bool(visit(tree))


@dataclass
class ExecutionContext:
    payload: Dict[str, Any]
    tenant_id: str
    policy_id: str
    execution_id: str
    user_id: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    rule_results: List[Dict[str, Any]] = field(default_factory=list)
    scorecard_results: Dict[str, Any] = field(default_factory=dict)
    transform_outputs: Dict[str, Any] = field(default_factory=dict)
    action_results: List[Dict[str, Any]] = field(default_factory=list)
    pending_operations: List[Dict[str, Any]] = field(default_factory=list)
    outcome: str = "pending"
    status: str = "running"
    current_step_index: int = 0
    paused_at_step: Optional[int] = None
    review_task_id: Optional[str] = None
    review_response: Dict[str, Any] = field(default_factory=dict)
    experiment_id: Optional[str] = None
    experiment_variant: Optional[str] = None
    step_trace: List[Dict[str, Any]] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    total_latency_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["started_at"] = self.started_at.replace(microsecond=0).isoformat() + "Z"
        payload["completed_at"] = self.completed_at.replace(microsecond=0).isoformat() + "Z" if self.completed_at else None
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ExecutionContext":
        return cls(
            payload=copy.deepcopy(payload.get("payload", {})),
            tenant_id=str(payload.get("tenant_id", "")),
            policy_id=str(payload.get("policy_id", "")),
            execution_id=str(payload.get("execution_id", "")),
            user_id=payload.get("user_id"),
            variables=copy.deepcopy(payload.get("variables", {})),
            rule_results=copy.deepcopy(payload.get("rule_results", [])),
            scorecard_results=copy.deepcopy(payload.get("scorecard_results", {})),
            transform_outputs=copy.deepcopy(payload.get("transform_outputs", {})),
            action_results=copy.deepcopy(payload.get("action_results", [])),
            pending_operations=copy.deepcopy(payload.get("pending_operations", [])),
            outcome=str(payload.get("outcome", "pending")),
            status=str(payload.get("status", "running")),
            current_step_index=int(payload.get("current_step_index", 0)),
            paused_at_step=payload.get("paused_at_step"),
            review_task_id=payload.get("review_task_id"),
            review_response=copy.deepcopy(payload.get("review_response", {})),
            experiment_id=payload.get("experiment_id"),
            experiment_variant=payload.get("experiment_variant"),
            step_trace=copy.deepcopy(payload.get("step_trace", [])),
            started_at=_datetime_from_iso(payload.get("started_at")) or datetime.utcnow(),
            completed_at=_datetime_from_iso(payload.get("completed_at")),
            total_latency_ms=int(payload.get("total_latency_ms", 0)),
        )


class ActionFailedError(RuntimeError):
    pass


class PolicyExecutor:
    def __init__(self, storage: Storage):
        self.storage = storage

    def _context_view(self, ctx: ExecutionContext) -> Dict[str, Any]:
        scorecards = {
            scorecard_id: result for scorecard_id, result in ctx.scorecard_results.items()
        }
        computed = copy.deepcopy(ctx.transform_outputs.get("computed", {}))
        combined = {key: value for output in ctx.transform_outputs.values() for key, value in output.items()}
        return {
            "payload": ctx.payload,
            "variables": ctx.variables,
            "scorecard": scorecards,
            "computed": computed or combined,
            "transforms": ctx.transform_outputs,
            "outcome": ctx.outcome,
            "execution_id": ctx.execution_id,
            "secrets": self._tenant_secrets(ctx.tenant_id),
            "review": ctx.review_response,
        }

    def _tenant_secrets(self, tenant_id: str) -> Dict[str, Any]:
        secrets: Dict[str, Any] = {}
        for connector in self.storage.list_connectors(tenant_id=tenant_id):
            raw = self.storage.get_connector(connector["id"], include_secrets=True, tenant_id=tenant_id) or {}
            config = raw.get("config", {})
            for key, value in config.items():
                if any(marker in key.lower() for marker in ("token", "secret", "password", "api_key", "apikey")):
                    secrets[key] = value
        return secrets

    def _payloads_by_source(self, ctx: ExecutionContext) -> Dict[str, Dict[str, Any]]:
        connectors = {item["id"]: item for item in self.storage.list_connectors(tenant_id=ctx.tenant_id)}
        normalized: Dict[str, Dict[str, Any]] = {}
        for connector_id, connector in connectors.items():
            normalized[connector_id] = copy.deepcopy(connector.get("sample_payload", {}))
        for key, value in ctx.payload.items():
            if key in connectors and isinstance(value, dict):
                normalized[key] = copy.deepcopy(value)
        if not any(key in connectors for key in ctx.payload.keys()):
            normalized["custom"] = copy.deepcopy(ctx.payload)
        return normalized

    def _compute_variables(self, ctx: ExecutionContext) -> None:
        connectors = {item["id"]: item for item in self.storage.list_connectors(tenant_id=ctx.tenant_id)}
        payloads = self._payloads_by_source(ctx)
        limits = self.storage.get_settings(tenant_id=ctx.tenant_id).get("engine_config", {})
        timeout_ms = int(limits.get("timeout_ms", 2000))
        memory_mb = int(limits.get("memory_mb", 128))
        values: Dict[str, Any] = {}
        for variable in self.storage.list_variables(tenant_id=ctx.tenant_id):
            source_payload = payloads.get(variable["source_id"], {})
            execution = execute_variable(variable["code"], source_payload, values, timeout_ms=timeout_ms, memory_mb=memory_mb)
            values[variable["id"]] = execution.get("value")
        ctx.variables = values
        ctx.payload = payloads

    async def execute(
        self,
        policy: Dict[str, Any],
        payload: Dict[str, Any],
        tenant_id: str,
        user_id: Optional[str] = None,
        resume_from: Optional[ExecutionContext] = None,
        source: str = "api",
        sdk_version: Optional[str] = None,
        experiment_variant: Optional[str] = None,
    ) -> ExecutionContext:
        if resume_from:
            ctx = resume_from
            ctx.status = "running"
            start_step = ctx.current_step_index
        else:
            ctx = ExecutionContext(
                payload=copy.deepcopy(payload),
                tenant_id=tenant_id,
                policy_id=policy["id"],
                execution_id=str(uuid.uuid4()),
                user_id=user_id,
                started_at=datetime.utcnow(),
            )
            self._compute_variables(ctx)
            start_step = 0

        assignment = resolve_experiment_assignment(self.storage, tenant_id, policy["id"], ctx.user_id or (ctx.payload.get("user_id") if isinstance(ctx.payload, dict) else None))
        if assignment:
            ctx.experiment_id = assignment["experiment"]["id"]
            ctx.experiment_variant = assignment["variant"].get("id")
        elif experiment_variant and not ctx.experiment_variant:
            ctx.experiment_variant = experiment_variant

        rules = {item["id"]: item for item in self.storage.list_rules(tenant_id=tenant_id)}
        rules = apply_experiment_overrides(rules, assignment)
        scorecards = {item["id"]: item for item in self.storage.list_scorecards(tenant_id=tenant_id)}
        connectors = {item["id"]: item for item in self.storage.list_connectors(tenant_id=tenant_id)}

        steps = policy.get("steps", [])
        for index in range(start_step, len(steps)):
            step = steps[index]
            ctx.current_step_index = index
            config = step.get("config", {}) if isinstance(step.get("config"), dict) else {}
            condition = config.get("condition")
            if condition and not evaluate_condition(condition, self._context_view(ctx)):
                ctx.step_trace.append({"step": step, "skipped": True, "reason": "Condition not met: {0}".format(condition)})
                continue

            started = time.perf_counter()
            result: Any = None
            error: Optional[str] = None
            try:
                step_type = step.get("type")
                if step_type == "connector":
                    result = self._execute_connector(step, ctx, connectors)
                elif step_type == "rule":
                    result = self._execute_rule(step, ctx, rules)
                elif step_type == "scorecard":
                    result = self._execute_scorecard(step, ctx, scorecards)
                elif step_type == "transform":
                    result = await self._execute_transform(step, ctx)
                elif step_type == "action":
                    result = await self._execute_action(step, ctx)
                elif step_type == "review_gate":
                    result = await self._execute_review_gate(step, ctx)
                    if ctx.status == "paused":
                        ctx.step_trace.append(
                            {
                                "step": step,
                                "result": result,
                                "error": error,
                                "duration_ms": _ms_since(started),
                            }
                        )
                        self._persist_execution(ctx, trigger_type=source)
                        return ctx
                elif step_type == "outcome":
                    ctx.outcome = _merge_outcome(
                        ctx.outcome,
                        step.get("ref_id") or config.get("outcome") or step.get("label") or "review",
                    )
                    result = {"outcome": ctx.outcome}
                else:
                    error = "Unknown step type: {0}".format(step_type)
            except Exception as exc:  # pragma: no cover - defensive execution path
                error = str(exc)
                if config.get("onFailure") == "abort":
                    ctx.status = "failed"
            ctx.step_trace.append(
                {
                    "step": step,
                    "result": result,
                    "error": error,
                    "duration_ms": _ms_since(started),
                }
            )
            if ctx.status == "failed":
                break

        if ctx.status == "running":
            ctx.status = "completed"
        ctx.completed_at = datetime.utcnow()
        ctx.total_latency_ms = int((ctx.completed_at - ctx.started_at).total_seconds() * 1000)
        self._persist_execution(ctx, trigger_type=source)
        self._log_decision(
            ctx,
            source=source,
            sdk_version=sdk_version,
            experiment_variant=ctx.experiment_variant or experiment_variant,
        )
        return ctx

    def _persist_execution(self, ctx: ExecutionContext, trigger_type: str) -> None:
        current = self.storage.get_workflow_execution(ctx.execution_id, tenant_id=ctx.tenant_id)
        payload = {
            "id": ctx.execution_id,
            "tenant_id": ctx.tenant_id,
            "policy_id": ctx.policy_id,
            "status": ctx.status,
            "context": ctx.to_dict(),
            "current_step_index": ctx.current_step_index,
            "trigger_type": trigger_type,
            "trigger_metadata": {},
            "started_at": ctx.started_at,
            "paused_at": datetime.utcnow() if ctx.status == "paused" else None,
            "completed_at": ctx.completed_at,
        }
        if current:
            self.storage.update_workflow_execution(ctx.execution_id, payload, tenant_id=ctx.tenant_id)
        else:
            self.storage.create_workflow_execution(payload, tenant_id=ctx.tenant_id)

    def _log_decision(self, ctx: ExecutionContext, source: str, sdk_version: Optional[str], experiment_variant: Optional[str]) -> None:
        self.storage.add_decision(
            {
                "tenant_id": ctx.tenant_id,
                "id": str(uuid.uuid4()),
                "policy_id": ctx.policy_id,
                "payload": redact_payload(ctx.payload),
                "computed_variables": copy.deepcopy(ctx.variables),
                "rule_results": copy.deepcopy(ctx.rule_results),
                "scorecard_result": next(iter(ctx.scorecard_results.values()), None),
                "trace": copy.deepcopy(ctx.step_trace),
                "outcome": ctx.outcome if ctx.outcome != "pending" else "review",
                "latency_ms": ctx.total_latency_ms,
                "source": source,
                "sdk_version": sdk_version,
                "experiment_variant": experiment_variant,
            },
            tenant_id=ctx.tenant_id,
        )

    def _execute_connector(self, step: Dict[str, Any], ctx: ExecutionContext, connectors: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        ref_id = step.get("ref_id") or step.get("ref")
        payload = ctx.payload.get(ref_id) if isinstance(ctx.payload, dict) else None
        if not payload:
            payload = connectors.get(ref_id, {}).get("sample_payload", {})
            if ref_id:
                ctx.payload[ref_id] = copy.deepcopy(payload)
        return {"status": "ok", "payload_keys": sorted(list((payload or {}).keys()))}

    def _execute_rule(self, step: Dict[str, Any], ctx: ExecutionContext, rules: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        ref_id = step.get("ref_id") or step.get("ref")
        rule = rules.get(ref_id)
        if not rule:
            raise ValueError("Unknown rule: {0}".format(ref_id))
        variable_lookup = {item["id"]: item for item in self.storage.list_variables(tenant_id=ctx.tenant_id)}
        result = evaluate_rule_definition(rule, ctx.variables, variable_lookup)
        ctx.rule_results.append({"rule_id": ref_id, **copy.deepcopy(result)})
        ctx.rule_results[-1]["ruleId"] = ref_id
        ctx.outcome = _merge_outcome(ctx.outcome, result.get("outcome"))
        return result

    def _execute_scorecard(self, step: Dict[str, Any], ctx: ExecutionContext, scorecards: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        ref_id = step.get("ref_id") or step.get("ref")
        scorecard = scorecards.get(ref_id)
        if not scorecard:
            raise ValueError("Unknown scorecard: {0}".format(ref_id))
        variable_lookup = {item["id"]: item for item in self.storage.list_variables(tenant_id=ctx.tenant_id)}
        result = evaluate_scorecard(scorecard, ctx.variables, variable_lookup)
        ctx.scorecard_results[ref_id] = result
        return result

    async def _execute_transform(self, step: Dict[str, Any], ctx: ExecutionContext) -> Dict[str, Any]:
        config = step.get("config", {})
        mapping = config.get("mapping", {})
        output_key = config.get("outputKey", "transformed")
        result = {}
        json_context = self._context_view(ctx)
        for target_field, source_expr in mapping.items():
            if isinstance(source_expr, str) and source_expr.startswith("$."):
                result[target_field] = resolve_jsonpath(source_expr, json_context)
            elif isinstance(source_expr, dict) and "expr" in source_expr:
                result[target_field] = evaluate_math_expr(str(source_expr["expr"]).replace("$.", "root.", 1), json_context)
            else:
                result[target_field] = source_expr
        ctx.transform_outputs[output_key] = result
        return result

    async def _execute_action(self, step: Dict[str, Any], ctx: ExecutionContext) -> Dict[str, Any]:
        config = step.get("config", {})
        view = self._context_view(ctx)
        resolved_secrets: set[str] = set()
        url = resolve_template(config.get("url", ""), view, resolved_secrets)
        method = str(config.get("method", "POST")).upper()
        headers = resolve_template(config.get("headers", {}), view, resolved_secrets)
        body = resolve_template(config.get("bodyTemplate", {}), view, resolved_secrets)
        timeout = int(config.get("timeoutMs", 5000)) / 1000
        retries = int(config.get("retries", 0))
        last_error = None

        if url.startswith("rulemind://simulate/"):
            simulated = {
                "status": int(config.get("simulatedStatus", 202)),
                "success": True,
                "body": json_dumps({"simulated": True, "step": step.get("id"), "url": url}),
                "attempt": 1,
                "queued": True,
                "url": url,
                "method": method,
            }
            ctx.action_results.append(simulated)
            return simulated

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(retries + 1):
                attempt_started = time.perf_counter()
                try:
                    if method == "GET":
                        response = await client.get(url, headers=headers)
                    elif method == "PUT":
                        response = await client.put(url, json=body, headers=headers)
                    elif method == "PATCH":
                        response = await client.patch(url, json=body, headers=headers)
                    else:
                        response = await client.post(url, json=body, headers=headers)
                    result = {
                        "status": response.status_code,
                        "success": 200 <= response.status_code < 300,
                        "body": response.text[:10240],
                        "attempt": attempt + 1,
                    }
                    self.storage.add_action_log(
                        {
                            "tenant_id": ctx.tenant_id,
                            "execution_id": ctx.execution_id,
                            "step_id": step.get("id"),
                            "action_name": step.get("name"),
                            "url": url,
                            "method": method,
                            "request_body": mask_secret_values(_redact_resolved_secrets(copy.deepcopy(body), resolved_secrets)),
                            "response_status": response.status_code,
                            "response_body": response.text[:10240],
                            "latency_ms": _ms_since(attempt_started),
                            "success": result["success"],
                            "retry_count": attempt,
                        },
                        tenant_id=ctx.tenant_id,
                    )
                    ctx.action_results.append(result)
                    if result["success"]:
                        return result
                    last_error = "HTTP {0}".format(response.status_code)
                except Exception as error:
                    last_error = str(error)
                if attempt < retries:
                    await asyncio.sleep((int(config.get("retryBackoffMs", 1000)) * (attempt + 1)) / 1000)

        failure_result = {"success": False, "error": last_error, "attempts": retries + 1}
        ctx.action_results.append(failure_result)
        on_failure = config.get("onFailure", "continue")
        if on_failure == "abort":
            raise ActionFailedError("Action failed: {0}".format(last_error))
        if on_failure == "review_gate":
            ctx.outcome = _merge_outcome(ctx.outcome, "review")
        return failure_result

    async def _execute_review_gate(self, step: Dict[str, Any], ctx: ExecutionContext) -> Dict[str, Any]:
        config = step.get("config", {})
        condition = config.get("condition")
        if condition and not evaluate_condition(condition, self._context_view(ctx)):
            return {"skipped": True, "reason": "Condition not met"}
        timeout_hours = int(config.get("timeoutHours", 48))
        task = self.storage.create_review_task(
            {
                "tenant_id": ctx.tenant_id,
                "execution_id": ctx.execution_id,
                "policy_id": ctx.policy_id,
                "step_id": step.get("id"),
                "queue": config.get("assignTo", "default"),
                "status": "pending",
                "required_fields": copy.deepcopy(config.get("requiredFields", [])),
                "context_snapshot": {
                    "variables": copy.deepcopy(ctx.variables),
                    "rule_results": copy.deepcopy(ctx.rule_results),
                    "scorecard_results": copy.deepcopy(ctx.scorecard_results),
                    "outcome_before_review": ctx.outcome,
                },
                "timeout_at": datetime.utcnow() + timedelta(hours=timeout_hours),
            },
            tenant_id=ctx.tenant_id,
        )
        ctx.status = "paused"
        ctx.paused_at_step = ctx.current_step_index
        ctx.review_task_id = task["id"]
        return {"paused": True, "reviewTaskId": task["id"]}
