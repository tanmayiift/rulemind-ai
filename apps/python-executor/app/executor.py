from __future__ import annotations

import asyncio
import ast
import copy
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    # Async-step callbacks: {step_id: callback_payload}. Set by the resume endpoint
    # so a paused async action continues with its provider's response.
    callbacks: Dict[str, Any] = field(default_factory=dict)
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
            callbacks=copy.deepcopy(payload.get("callbacks", {})),
            experiment_id=payload.get("experiment_id"),
            experiment_variant=payload.get("experiment_variant"),
            step_trace=copy.deepcopy(payload.get("step_trace", [])),
            started_at=_datetime_from_iso(payload.get("started_at")) or datetime.utcnow(),
            completed_at=_datetime_from_iso(payload.get("completed_at")),
            total_latency_ms=int(payload.get("total_latency_ms", 0)),
        )


class ActionFailedError(RuntimeError):
    pass


_DECISION_LOG_POOL: Optional[ThreadPoolExecutor] = None


def _decision_log_pool() -> ThreadPoolExecutor:
    """Shared background pool for fire-and-forget decision logging."""
    global _DECISION_LOG_POOL
    if _DECISION_LOG_POOL is None:
        _DECISION_LOG_POOL = ThreadPoolExecutor(max_workers=int(os.getenv("DECISION_LOG_WORKERS", "4")))
    return _DECISION_LOG_POOL


_MAX_WORKFLOW_DEPTH = 10


class PolicyExecutor:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._visited_workflows: set = set()
        # Per-execution catalog cache. A PolicyExecutor is created fresh per
        # request, so memoizing the tenant's connectors/variables/settings/secrets
        # here removes the repeated DB round-trips that dominated decision latency
        # (list_variables was previously queried on every rule/scorecard step, and
        # secrets were re-fetched + decrypted on every step via _context_view).
        self._catalog: Dict[str, Dict[str, Any]] = {}

    def _catalog_for(self, tenant_id: str) -> Dict[str, Any]:
        cat = self._catalog.get(tenant_id)
        if cat is None:
            variables = self.storage.list_variables(tenant_id=tenant_id)
            cat = {
                "connectors": {item["id"]: item for item in self.storage.list_connectors(tenant_id=tenant_id)},
                "variables": variables,
                "variable_lookup": {item["id"]: item for item in variables},
                "settings": self.storage.get_settings(tenant_id=tenant_id),
            }
            self._catalog[tenant_id] = cat
        return cat

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
        cat = self._catalog_for(tenant_id)
        if "secrets" in cat:
            return cat["secrets"]
        secrets: Dict[str, Any] = {}
        for connector in cat["connectors"].values():
            raw = self.storage.get_connector(connector["id"], include_secrets=True, tenant_id=tenant_id) or {}
            config = raw.get("config", {})
            for key, value in config.items():
                if any(marker in key.lower() for marker in ("token", "secret", "password", "api_key", "apikey")):
                    secrets[key] = value
        cat["secrets"] = secrets
        return secrets

    def _payloads_by_source(self, ctx: ExecutionContext) -> Dict[str, Dict[str, Any]]:
        connectors = self._catalog_for(ctx.tenant_id)["connectors"]
        normalized: Dict[str, Dict[str, Any]] = {}
        for connector_id, connector in connectors.items():
            normalized[connector_id] = copy.deepcopy(connector.get("sample_payload", {}))
        for key, value in ctx.payload.items():
            if key in connectors and isinstance(value, dict):
                normalized[key] = copy.deepcopy(value)
        if not any(key in connectors for key in ctx.payload.keys()):
            normalized["custom"] = copy.deepcopy(ctx.payload)
        return normalized

    @staticmethod
    def _collect_condition_variable_ids(node: Any, out: set) -> None:
        if isinstance(node, dict):
            if node.get("type") == "condition" and node.get("variable"):
                out.add(str(node["variable"]))
            for value in node.values():
                PolicyExecutor._collect_condition_variable_ids(value, out)
        elif isinstance(node, list):
            for item in node:
                PolicyExecutor._collect_condition_variable_ids(item, out)

    def _needed_variable_ids(self, policy: Dict[str, Any], rules: Dict[str, Any], scorecards: Dict[str, Any], tenant_id: str) -> set:
        """Variable ids the policy uses, closed over inter-variable dependencies.

        Over-inclusive by design (a substring match on variable code counts as a
        dependency) so we never skip a variable a computation relies on.
        """
        direct: set = set()
        for step in policy.get("steps", []):
            ref_id = step.get("ref_id") or step.get("ref")
            if step.get("type") == "rule" and ref_id in rules:
                rule = rules[ref_id]
                self._collect_condition_variable_ids(rule.get("tree"), direct)
                self._collect_condition_variable_ids(rule.get("nodes"), direct)
            elif step.get("type") == "scorecard" and ref_id in scorecards:
                for factor in scorecards[ref_id].get("bins", []):
                    if factor.get("variable_id"):
                        direct.add(str(factor["variable_id"]))
        by_id = {v["id"]: v for v in self._catalog_for(tenant_id)["variables"]}
        needed: set = set()
        stack = list(direct)
        while stack:
            vid = stack.pop()
            if vid in needed or vid not in by_id:
                continue
            needed.add(vid)
            code = str(by_id[vid].get("code", ""))
            for other in by_id:
                if other != vid and other not in needed and other in code:
                    stack.append(other)
        return needed

    def _compute_variables(self, ctx: ExecutionContext, needed_ids: Optional[set] = None) -> None:
        catalog = self._catalog_for(ctx.tenant_id)
        payloads = self._payloads_by_source(ctx)
        limits = catalog["settings"].get("engine_config", {})
        timeout_ms = int(limits.get("timeout_ms", 2000))
        memory_mb = int(limits.get("memory_mb", 128))
        values: Dict[str, Any] = {}
        for variable in catalog["variables"]:
            if needed_ids is not None and variable["id"] not in needed_ids:
                continue
            source_payload = payloads.get(variable["source_id"], {})
            execution = execute_variable(variable["code"], source_payload, values, timeout_ms=timeout_ms, memory_mb=memory_mb)
            values[variable["id"]] = execution.get("value")
        ctx.variables = values
        ctx.payload = payloads

    async def _run_steps(
        self,
        steps: List[Dict[str, Any]],
        ctx: ExecutionContext,
        rules: Dict[str, Any],
        scorecards: Dict[str, Any],
        connectors: Dict[str, Any],
        source: str,
        depth: int = 0,
        start_index: int = 0,
    ) -> None:
        """Run an ordered list of steps. Reused by the top-level policy, by branch
        arms, and by sub-workflows. Bubbles up a review-gate pause (nested resume
        is a later phase) and stops on an aborting failure."""
        for index in range(start_index, len(steps)):
            step = steps[index]
            if depth == 0:
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
                result, error = await self._execute_step_body(step, ctx, rules, scorecards, connectors, source, depth)
                if ctx.status == "paused":
                    ctx.step_trace.append({"step": step, "result": result, "error": error, "duration_ms": _ms_since(started)})
                    return
            except Exception as exc:  # pragma: no cover - defensive execution path
                error = str(exc)
                if config.get("onFailure") == "abort":
                    ctx.status = "failed"
            ctx.step_trace.append({"step": step, "result": result, "error": error, "duration_ms": _ms_since(started)})
            if ctx.status == "failed":
                return

    async def _execute_step_body(self, step, ctx, rules, scorecards, connectors, source, depth):
        step_type = step.get("type")
        config = step.get("config", {}) if isinstance(step.get("config"), dict) else {}
        if step_type == "connector":
            return self._execute_connector(step, ctx, connectors), None
        if step_type == "rule":
            return self._execute_rule(step, ctx, rules), None
        if step_type == "scorecard":
            return self._execute_scorecard(step, ctx, scorecards), None
        if step_type == "transform":
            return await self._execute_transform(step, ctx), None
        if step_type == "action":
            return await self._execute_action(step, ctx), None
        if step_type == "model":
            return self._execute_model(step, ctx), None
        if step_type == "review_gate":
            return await self._execute_review_gate(step, ctx), None
        if step_type == "outcome":
            ctx.outcome = _merge_outcome(ctx.outcome, step.get("ref_id") or config.get("outcome") or step.get("label") or "review")
            return {"outcome": ctx.outcome}, None
        if step_type == "branch":
            return await self._execute_branch(step, ctx, rules, scorecards, connectors, source, depth), None
        if step_type == "workflow":
            return await self._execute_subworkflow(step, ctx, rules, scorecards, connectors, source, depth), None
        if step_type == "monitor":
            return await self._execute_monitor(step, ctx), None
        return None, "Unknown step type: {0}".format(step_type)

    async def _execute_monitor(self, step: Dict[str, Any], ctx: ExecutionContext) -> Dict[str, Any]:
        """Post-decision monitor: fire an external alert webhook and/or schedule a
        re-evaluation of the subject after a delay (drift / re-apply windows)."""
        config = step.get("config", {}) if isinstance(step.get("config"), dict) else {}
        result: Dict[str, Any] = {"monitor": step.get("name", "monitor")}
        alert_url = config.get("alertUrl")
        if alert_url:
            try:
                await self._fire_action_request(
                    step,
                    ctx,
                    {
                        "url": alert_url,
                        "method": config.get("method", "POST"),
                        "headers": config.get("headers", {}),
                        "bodyTemplate": config.get("payload", {"executionId": "{{execution_id}}", "outcome": "{{outcome}}"}),
                        "timeoutMs": config.get("timeoutMs", 5000),
                    },
                )
                result["alerted"] = True
            except Exception as exc:  # pragma: no cover - alert best-effort
                result["alertError"] = str(exc)
        reeval_days = config.get("reevaluateInDays")
        if reeval_days:
            try:
                self.storage.add_audit_event(
                    {
                        "tenant_id": ctx.tenant_id,
                        "event_type": "monitor_scheduled",
                        "entity_type": "workflow_execution",
                        "entity_id": ctx.execution_id,
                        "detail": "Re-evaluation scheduled in {0} day(s).".format(reeval_days),
                        "metadata": {"policy_id": ctx.policy_id, "reevaluateInDays": reeval_days, "subject": ctx.user_id},
                    },
                    tenant_id=ctx.tenant_id,
                )
                result["reevaluateInDays"] = reeval_days
                result["scheduled"] = True
            except Exception:  # pragma: no cover
                result["scheduled"] = False
        return result

    async def _execute_branch(self, step, ctx, rules, scorecards, connectors, source, depth):
        """Multi-branch routing: run the first branch whose condition matches (or
        `default`). Branches are lists of steps evaluated with the live context."""
        config = step.get("config", {}) if isinstance(step.get("config"), dict) else {}
        branches = config.get("branches", []) or []
        view = self._context_view(ctx)
        for i, branch in enumerate(branches):
            cond = branch.get("condition")
            if not cond or evaluate_condition(cond, view):
                await self._run_steps(branch.get("steps", []) or [], ctx, rules, scorecards, connectors, source, depth + 1)
                return {"branch": i, "label": branch.get("label", i), "matched": True}
        default_steps = config.get("default", []) or []
        if default_steps:
            await self._run_steps(default_steps, ctx, rules, scorecards, connectors, source, depth + 1)
            return {"branch": "default", "matched": True}
        return {"branch": None, "matched": False}

    async def _execute_subworkflow(self, step, ctx, rules, scorecards, connectors, source, depth):
        """Invoke another policy as a step (composition), sharing the live context.
        Guards against cycles and runaway depth."""
        if depth >= _MAX_WORKFLOW_DEPTH:
            raise ValueError("Max sub-workflow depth ({0}) exceeded".format(_MAX_WORKFLOW_DEPTH))
        ref_id = step.get("ref_id") or step.get("ref")
        if not ref_id:
            raise ValueError("Sub-workflow step missing ref_id")
        if ref_id in self._visited_workflows:
            raise ValueError("Sub-workflow cycle detected: {0}".format(ref_id))
        sub_policy = self.storage.get_policy(ref_id, tenant_id=ctx.tenant_id)
        if not sub_policy:
            raise ValueError("Unknown sub-workflow: {0}".format(ref_id))
        self._visited_workflows.add(ref_id)
        await self._run_steps(sub_policy.get("steps", []) or [], ctx, rules, scorecards, connectors, source, depth + 1)
        return {"workflow": ref_id, "outcome": ctx.outcome}

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
        connectors = self._catalog_for(tenant_id)["connectors"]

        # By default every tenant variable is computed (full parity + available for
        # audit/downstream). Setting COMPUTE_ONLY_USED_VARS=1 restricts computation
        # to the variables the policy actually uses (transitively) — a real latency
        # win, but it changes the workload, so it is opt-in rather than default.
        if not resume_from:
            needed = None
            if os.getenv("COMPUTE_ONLY_USED_VARS", "0") == "1":
                needed = self._needed_variable_ids(policy, rules, scorecards, tenant_id)
            self._compute_variables(ctx, needed_ids=needed)

        steps = policy.get("steps", [])
        # Cycle guard for sub-workflow composition — this policy is already active.
        self._visited_workflows = {policy["id"]}
        await self._run_steps(steps, ctx, rules, scorecards, connectors, source, depth=0, start_index=start_step)
        if ctx.status == "paused":
            self._persist_execution(ctx, trigger_type=source)
            return ctx

        if ctx.status == "running":
            ctx.status = "completed"
        ctx.completed_at = datetime.utcnow()
        ctx.total_latency_ms = int((ctx.completed_at - ctx.started_at).total_seconds() * 1000)
        # A WorkflowExecution row only exists so a paused execution can be resumed.
        # A one-shot decision that never paused (and whose policy has no review
        # gate) needs no such row — skipping this write roughly halves the DB
        # writes per decision on the hot path. Resumable executions are always
        # persisted at the pause point above.
        needs_execution_row = (
            ctx.paused_at_step is not None
            or ctx.review_task_id is not None
            or any(step.get("type") == "review_gate" for step in steps)
        )
        if needs_execution_row:
            self._persist_execution(ctx, trigger_type=source)
        # Decision logging can be moved off the request's critical path. Gated by
        # env so tests (which assert the decision immediately) stay deterministic;
        # enable ASYNC_DECISION_LOG=1 in high-QPS deployments.
        if os.getenv("ASYNC_DECISION_LOG", "0") == "1":
            _decision_log_pool().submit(
                self._log_decision, ctx, source, sdk_version, ctx.experiment_variant or experiment_variant
            )
        else:
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
        variable_lookup = self._catalog_for(ctx.tenant_id)["variable_lookup"]
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
        variable_lookup = self._catalog_for(ctx.tenant_id)["variable_lookup"]
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
        # Async/durable action: on first arrival fire the request (to kick off the
        # long-running provider job) then pause the execution; it resumes via the
        # workflow callback endpoint once the provider posts its result back.
        step_id = str(step.get("id") or step.get("name") or "action")
        if config.get("mode") == "async":
            if step_id in ctx.callbacks:
                result = {"async": True, "resumed": True, "callback": ctx.callbacks[step_id]}
                ctx.action_results.append(result)
                # Merge an outcome the provider may have returned.
                callback_outcome = ctx.callbacks[step_id].get("outcome") if isinstance(ctx.callbacks[step_id], dict) else None
                if callback_outcome:
                    ctx.outcome = _merge_outcome(ctx.outcome, str(callback_outcome))
                return result
            # kick off the async job (best-effort) then pause durably
            try:
                if config.get("url"):
                    await self._fire_action_request(step, ctx, config)
            except Exception:  # pragma: no cover - kickoff failures don't block the pause
                pass
            ctx.status = "paused"
            ctx.paused_at_step = ctx.current_step_index
            ctx.pending_operations.append({"type": "async_action", "step_id": step_id, "url": config.get("url")})
            return {"paused": True, "awaiting": "callback", "stepId": step_id, "executionId": ctx.execution_id}
        return await self._fire_action_request(step, ctx, config)

    async def _fire_action_request(self, step: Dict[str, Any], ctx: ExecutionContext, config: Dict[str, Any]) -> Dict[str, Any]:
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

    def _execute_model(self, step: Dict[str, Any], ctx: ExecutionContext) -> Dict[str, Any]:
        from .model_executor import execute_model
        ref_id = step.get("ref_id") or step.get("ref")
        model = self.storage.get_model(ref_id, include_blob=True)
        if not model:
            raise ValueError("Unknown model: {0}".format(ref_id))
        config = step.get("config", {}) if isinstance(step.get("config"), dict) else {}
        input_mapping = config.get("inputMapping", {})
        features = config.get("features")
        output_variable = config.get("outputVariable", f"model_{ref_id}")

        # Build input data from mapping or use variables directly
        if input_mapping:
            from .jsonpath import resolve_jsonpath
            view = self._context_view(ctx)
            input_data = {}
            for target, source_expr in input_mapping.items():
                if isinstance(source_expr, str) and source_expr.startswith("$."):
                    input_data[target] = resolve_jsonpath(source_expr, view)
                else:
                    input_data[target] = ctx.variables.get(str(source_expr), source_expr)
        else:
            input_data = copy.deepcopy(ctx.variables)

        result = execute_model(model["model_blob"], input_data, features=features)
        # Store prediction in variables for downstream steps
        if result.get("prediction") is not None:
            ctx.variables[output_variable] = result["prediction"]
        return result

    async def _execute_review_gate(self, step: Dict[str, Any], ctx: ExecutionContext) -> Dict[str, Any]:
        config = step.get("config", {})
        condition = config.get("condition")
        if condition and not evaluate_condition(condition, self._context_view(ctx)):
            return {"skipped": True, "reason": "Condition not met"}
        timeout_hours = int(config.get("timeoutHours", 48))
        # Human routing + SLA (stored in the snapshot; no schema change needed).
        sla_hours = config.get("slaHours")
        routing = {
            "queue": config.get("assignTo", "default"),
            "role": config.get("role"),
            "priority": config.get("priority", "normal"),
            "sla_at": (datetime.utcnow() + timedelta(hours=int(sla_hours))).replace(microsecond=0).isoformat() + "Z" if sla_hours else None,
        }
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
                    "routing": routing,
                },
                "timeout_at": datetime.utcnow() + timedelta(hours=timeout_hours),
            },
            tenant_id=ctx.tenant_id,
        )
        ctx.status = "paused"
        ctx.paused_at_step = ctx.current_step_index
        ctx.review_task_id = task["id"]
        return {"paused": True, "reviewTaskId": task["id"]}
