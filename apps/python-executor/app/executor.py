from __future__ import annotations

import asyncio
import ast
import copy
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from . import circuit_breaker, input_schema
from .experiments import apply_experiment_overrides, resolve_experiment_assignment
from .jsonpath import evaluate_math_expr, resolve_jsonpath
from .logic import compare, evaluate_rule_definition, evaluate_scorecard, json_dumps, redact_payload
from .sandbox import execute_variable
from .storage import Storage, mask_secret_values
from .egress import (
    EgressBlocked,
    build_pinned_request,
    clamp_poll_interval_ms,
    clamp_polls,
    clamp_retries,
    clamp_timeout_ms,
)
from .templates import resolve_template


def _datetime_from_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _url_host(url: str) -> str:
    """Downstream identity for circuit-breaker keying: the host (with port) of a URL, or the
    raw string if it doesn't parse. Groups all calls to the same target under one breaker."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.netloc or url
    except Exception:
        return url


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
            for operator, comparator in zip(node.ops, node.comparators, strict=False):
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


class EvaluationBudgetExceeded(Exception):
    """A decision ran past its wall-clock budget and was abandoned.

    Raised rather than returned because a partial evaluation is not a decision -- some
    variables computed, some did not, and the ones that did not are None, which is exactly the
    input that silently flips gates (see _fail_closed_on_variable_errors). Surfacing this as a
    504 lets the caller retry; inventing an outcome from half-computed inputs would not.
    """

    def __init__(self, budget_ms: int, elapsed_ms: int, stage: str) -> None:
        self.budget_ms = budget_ms
        self.elapsed_ms = elapsed_ms
        self.stage = stage
        super().__init__(
            "Evaluation budget of {0}ms exceeded after {1}ms during {2}.".format(
                budget_ms, elapsed_ms, stage))


@dataclass
class ExecutionContext:
    payload: Dict[str, Any]
    tenant_id: str
    policy_id: str
    execution_id: str
    user_id: Optional[str] = None
    # Set by _log_decision so callers can return it — /decide used to emit no id at
    # all, leaving no way to deep-link the case it had just created.
    decision_id: Optional[str] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    # {variable_id: message} for variables that FAILED to compute. Their entry in `variables`
    # is None, which is indistinguishable from a legitimately-null value -- and a None fed to a
    # gate silently flips the decision (logic.py returns False for every comparison against
    # None, so "reject if fraud_score >= 90" simply does not reject). Keeping the errors
    # separate is what lets the decision fail closed instead of guessing.
    variable_errors: Dict[str, str] = field(default_factory=dict)
    rule_results: List[Dict[str, Any]] = field(default_factory=list)
    scorecard_results: Dict[str, Any] = field(default_factory=dict)
    decision_table_results: Dict[str, Any] = field(default_factory=dict)
    transform_outputs: Dict[str, Any] = field(default_factory=dict)
    action_results: List[Dict[str, Any]] = field(default_factory=list)
    # Innermost active loop bindings (item + index names -> values); exposed in the
    # context view so loop-body steps and conditions can reference the current item.
    loop_scope: Dict[str, Any] = field(default_factory=dict)
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
    # Wall-clock budget for the whole evaluation. 0 disables it. `deadline` is a
    # time.perf_counter() value, not a timestamp -- it must not move if the wall clock does.
    budget_ms: int = 0
    deadline: Optional[float] = None

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
            decision_table_results=copy.deepcopy(payload.get("decision_table_results", {})),
            transform_outputs=copy.deepcopy(payload.get("transform_outputs", {})),
            action_results=copy.deepcopy(payload.get("action_results", [])),
            loop_scope=copy.deepcopy(payload.get("loop_scope", {})),
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


_MAX_WORKFLOW_DEPTH = 10


# Decision sources whose catalog may come from the process-wide, epoch-stamped cache.
#
# Everything else — the test console, simulations, backtests, anything an author drives —
# reads straight from the DB. Not because the cache is unsafe, but because its TTL backstop
# exists for the writes that DON'T bump the epoch (connector CRUD, `dev` variable edits), and
# those are exactly the edits an author makes and then immediately tests. "Edit a variable,
# hit Test, see the old value for a few seconds" is a correctness bug, whatever the latency
# win. Production traffic replays the same catalog thousands of times and is where the win is.
_SHARED_CATALOG_SOURCES = frozenset({"api", "sdk_server", "sdk_edge", "sdk_edge_resume", "callback"})

# Sources allowed to pre-fill unsupplied fields from each connector's `sample_payload`.
#
# This is an AUTHORING affordance: in the test console you type {"bureau_score": 590} and want
# the other twenty fields to hold plausible values so the policy runs end to end. On real
# traffic it is a correctness hole with no visible symptom. Every connector's sample was seeded
# unconditionally, so `POST /decide {"payload": {}}` returned **approve** on a demo
# bureau_score of 756 -- a caller whose integration silently sent nothing got a confident
# approval with a complete, legitimate-looking audit trail behind it. Failing closed on a
# missing field is recoverable; approving on someone else's demo data is not.
#
# `simulation` keeps it because what-if and backtest are author-driven surfaces that write no
# decision and are read as exploration, and stripping it there would change every existing
# saved scenario. Nothing else does: not api, sdk_*, webhook, cron, callback, review or shadow.
_SAMPLE_SEEDING_SOURCES = frozenset({"test_console", "simulation", "loop_debug"})


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
        # Secrets are memoized separately, not written back into `_catalog`: the catalog entry
        # may be the process-wide shared one, and mutating a shared dict from a request thread
        # is a data race waiting to happen.
        self._secrets: Dict[str, Dict[str, Any]] = {}
        # Set by execute(); decides whether this run may use the shared cross-request cache.
        self._source: str = "api"
        # Environment of the policy being executed (dev/uat/prod). Read only by sub-workflow
        # alias resolution, so an alias can point at a different child per environment.
        self._environment: str = "dev"

    def _catalog_for(self, tenant_id: str) -> Dict[str, Any]:
        cat = self._catalog.get(tenant_id)
        if cat is None:
            cat = self._load_catalog(tenant_id)
            self._catalog[tenant_id] = cat
        return cat

    def _load_catalog(self, tenant_id: str) -> Dict[str, Any]:
        """4 DB reads, paid per request because the executor that memoizes them is itself
        constructed per request (main.workflow_executor). For production traffic that work is
        identical on every call, so it is served from the shared epoch-stamped cache in
        fast_decide — the mechanism the fast path already uses, which `invalidate()` clears
        on publish and which carries a TTL backstop for edits that don't bump the epoch."""
        from . import fast_decide

        if self._source in _SHARED_CATALOG_SOURCES:
            return fast_decide.tenant_catalog(self.storage, tenant_id)
        return fast_decide._build_tenant_catalog(self.storage, tenant_id)

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
            "decision_table": {tid: r.get("outputs", {}) for tid, r in ctx.decision_table_results.items()},
            # Loop bindings live at the top level (referenceable by their `as`/`indexAs`
            # names) plus under a stable `loop` key for JSONPath resolution.
            "loop": dict(ctx.loop_scope),
            **ctx.loop_scope,
            "computed": computed or combined,
            "transforms": ctx.transform_outputs,
            "outcome": ctx.outcome,
            "execution_id": ctx.execution_id,
            "secrets": self._tenant_secrets(ctx.tenant_id),
            "review": ctx.review_response,
        }

    def _tenant_secrets(self, tenant_id: str) -> Dict[str, Any]:
        """Decrypted connector secrets for template resolution.

        The per-connector `get_connector(include_secrets=True)` loop that used to live inline
        here was the "+N" in this path's 8+N queries: one DB read and one Fernet decrypt per
        connector, on every request, because the executor holding the memo was itself
        per-request. Production sources now share it across requests via the epoch cache.

        Still lazy: nothing reads secrets unless an action / transform / review-gate step runs,
        so a pure-rule policy pays nothing. And the result is memoized HERE rather than written
        back into the catalog dict, because that dict may be the shared one.
        """
        cached = self._secrets.get(tenant_id)
        if cached is None:
            from . import fast_decide

            connectors = self._catalog_for(tenant_id)["connectors"]
            if self._source in _SHARED_CATALOG_SOURCES:
                cached = fast_decide.tenant_secrets(self.storage, tenant_id, connectors)
            else:
                cached = fast_decide._build_tenant_secrets(self.storage, tenant_id, connectors)
            self._secrets[tenant_id] = cached
        return cached

    def _validate_input(self, ctx: ExecutionContext) -> None:
        """Check the caller's nested input keys against the sources' declared schema (P06).

        WHY THIS RUNS BEFORE ANYTHING ELSE. An unchecked nested key resolves to None, and
        `logic.py` returns False for every comparison against None -- so a misspelled
        `bureau.scores[0].scoreNmae` does not fail, it re-phrases the rule, and which direction it
        breaks depends on how the author happened to word the gate. Same failure mode as a
        variable that could not compute (_fail_closed_on_variable_errors), one step earlier in the
        pipeline: there the input errored, here it was never checked.

        Off by default. A hard schema check switched on globally would refuse every integration
        already in production, so it is opt-in per tenant and per policy under
        `engine_config.input_validation` -- see app/input_schema.py for the shape.

        Not fail-open: when enforcement finds a blocking issue this RAISES, and no decision is
        produced. `warn` mode records the same issues and lets the decision through, so an
        operator can see what enforcement would reject before switching it on.
        """
        catalog = self._catalog_for(ctx.tenant_id)
        settings = input_schema.resolve_settings(
            catalog["settings"].get("engine_config", {}) or {}, ctx.policy_id)
        if not settings.enabled:
            return
        issues = input_schema.validate_payload(ctx.payload, catalog["connectors"], settings)
        if not issues:
            return
        blocking = [issue for issue in issues if settings.blocks(issue)]
        self._record_input_issues(ctx, issues, blocked=bool(blocking))
        ctx.step_trace.append({
            "type": "input_validation",
            "status": "rejected" if blocking else "warned",
            "mode": settings.mode,
            "issues": [issue.to_dict() for issue in issues],
            "detail": "{0} input problem(s) found; {1} of them block the decision.".format(
                len(issues), len(blocking)),
        })
        if blocking:
            raise input_schema.InputValidationError(issues, policy_id=ctx.policy_id, blocking=blocking)

    def _record_input_issues(
        self, ctx: ExecutionContext, issues: List[input_schema.InputIssue], blocked: bool
    ) -> None:
        """Make a bad payload observable whether or not it was rejected.

        Warn mode exists to be watched: an operator has to be able to see the typo'd integration
        without reading decisions one at a time. Best-effort -- recording must never itself break
        the decision.
        """
        try:
            self.storage.add_error_event(
                {
                    "tenant_id": ctx.tenant_id,
                    "scope": "decision",
                    "stage": "input_validation",
                    "entity_type": "policy",
                    "entity_id": ctx.policy_id,
                    "message": "Input payload {0}: {1} problem(s) at {2}".format(
                        "rejected" if blocked else "flagged (warn mode)",
                        len(issues),
                        ", ".join(issue.path for issue in issues[:10])),
                    "details": {
                        "policy_id": ctx.policy_id,
                        "execution_id": ctx.execution_id,
                        "blocked": blocked,
                        "issues": [issue.to_dict() for issue in issues],
                    },
                },
                tenant_id=ctx.tenant_id,
            )
        except Exception:  # noqa: BLE001 - observability must not break the decision
            pass

    def _payloads_by_source(self, ctx: ExecutionContext) -> Dict[str, Dict[str, Any]]:
        connectors = self._catalog_for(ctx.tenant_id)["connectors"]
        seed_samples = self._source in _SAMPLE_SEEDING_SOURCES
        normalized: Dict[str, Dict[str, Any]] = {}
        for connector_id, connector in connectors.items():
            # See _SAMPLE_SEEDING_SOURCES: on production traffic an unsupplied field must stay
            # unsupplied, so the variable that reads it fails and the decision fails closed --
            # rather than quietly evaluating against demo data.
            normalized[connector_id] = (
                copy.deepcopy(connector.get("sample_payload", {})) if seed_samples else {})

        # A source-keyed dict (e.g. {"loan": {...}}) replaces that source wholesale;
        # everything else is a flat top-level field.
        flat_fields: Dict[str, Any] = {}
        for key, value in ctx.payload.items():
            if key in connectors and isinstance(value, dict):
                normalized[key] = copy.deepcopy(value)
            else:
                flat_fields[key] = value

        # Field-level override: a flat top-level field drives EVERY source that
        # declares it in its schema. Without this, an uploaded/synthetic/JSON case
        # like {"bureau_score": 590} never reached the loan source's variables — the
        # source silently kept its (approving) sample_payload, so every case looked
        # identical. This is the fix for "all cases approve regardless of input".
        if flat_fields:
            for connector_id, connector in connectors.items():
                for field in connector.get("schema_paths", []) or []:
                    if field in flat_fields:
                        normalized[connector_id][field] = flat_fields[field]

        # `custom` always carries the flat fields so custom-source variables work
        # whether or not a source-keyed dict was also supplied.
        normalized["custom"] = copy.deepcopy(flat_fields) if flat_fields else copy.deepcopy(ctx.payload)
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
            ref_id = str(step.get("ref_id") or step.get("ref") or "")
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
            # Two-part enforcement. The check abandons the run between variables; the clamp
            # stops the NEXT variable from overshooting the budget on its own -- without it a
            # 2000ms variable started with 5ms of budget left would still run the full 2000ms,
            # and with 55 variables that error compounds.
            self._check_budget(ctx, "variable computation")
            remaining = self._budget_remaining_ms(ctx)
            effective_timeout = timeout_ms if remaining is None else max(1, min(timeout_ms, remaining))
            source_payload = payloads.get(variable["source_id"], {})
            execution = execute_variable(variable["code"], source_payload, values, timeout_ms=effective_timeout, memory_mb=memory_mb)
            values[variable["id"]] = execution.get("value")
            # The error used to be discarded here. A sandbox timeout returns
            # {"value": None, "error": "..."} rather than raising, so the variable became a
            # silent None -- and under load the sandbox queue manufactures those from perfectly
            # healthy code. The fast path already recorded them (fast_decide.py:216-219, with a
            # comment saying a None fed to a gate can flip a decision); this is the DEFAULT
            # path, and it did not.
            if execution.get("error"):
                ctx.variable_errors[variable["id"]] = str(execution.get("error"))
        ctx.variables = values
        ctx.payload = payloads
        self._record_variable_errors(ctx)

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
            # Variables are not the only slow thing: an action step makes an outbound HTTP call
            # with its own timeoutMs and retries, so a policy can blow the budget without
            # computing a single variable.
            self._check_budget(ctx, "step {0}".format(step.get("id") or index))
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
            except EvaluationBudgetExceeded:
                # Must escape this handler. A sub-workflow step recurses into _run_steps, so a
                # budget exception raised in a NESTED policy would otherwise be caught here and
                # downgraded into an ordinary step error -- the run would keep going, past the
                # deadline, and the budget would silently apply only to the top level.
                raise
            except Exception as exc:  # a step raised — do not silently drop it
                error = str(exc)
                # Accuracy is paramount: a decision GATE that errors must not be silently
                # skipped, or a decision could pass a gate it should have failed (fail-open).
                # Fail CLOSED — escalate to human review — and record an observable error event
                # (not just the trace line) so it's alertable, never a silent wrong decision.
                #
                # `workflow` belongs in this set for the same reason, and did not used to be in
                # it. A sub-workflow is a bundle of gates; the errors that escape it are exactly
                # the ones that mean NONE of them ran -- an unregistered alias, a dangling id, a
                # cycle, depth exhaustion. The parent then finished with the child's rejections
                # simply absent, which is the fail-open the child was composed in to prevent.
                # (A step failing INSIDE the child is caught by the child's own _run_steps at
                # depth+1 and handled there, so this only escalates whole-child failures.)
                if step.get("type") in ("rule", "scorecard", "decision_table", "workflow"):
                    ctx.outcome = _merge_outcome(ctx.outcome, "review")
                    self._record_gate_error(ctx, step, error)
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
        if step_type == "decision_table":
            return self._execute_decision_table(step, ctx), None
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
        if step_type == "loop":
            return await self._execute_loop(step, ctx, rules, scorecards, connectors, source, depth), None
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

    def _resolve_collection(self, over: Any, ctx: ExecutionContext) -> Any:
        """Resolve a loop's iterable from config: a literal list, a `$.`-JSONPath,
        or a dotted path (e.g. `payload.line_items`) into the context view."""
        if isinstance(over, list):
            return over
        if not isinstance(over, str) or not over:
            return None
        view = self._context_view(ctx)
        if over.startswith("$."):
            return resolve_jsonpath(over, view)
        current: Any = view
        for token in over.split("."):
            if isinstance(current, dict):
                current = current.get(token)
            elif isinstance(current, list) and token.isdigit():
                idx = int(token)
                current = current[idx] if 0 <= idx < len(current) else None
            else:
                return None
        return current

    async def _execute_loop(self, step, ctx, rules, scorecards, connectors, source, depth):
        """Iterate a set of sub-steps over a collection (or a map's items). Each
        iteration exposes the current item + index under the configured names, and
        a per-iteration summary is captured for the loop-debug endpoint. Bubbles a
        durable pause up (mid-loop resume is a later phase)."""
        if depth >= _MAX_WORKFLOW_DEPTH:
            raise ValueError("Max loop/workflow nesting depth ({0}) exceeded".format(_MAX_WORKFLOW_DEPTH))
        config = step.get("config", {}) if isinstance(step.get("config"), dict) else {}
        as_name = str(config.get("as", "item"))
        index_name = str(config.get("indexAs", "index"))
        sub_steps = config.get("steps", []) or []
        hard_cap = 10000
        try:
            max_iterations = min(int(config.get("maxIterations", 1000)), hard_cap)
        except (TypeError, ValueError):
            max_iterations = 1000

        collection = self._resolve_collection(config.get("over", config.get("items")), ctx)
        # Map objects iterate as {key, value} pairs; anything non-iterable is a no-op.
        if isinstance(collection, dict):
            items: List[Any] = [{"key": k, "value": v} for k, v in collection.items()]
        elif isinstance(collection, list):
            items = collection
        else:
            items = []

        outer_scope = ctx.loop_scope
        truncated = len(items) > max_iterations
        sliced = items[:max_iterations]

        # Concurrent map: when each iteration is independent (typically a connector /
        # http_request per line-item), run them concurrently on isolated cloned
        # contexts — the main latency lever for I/O-bound loops. Effects merge back in
        # deterministic index order. `mode: "parallel"` (+ optional `concurrency`).
        mode = str(config.get("mode", "sequential")).lower()
        if mode == "parallel" and sliced:
            iterations = await self._run_loop_parallel(
                sliced, sub_steps, ctx, rules, scorecards, connectors, source, depth,
                as_name, index_name, outer_scope, config,
            )
        else:
            iterations = []
            for idx, item in enumerate(sliced):
                ctx.loop_scope = {**outer_scope, as_name: item, index_name: idx}
                outcome_before = ctx.outcome
                trace_start = len(ctx.step_trace)
                await self._run_steps(sub_steps, ctx, rules, scorecards, connectors, source, depth + 1)
                iterations.append({
                    "index": idx,
                    "item": copy.deepcopy(item),
                    "outcome_before": outcome_before,
                    "outcome_after": ctx.outcome,
                    "steps": copy.deepcopy(ctx.step_trace[trace_start:]),
                    "paused": ctx.status == "paused",
                    "failed": ctx.status == "failed",
                })
                if ctx.status in {"paused", "failed"}:
                    break
            ctx.loop_scope = outer_scope

        return {
            "loop": step.get("id") or step.get("name") or "loop",
            "over": config.get("over"),
            "count": len(items),
            "iterations_run": len(iterations),
            "truncated": truncated,
            "mode": mode if mode == "parallel" else "sequential",
            "iterations": iterations,
        }

    def _clone_for_iteration(self, ctx: "ExecutionContext", loop_scope: Dict[str, Any]) -> "ExecutionContext":
        """A per-iteration child context for parallel loops: shares the immutable
        inputs (payload, tenant) but gets its own copies of everything sub-steps
        write, so concurrent iterations never race on shared state."""
        child = copy.copy(ctx)
        child.variables = dict(ctx.variables)
        child.rule_results = []
        child.scorecard_results = {}
        child.decision_table_results = {}
        child.transform_outputs = {}
        child.action_results = []
        child.step_trace = []
        child.pending_operations = []
        child.loop_scope = loop_scope
        child.outcome = ctx.outcome
        child.status = "running"
        return child

    async def _run_loop_parallel(self, sliced, sub_steps, ctx, rules, scorecards, connectors,
                                 source, depth, as_name, index_name, outer_scope, config):
        hard_cap_concurrency = 32
        try:
            concurrency = max(1, min(int(config.get("concurrency", 8)), hard_cap_concurrency))
        except (TypeError, ValueError):
            concurrency = 8
        semaphore = asyncio.Semaphore(concurrency)
        entry_outcome = ctx.outcome

        async def run_one(idx: int, item: Any):
            async with semaphore:
                child = self._clone_for_iteration(ctx, {**outer_scope, as_name: item, index_name: idx})
                await self._run_steps(sub_steps, child, rules, scorecards, connectors, source, depth + 1)
                return idx, item, child

        results = await asyncio.gather(*[run_one(idx, item) for idx, item in enumerate(sliced)])
        results.sort(key=lambda entry: entry[0])

        # A durable pause can't be represented across concurrent iterations — reject it
        # clearly rather than silently dropping the resume point.
        if any(child.status == "paused" for _, _, child in results):
            raise ValueError("Review gates / durable pauses are not supported inside a parallel loop; use sequential mode.")

        iterations: List[Dict[str, Any]] = []
        cumulative = entry_outcome
        for idx, item, child in results:
            # Merge child effects into the parent in index order (parity with sequential).
            ctx.rule_results.extend(child.rule_results)
            ctx.action_results.extend(child.action_results)
            ctx.scorecard_results.update(child.scorecard_results)
            ctx.decision_table_results.update(child.decision_table_results)
            ctx.transform_outputs.update(child.transform_outputs)
            ctx.step_trace.extend(child.step_trace)
            # Reproduce the sequential loop's cumulative outcome merge: each iteration's
            # outcome_after is the running merge up to and including this item. The
            # precedence merge is order-independent, so parallel matches sequential.
            outcome_before = cumulative
            cumulative = _merge_outcome(cumulative, child.outcome)
            iterations.append({
                "index": idx,
                "item": copy.deepcopy(item),
                "outcome_before": outcome_before,
                "outcome_after": cumulative,
                "steps": copy.deepcopy(child.step_trace),
                "paused": False,
                "failed": child.status == "failed",
            })
        ctx.loop_scope = outer_scope
        ctx.outcome = cumulative
        if any(it["failed"] for it in iterations):
            ctx.status = "failed"
        return iterations

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

    def _resolve_workflow_alias(self, alias: str, ctx: ExecutionContext) -> str:
        """Map a sub-workflow alias to a policy id, within THIS tenant (P08).

        The registry is `engine_config.workflow_aliases` on tenant settings -- the same
        per-tenant settings pattern as `engine_config.shadow_map`. Two forms:

            "workflow_aliases": {"kyc_check": "policy_kyc_v3"}
            "workflow_aliases": {"kyc_check": {"dev": "policy_kyc_v4", "prod": "policy_kyc_v3"}}

        The second is the point of the feature: a parent names `kyc_check` once and never has to
        be rewritten as the child moves dev -> uat -> prod, or as a new version of the child
        replaces it in one environment at a time. `default` covers environments not listed.

        CROSS-TENANT IS STRUCTURALLY IMPOSSIBLE, not merely checked. The registry is read from
        this tenant's own settings and the id it yields is loaded with a tenant-scoped
        get_policy, so a tenant can only ever name policies it already owns; an alias pointing at
        another tenant's id resolves to nothing and fails closed like any other dangling alias.

        Returns "" when the alias is not registered. The caller treats that as fatal -- there is
        deliberately NO fallback to "maybe a policy has this id", because a fallback would make a
        typo'd alias silently run a policy nobody named.
        """
        registry = self._catalog_for(ctx.tenant_id)["settings"].get("engine_config", {}) or {}
        entry: Any = (registry.get("workflow_aliases") or {}).get(alias)
        if isinstance(entry, dict):
            entry = entry.get(self._environment) or entry.get("default")
        return str(entry) if entry else ""

    async def _execute_subworkflow(self, step, ctx, rules, scorecards, connectors, source, depth):
        """Invoke another policy as a step (composition), sharing the live context.
        Guards against cycles and runaway depth.

        A child may be named by raw `ref_id` or by a stable `config.alias`. The alias WINS when
        both are present: a step that names an alias has opted into indirection, and quietly
        running a stale hard-coded id when the registry does not answer is precisely the bug the
        alias exists to prevent.
        """
        if depth >= _MAX_WORKFLOW_DEPTH:
            raise ValueError("Max sub-workflow depth ({0}) exceeded".format(_MAX_WORKFLOW_DEPTH))
        config = step.get("config", {}) if isinstance(step.get("config"), dict) else {}
        alias = str(config.get("alias") or step.get("alias") or "")
        ref_id = str(step.get("ref_id") or step.get("ref") or "")
        if alias:
            resolved = self._resolve_workflow_alias(alias, ctx)
            if not resolved:
                raise ValueError(
                    "Sub-workflow alias '{0}' is not registered for this workspace "
                    "(engine_config.workflow_aliases)".format(alias))
            ref_id = resolved
        if not ref_id:
            raise ValueError("Sub-workflow step missing ref_id")
        # Cycle check on the RESOLVED id, never on the alias. Two aliases can point at one policy
        # (and an alias can point at a policy a parent also names by raw id), so guarding the
        # name would let A -> B -> A back in through a second spelling of B.
        if ref_id in self._visited_workflows:
            raise ValueError("Sub-workflow cycle detected: {0}".format(ref_id))
        sub_policy = self.storage.get_policy(ref_id, tenant_id=ctx.tenant_id)
        if not sub_policy:
            raise ValueError("Unknown sub-workflow: {0}".format(ref_id))
        self._visited_workflows.add(ref_id)
        await self._run_steps(sub_policy.get("steps", []) or [], ctx, rules, scorecards, connectors, source, depth + 1)
        result = {"workflow": ref_id, "outcome": ctx.outcome}
        if alias:
            result["alias"] = alias
        return result

    async def debug_loop(
        self,
        loop_step: Dict[str, Any],
        payload: Dict[str, Any],
        tenant_id: str,
        variable_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Evaluate a single loop step against a payload and return its full
        per-iteration trace, without persisting a decision. Powers the loop-debug
        endpoint. If `variable_values` is given they are used verbatim; otherwise
        the tenant's variables are computed from the payload."""
        ctx = ExecutionContext(
            payload=copy.deepcopy(payload or {}),
            tenant_id=tenant_id,
            policy_id="__loop_debug__",
            execution_id=str(uuid.uuid4()),
            started_at=datetime.utcnow(),
        )
        # An authoring surface: debug_loop never goes through execute(), so _source would
        # otherwise still read "api" and the sample pre-fill this endpoint relies on would stop.
        self._source = "loop_debug"
        rules = {item["id"]: item for item in self.storage.list_rules(tenant_id=tenant_id)}
        scorecards = {item["id"]: item for item in self.storage.list_scorecards(tenant_id=tenant_id)}
        self._decision_tables = {item["id"]: item for item in self.storage.list_decision_tables(tenant_id=tenant_id)}
        connectors = self._catalog_for(tenant_id)["connectors"]
        self._visited_workflows = {"__loop_debug__"}
        if variable_values is not None:
            ctx.variables = dict(variable_values)
        else:
            self._compute_variables(ctx)
        result = await self._execute_loop(loop_step, ctx, rules, scorecards, connectors, "loop_debug", depth=0)
        result["outcome"] = ctx.outcome
        result["status"] = ctx.status
        return result

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
        simulate: bool = False,
    ) -> ExecutionContext:
        # Simulation/backtest mode: compute the outcome without any side effects —
        # no decision log, no WorkflowExecution row, and review gates resolve to a
        # terminal "review" instead of pausing + creating a task. Lets a batch of
        # thousands of what-if cases run concurrently without DB write contention.
        self._simulate = simulate
        # Recorded before any catalog read: it decides whether this run may serve its tenant
        # catalog from the shared cross-request cache (production sources) or must read the
        # DB (authoring/simulation). A simulate=True run never shares, whatever its source —
        # backtests are author-driven and must see the edit being tested.
        self._source = "simulation" if simulate else source
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

        # Start the clock before ANY work, including the catalog reads below -- a decision that
        # spends its whole budget on a slow DB is just as timed out as one that spends it in the
        # sandbox, and the caller cannot tell the difference anyway.
        self._start_budget(ctx)
        # Sub-workflow aliases can resolve to a different child per environment, so record which
        # environment this run is in before any step executes.
        self._environment = str(policy.get("status") or "dev")

        # Check the caller's input BEFORE computing anything. A resume carries the payload that
        # was already validated at the original run, so re-checking it would only be able to
        # reject a decision a human is already reviewing.
        if not resume_from:
            self._validate_input(ctx)

        assignment = resolve_experiment_assignment(self.storage, tenant_id, policy["id"], ctx.user_id or (ctx.payload.get("user_id") if isinstance(ctx.payload, dict) else None))
        if assignment:
            ctx.experiment_id = assignment["experiment"]["id"]
            ctx.experiment_variant = assignment["variant"].get("id")
        elif experiment_variant and not ctx.experiment_variant:
            ctx.experiment_variant = experiment_variant

        rules = {item["id"]: item for item in self.storage.list_rules(tenant_id=tenant_id)}
        rules = apply_experiment_overrides(rules, assignment)
        scorecards = {item["id"]: item for item in self.storage.list_scorecards(tenant_id=tenant_id)}
        self._decision_tables = {item["id"]: item for item in self.storage.list_decision_tables(tenant_id=tenant_id)}
        connectors = self._catalog_for(tenant_id)["connectors"]

        # By default every tenant variable is computed (full parity + available for
        # audit/downstream). Setting COMPUTE_ONLY_USED_VARS=1 restricts computation
        # to the variables the policy actually uses (transitively) — a real latency
        # win, but it changes the workload, so it is opt-in rather than default.
        if not resume_from:
            needed = None
            # Simulation only needs the variables the policy actually reads —
            # computing the full catalog per case is the dominant backtest cost.
            if simulate or os.getenv("COMPUTE_ONLY_USED_VARS", "0") == "1":
                needed = self._needed_variable_ids(policy, rules, scorecards, tenant_id)
            self._compute_variables(ctx, needed_ids=needed)

        steps = policy.get("steps", [])
        # Cycle guard for sub-workflow composition — this policy is already active.
        self._visited_workflows = {policy["id"]}
        await self._run_steps(steps, ctx, rules, scorecards, connectors, source, depth=0, start_index=start_step)
        if ctx.status == "paused":
            if not self._simulate:
                self._persist_execution(ctx, trigger_type=source)
            return ctx

        if ctx.status == "running":
            ctx.status = "completed"
        self._fail_closed_on_variable_errors(ctx)
        ctx.completed_at = datetime.utcnow()
        ctx.total_latency_ms = int((ctx.completed_at - ctx.started_at).total_seconds() * 1000)
        # Simulation is a pure what-if: no decision log, no execution row.
        if self._simulate:
            return ctx
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
        # Decision logging runs off the request's critical path by default (the
        # single biggest per-decision latency tax). Reads flush pending writes for
        # in-process read-after-write; ASYNC_DECISION_LOG=0 forces synchronous.
        # _log_decision builds the record and routes through decision_log.persist_decision, which
        # does the WAL append + async submit itself — so call it directly (no outer submit()) to
        # keep a single async hop and correct flush()-based read-after-write.
        self._log_decision(ctx, source, sdk_version, ctx.experiment_variant or experiment_variant)
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

    # Default budget. A healthy decision is single-digit milliseconds, so 10s is ~1000x
    # headroom -- this is a SAFETY bound, not an SLO. Before it existed there was no bound at
    # all: the demo tenant alone has 55 variables at a 2000ms per-variable timeout, so one
    # request could legitimately hold a threadpool slot for 110 seconds. Tune it down to your
    # actual SLO; set evaluation_budget_ms = 0 to disable.
    DEFAULT_BUDGET_MS = 10000

    def _start_budget(self, ctx: ExecutionContext) -> None:
        limits = self._catalog_for(ctx.tenant_id)["settings"].get("engine_config", {}) or {}
        try:
            budget = int(limits.get("evaluation_budget_ms", self.DEFAULT_BUDGET_MS))
        except (TypeError, ValueError):
            budget = self.DEFAULT_BUDGET_MS
        ctx.budget_ms = max(0, budget)
        ctx.deadline = (time.perf_counter() + ctx.budget_ms / 1000.0) if ctx.budget_ms else None

    def _budget_remaining_ms(self, ctx: ExecutionContext) -> Optional[int]:
        """Milliseconds left, or None when no budget is set."""
        if ctx.deadline is None:
            return None
        return int((ctx.deadline - time.perf_counter()) * 1000)

    def _check_budget(self, ctx: ExecutionContext, stage: str) -> None:
        """Cooperative deadline check.

        Deliberately NOT asyncio.wait_for. /decide is a sync `def` served from Starlette's
        threadpool, so cancelling the await would return 504 to the caller while the underlying
        work carried on holding its thread -- the timeout would make the overload worse rather
        than shed it. Checking between units of work, and shrinking each variable's own timeout
        to whatever budget is left, bounds the real wall clock instead of just the wait.
        """
        remaining = self._budget_remaining_ms(ctx)
        if remaining is not None and remaining <= 0:
            # remaining is <= 0 here, so this is budget + overshoot -- how far past the deadline
            # the last unit of work actually ran, which is the number an operator needs when
            # deciding whether the budget is too tight or a single step is misbehaving.
            raise EvaluationBudgetExceeded(ctx.budget_ms, ctx.budget_ms - remaining, stage)

    def _fail_closed_on_variable_errors(self, ctx: ExecutionContext) -> None:
        """Send a decision to review when an input it needed could not be computed.

        WHY THIS IS NOT OPTIONAL. A failed variable is None, and logic.py returns False for
        every comparison against None. So the direction a decision fails is decided by how the
        author happened to phrase the rule:

            "reject if fraud_score >= 90"   -> condition False -> NOT rejected   (fails OPEN)
            "approve if bureau_score >= 700" -> condition False -> not approved  (fails closed)

        Every "flag the bad ones" rule -- the entire fraud and risk category -- fails open. And
        scorecards are worse: a null factor contributes 0 points, so the score inflates toward
        approve. Under load, a saturated sandbox pool turns healthy variables into nulls, which
        means peak traffic is exactly when this silently approves the most.

        Deliberately conservative: ANY failed variable sends the decision to review, without
        trying to prove which gates read it. Dependency analysis would be more precise and is
        the wrong trade here -- being wrong in the precise direction means approving a fraud
        case. `engine_config.fail_open_on_variable_error` exists for an operator who has
        measured their own exposure and disagrees; it defaults to off.
        """
        if not ctx.variable_errors:
            return
        settings = self._catalog_for(ctx.tenant_id)["settings"].get("engine_config", {}) or {}
        if settings.get("fail_open_on_variable_error"):
            return
        if ctx.outcome == "review":
            return
        # A reject is NOT "conservative enough" to leave alone. It is an adverse action, and an
        # adverse action has to carry a reason -- so leaving it would mean issuing a customer a
        # denial whose stated reason is an input that never computed. Both directions are wrong
        # when the inputs are unknown; review is the only honest destination.
        original = ctx.outcome
        ctx.outcome = "review"
        ctx.step_trace.append({
            "type": "variable_error_guard",
            "status": "failed_closed",
            "outcome": "review",
            "original_outcome": original,
            "failed_variables": sorted(ctx.variable_errors),
            "detail": "Sent to review: {0} variable(s) could not be computed, and a null input "
                      "silently changes gate outcomes.".format(len(ctx.variable_errors)),
        })

    def _record_variable_errors(self, ctx: ExecutionContext) -> None:
        """Make a failed variable observable.

        Mirrors _record_gate_error's contract: a decision that was affected by a failure must be
        alertable rather than silent. Best-effort -- recording must never break the decision.

        Note what this does NOT do: it does not substitute a value. The decision still has a
        None where a number should be, and `_variables_failed_closed` is what stops that None
        reaching a gate.
        """
        if not ctx.variable_errors:
            return
        for variable_id, message in ctx.variable_errors.items():
            try:
                self.storage.add_error_event(
                    {
                        "tenant_id": ctx.tenant_id,
                        "scope": "decision",
                        "stage": "variable_error",
                        "entity_type": "variable",
                        "entity_id": variable_id,
                        "message": "Variable failed to compute (value is null): {0}".format(message),
                        "details": {"policy_id": ctx.policy_id, "execution_id": ctx.execution_id},
                    },
                    tenant_id=ctx.tenant_id,
                )
            except Exception:  # noqa: BLE001 - observability must not break the decision
                pass

    def _record_gate_error(self, ctx: ExecutionContext, step: Dict[str, Any], message: str) -> None:
        """Record an observable error when a decision gate (rule/scorecard/table) fails to
        evaluate — so a fail-closed decision is alertable, not silent. Best-effort: recording the
        error must never itself break the decision."""
        try:
            self.storage.add_error_event(
                {
                    "tenant_id": ctx.tenant_id,
                    "scope": "decision",
                    "stage": "gate_error",
                    "entity_type": step.get("type"),
                    "entity_id": step.get("ref_id") or step.get("ref") or step.get("id"),
                    "message": "Decision gate errored (failed closed to review): {0}".format(message),
                    "details": {"policy_id": ctx.policy_id, "execution_id": ctx.execution_id},
                },
                tenant_id=ctx.tenant_id,
            )
        except Exception:  # pragma: no cover - observability must not break the decision
            pass

    def _record_action_error(self, ctx: ExecutionContext, step: Dict[str, Any], url: str, method: str, message: str) -> None:
        """Record an observable error when an action is refused or fails. Best-effort.

        For an SSRF block this is the alert that an author tried to reach an internal target --
        the request never left the process, but the attempt is exactly what a security team wants
        to see."""
        try:
            self.storage.add_error_event(
                {
                    "tenant_id": ctx.tenant_id,
                    "scope": "decision",
                    "stage": "action_error",
                    "entity_type": "action",
                    "entity_id": step.get("id"),
                    "message": message,
                    "details": {"policy_id": ctx.policy_id, "execution_id": ctx.execution_id, "method": method},
                },
                tenant_id=ctx.tenant_id,
            )
        except Exception:  # pragma: no cover - observability must not break the decision
            pass

    def _log_decision(self, ctx: ExecutionContext, source: str, sdk_version: Optional[str], experiment_variant: Optional[str]) -> None:
        # client_id is the CASE identifier — what "show me this customer's decision
        # history" filters on. storage.add_decision() reads it off this record, but
        # nothing used to put it here, so the column was always NULL on the /decide
        # path and case-wise history was unreachable through the public API.
        # ctx.user_id is /decide's `userId`; fall back to an explicit client_id in
        # the payload for callers that carry it there.
        payload_client_id = None
        if isinstance(ctx.payload, dict):
            payload_client_id = ctx.payload.get("client_id") or ctx.payload.get("clientId")
        decision_id = str(uuid.uuid4())
        ctx.decision_id = decision_id
        record = {
            "tenant_id": ctx.tenant_id,
            "id": decision_id,
            # NOT ctx.user_id. client_id is the DEVICE idempotency key under
            # UNIQUE(tenant_id, client_id); a userId repeats per customer, so writing it here
            # silently dropped every re-decision. The subject goes in user_id.
            "client_id": payload_client_id,
            "user_id": ctx.user_id,
            "policy_id": ctx.policy_id,
            "payload": redact_payload(ctx.payload, extra_keys=self.storage.tenant_pii_redact_keys(ctx.tenant_id)),
            "computed_variables": copy.deepcopy(ctx.variables),
            "rule_results": copy.deepcopy(ctx.rule_results),
            "scorecard_result": next(iter(ctx.scorecard_results.values()), None),
            "trace": copy.deepcopy(ctx.step_trace),
            "outcome": ctx.outcome if ctx.outcome != "pending" else "review",
            "latency_ms": ctx.total_latency_ms,
            "source": source,
            "sdk_version": sdk_version,
            "experiment_id": ctx.experiment_id,
            "experiment_variant": experiment_variant,
        }
        # WAL-durable (DECISION_WAL=1) then DB write; a failed write is surfaced (metric +
        # error_event) instead of vanishing silently, and the WAL entry is replayed on restart.
        from . import decision_log

        decision_log.persist_decision(self.storage, record, ctx.tenant_id)

    def _execute_connector(self, step: Dict[str, Any], ctx: ExecutionContext, connectors: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        ref_id = str(step.get("ref_id") or step.get("ref") or "")
        payload = ctx.payload.get(ref_id) if isinstance(ctx.payload, dict) else None
        if not payload:
            payload = connectors.get(ref_id, {}).get("sample_payload", {})
            if ref_id:
                ctx.payload[ref_id] = copy.deepcopy(payload)
        return {"status": "ok", "payload_keys": sorted(list((payload or {}).keys()))}

    def _execute_rule(self, step: Dict[str, Any], ctx: ExecutionContext, rules: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        ref_id = str(step.get("ref_id") or step.get("ref") or "")
        rule = rules.get(ref_id)
        if not rule:
            raise ValueError("Unknown rule: {0}".format(ref_id))
        variable_lookup = self._catalog_for(ctx.tenant_id)["variable_lookup"]
        result = evaluate_rule_definition(rule, ctx.variables, variable_lookup)
        ctx.rule_results.append({"rule_id": ref_id, **copy.deepcopy(result)})
        ctx.rule_results[-1]["ruleId"] = ref_id
        ctx.outcome = _merge_outcome(ctx.outcome, result.get("outcome"))
        return result

    def _execute_decision_table(self, step: Dict[str, Any], ctx: ExecutionContext) -> Dict[str, Any]:
        from .decision_tables import evaluate_decision_table

        ref_id = str(step.get("ref_id") or step.get("ref") or "")
        table = getattr(self, "_decision_tables", {}).get(ref_id)
        if not table:
            raise ValueError("Unknown decision table: {0}".format(ref_id))
        variable_lookup = self._catalog_for(ctx.tenant_id)["variable_lookup"]
        result = evaluate_decision_table(table, ctx.variables, variable_lookup)
        ctx.decision_table_results[ref_id] = result
        if result.get("outcome"):
            ctx.outcome = _merge_outcome(ctx.outcome, str(result["outcome"]))
        return result

    def _execute_scorecard(self, step: Dict[str, Any], ctx: ExecutionContext, scorecards: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        ref_id = str(step.get("ref_id") or step.get("ref") or "")
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
        if config.get("mode") == "poll":
            return await self._poll_action(step, ctx, config)
        return await self._fire_action_request(step, ctx, config)

    # ------------------------------------------------------------------ polling actions
    #
    # THE GAP THIS CLOSES. The action step had exactly two shapes: fire once (with
    # retry-on-FAILURE), and `mode: "async"`, which pauses the whole execution until the provider
    # posts back to the workflow callback endpoint. Neither is polling.
    #
    # Retrying is not polling, and the difference is the whole point: a retry loop exits on the
    # first 2xx, so a provider that answers `200 {"status": "pending"}` -- which is the normal
    # shape of a job-status endpoint -- ends the loop on the first attempt with a result the
    # policy then treats as final. The condition polling waits for is on the RESPONSE CONTENT,
    # not on the transport.
    #
    # And async is not polling either: it requires the provider to call back. Every provider that
    # only offers "POST to start, GET to check" -- KYC vendors, document processors, batch bureau
    # pulls -- has no callback to give, so today they can only be modelled as a single shot that
    # reads whatever the first response happened to contain.
    #
    # FAIL CLOSED. Three separate ways this refuses to report success it does not have:
    #   * no condition, or a malformed one, is never satisfied (rather than trivially true);
    #   * a body that is not JSON, or a path that is not in it, resolves to None -- and logic.py
    #     returns False for every comparison against None, so it polls on and then times out;
    #   * running out of attempts defaults to `review_gate`, NOT `continue`. Continuing would
    #     hand the policy a "pending" payload and let a rule phrased as `reject if status ==
    #     "failed"` approve it, which is the exact fail-open shape `_fail_closed_on_variable_errors`
    #     exists to stop. An author who genuinely wants a best-effort poll sets onPollTimeout
    #     explicitly.

    @staticmethod
    def _poll_condition_met(condition: Any, result: Dict[str, Any]) -> bool:
        """Evaluate the exit condition against one response.

        `compare` is logic.py's own primitive -- the operator set the TypeScript, Rust, Kotlin and
        Dart engines are held to by the parity corpus. A poll condition is authored in the same UI
        as a rule condition, so `status == "complete"` has to mean here exactly what it means
        there; a second comparison implementation in this file would be a second set of operator
        semantics to keep in step with four other engines.
        """
        if not isinstance(condition, dict) or not condition:
            return False
        body: Any = result.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (ValueError, TypeError):
                body = None
        view = {"status": result.get("status"), "success": result.get("success"), "body": body}
        path = str(condition.get("path") or condition.get("field") or "")
        if not path:
            return False
        # `$.body.status` is the explicit form. A bare `body.status` is accepted because that is
        # what the condition builder emits for rules, and an author should not have to know which
        # of the two surfaces they are on.
        actual = resolve_jsonpath(path if path.startswith("$.") else "$." + path, view)
        try:
            return bool(compare(
                actual,
                str(condition.get("operator") or condition.get("op") or "=="),
                condition.get("value"),
                condition.get("value2"),
                condition.get("type"),
            ))
        except Exception:  # pragma: no cover - a bad operator must not decide the policy
            return False

    async def _poll_action(self, step: Dict[str, Any], ctx: ExecutionContext, config: Dict[str, Any]) -> Dict[str, Any]:
        condition = config.get("pollUntil") or config.get("until")
        max_polls = clamp_polls(config.get("maxPolls", 3))
        interval_ms = clamp_poll_interval_ms(config.get("pollIntervalMs", 1000))
        step_id = str(step.get("id") or step.get("name") or "action")

        result: Dict[str, Any] = {}
        for attempt in range(max_polls):
            # BEFORE the request, not after: the point is not to start work there is no time to
            # finish. The single-shot path is bounded by clamp_timeout_ms alone because it makes
            # one request; a poll makes up to `max_polls` of them with sleeps in between, so
            # without this the step's ceiling is maxPolls x (timeout + interval) regardless of
            # what the tenant's evaluation budget says.
            self._check_budget(ctx, "action poll ({0})".format(step_id))
            result = await self._fire_action_request(step, ctx, config)
            if self._poll_condition_met(condition, result):
                # Mutated in place, not copied: _fire_action_request appends the very same dict to
                # ctx.action_results, so a copy would leave the recorded attempt without the poll
                # fields and the returned one with them -- two versions of one action.
                result["polls"] = attempt + 1
                result["pollSatisfied"] = True
                ctx.step_trace.append({
                    "type": "action_poll", "stepId": step_id, "status": "satisfied",
                    "polls": attempt + 1, "maxPolls": max_polls,
                })
                return result
            if attempt < max_polls - 1 and interval_ms:
                # Never sleep past the deadline. Sleeping the full interval and letting the NEXT
                # _check_budget raise would report the overshoot as the poll's fault when it was
                # this sleep's; clamping means the budget exception, when it comes, is raised at
                # the moment the budget actually ran out.
                remaining = self._budget_remaining_ms(ctx)
                sleep_ms = interval_ms if remaining is None else max(0, min(interval_ms, remaining))
                if sleep_ms:
                    await asyncio.sleep(sleep_ms / 1000.0)

        # Exhausted. The last response is kept rather than replaced, because an operator
        # debugging a poll that never satisfied needs to see what the provider kept saying -- and
        # it is annotated in place for the same reason as the success branch above, so
        # ctx.action_results holds one entry per HTTP attempt with the last one marked, rather
        # than N attempts plus a synthetic summary that looks like an N+1th call that never
        # happened.
        timed_out = result
        timed_out["polls"] = max_polls
        timed_out["pollSatisfied"] = False
        timed_out["success"] = False
        timed_out["error"] = "poll_condition_not_met after {0} attempt(s)".format(max_polls)
        ctx.step_trace.append({
            "type": "action_poll", "stepId": step_id, "status": "timeout",
            "polls": max_polls, "maxPolls": max_polls,
        })
        self._record_action_error(
            ctx, step, str(config.get("url", "")), str(config.get("method", "POST")).upper(),
            timed_out["error"],
        )
        # Default review_gate, not the single-shot path's `continue` -- see the block comment above.
        on_timeout = config.get("onPollTimeout", "review_gate")
        if on_timeout == "abort":
            raise ActionFailedError(timed_out["error"])
        if on_timeout == "review_gate":
            ctx.outcome = _merge_outcome(ctx.outcome, "review")
        return timed_out

    async def _fire_action_request(self, step: Dict[str, Any], ctx: ExecutionContext, config: Dict[str, Any]) -> Dict[str, Any]:
        view = self._context_view(ctx)
        resolved_secrets: set[str] = set()
        url = resolve_template(config.get("url", ""), view, resolved_secrets)
        method = str(config.get("method", "POST")).upper()
        headers = resolve_template(config.get("headers", {}), view, resolved_secrets)
        body = resolve_template(config.get("bodyTemplate", {}), view, resolved_secrets)
        # Clamp author-supplied tunables: unclamped, one action could be timeoutMs=600000 x
        # retries=10 -- a 100-minute request pinning a worker (and defeating the T1.3 budget).
        timeout = clamp_timeout_ms(config.get("timeoutMs", 5000)) / 1000
        retries = clamp_retries(config.get("retries", 0))
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

        # Circuit breaker: if this downstream has been failing, short-circuit NOW instead of
        # paying the full timeout+retry budget again (which is what drags tenant-wide latency up
        # when one connector is down). Keyed by connector id when known, else the URL host.
        breaker = None
        if circuit_breaker.enabled():
            breaker_key = str(step.get("ref_id") or step.get("ref") or config.get("connectorId") or _url_host(url))
            breaker = circuit_breaker.get_breaker(breaker_key)
            try:
                breaker.allow()
            except circuit_breaker.CircuitOpenError as open_err:
                short = {
                    "success": False,
                    "error": "circuit_open: {0}".format(open_err),
                    "circuit_open": True,
                    "attempts": 0,
                }
                ctx.action_results.append(short)
                on_failure = config.get("onFailure", "continue")
                if on_failure == "abort":
                    raise ActionFailedError("Action skipped — circuit open for {0}".format(breaker_key)) from open_err
                if on_failure == "review_gate":
                    ctx.outcome = _merge_outcome(ctx.outcome, "review")
                return short

        # SSRF guard on the REAL, secret-injecting path (the console guard never covered this).
        # Resolve + range-check the host, then pin the connection to the vetted IP so a DNS
        # rebind can't swing it to an internal address between check and connect.
        try:
            pinned = build_pinned_request(url)
        except EgressBlocked as blocked:
            last_error = "blocked_by_egress_policy: {0}".format(blocked)
            self._record_action_error(ctx, step, url, method, last_error)
            if breaker is not None:
                breaker.record_failure()
            failure_result = {"success": False, "error": last_error, "attempts": 0, "blocked": True}
            ctx.action_results.append(failure_result)
            on_failure = config.get("onFailure", "continue")
            if on_failure == "abort":
                raise ActionFailedError(last_error) from blocked
            if on_failure == "review_gate":
                ctx.outcome = _merge_outcome(ctx.outcome, "review")
            return failure_result

        request_headers = dict(headers or {})
        request_headers["Host"] = pinned.host_header
        request_extensions = {"sni_hostname": pinned.sni_hostname}
        connect_url = pinned.url

        # follow_redirects stays False: chasing a 3xx re-opens SSRF (redirect to metadata) and
        # would bypass the pinning we just did. A redirect surfaces as a non-2xx result instead.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for attempt in range(retries + 1):
                attempt_started = time.perf_counter()
                try:
                    if method == "GET":
                        response = await client.get(connect_url, headers=request_headers, extensions=request_extensions)
                    elif method == "PUT":
                        response = await client.put(connect_url, json=body, headers=request_headers, extensions=request_extensions)
                    elif method == "PATCH":
                        response = await client.patch(connect_url, json=body, headers=request_headers, extensions=request_extensions)
                    else:
                        response = await client.post(connect_url, json=body, headers=request_headers, extensions=request_extensions)
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
                        if breaker is not None:
                            breaker.record_success()
                        return result
                    last_error = "HTTP {0}".format(response.status_code)
                except Exception as error:
                    last_error = str(error)
                if attempt < retries:
                    await asyncio.sleep((int(config.get("retryBackoffMs", 1000)) * (attempt + 1)) / 1000)

        # All attempts failed -> count it against the breaker so a persistently-down target trips.
        if breaker is not None:
            breaker.record_failure()
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
        ref_id = str(step.get("ref_id") or step.get("ref") or "")
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
        # In simulation, a review gate is a terminal "review" outcome — never pause
        # or create a human task (that would persist rows and stall a backtest).
        if getattr(self, "_simulate", False):
            ctx.outcome = _merge_outcome(ctx.outcome, "review")
            return {"simulated": True, "outcome": "review"}
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
