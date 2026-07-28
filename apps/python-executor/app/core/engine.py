"""Pure decision core — see app/core/__init__.py for the contract.

`decide(bundle, payload, context)` evaluates a compiled bundle against a payload
and returns a decision with full explainability. No I/O, no DB, no globals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Pure primitives only. Do NOT add storage/db imports here.
from ..experiments import apply_experiment_overrides, assign_variant
from ..logic import evaluate_rule_definition, evaluate_scorecard

# Outcome precedence — mirrors PolicyExecutor._merge_outcome, re-declared here so
# the core has zero dependency on the (storage-coupled) executor module.
_OUTCOME_PRECEDENCE = {"pending": 0, "pass": 1, "approve": 2, "review": 3, "reject": 4}

# Step types the pure core evaluates. Side-effectful steps (connector fetch,
# outbound action, human review gate) are the host's responsibility and are
# recorded as "deferred" rather than executed.
_PURE_STEP_TYPES = {"rule", "scorecard", "outcome"}
_DEFERRED_STEP_TYPES = {"connector", "action", "review_gate", "transform", "model"}


class InputValidationError(ValueError):
    """Raised when a payload fails the bundle/policy input schema."""

    def __init__(self, errors: List[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class DecisionResult:
    policy_id: str
    outcome: str
    score: Optional[float] = None
    variables: Dict[str, Any] = field(default_factory=dict)
    rule_results: List[Dict[str, Any]] = field(default_factory=list)
    scorecard_result: Optional[Dict[str, Any]] = None
    trace: List[Dict[str, Any]] = field(default_factory=list)
    experiment_id: Optional[str] = None
    experiment_variant: Optional[str] = None
    input_valid: bool = True
    validation_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "outcome": self.outcome,
            "score": self.score,
            "variables": self.variables,
            "rule_results": self.rule_results,
            "scorecard_result": self.scorecard_result,
            "trace": self.trace,
            "experiment_id": self.experiment_id,
            "experiment_variant": self.experiment_variant,
            "input_valid": self.input_valid,
            "validation_errors": self.validation_errors,
        }


def _merge_outcome(current: Optional[str], candidate: Optional[str]) -> str:
    current_value = str(current or "pending")
    candidate_value = str(candidate or current_value)
    current_rank = _OUTCOME_PRECEDENCE.get(current_value, 0)
    candidate_rank = _OUTCOME_PRECEDENCE.get(candidate_value, 0)
    if candidate_rank > current_rank:
        return candidate_value
    if candidate_rank < current_rank:
        return current_value
    if current_value == "pass" and candidate_value == "approve":
        return candidate_value
    return current_value


def _coerce_number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_input(payload: Dict[str, Any], input_schema: Optional[Dict[str, Any]]) -> List[str]:
    """Validate a payload against an input schema.

    Schema shape (all keys optional per field):
        {"<field>": {"required": bool, "type": "number|string|boolean",
                      "min": n, "max": n, "pattern": "regex", "enum": [...]}}

    Returns a list of human-readable errors (empty when valid). Enforcing this in
    the core means every runtime (server, edge, SDK host) gets the same guardrail.
    """
    errors: List[str] = []
    if not input_schema or not isinstance(input_schema, dict):
        return errors
    fields = input_schema.get("fields", input_schema)
    if not isinstance(fields, dict):
        return errors
    for name, rule in fields.items():
        if not isinstance(rule, dict):
            continue
        present = isinstance(payload, dict) and name in payload and payload[name] is not None
        if rule.get("required") and not present:
            errors.append(f"'{name}' is required")
            continue
        if not present:
            continue
        value = payload[name]
        expected_type = rule.get("type")
        if expected_type == "number":
            number = _coerce_number(value)
            if number is None:
                errors.append(f"'{name}' must be a number")
                continue
            if rule.get("min") is not None and number < float(rule["min"]):
                errors.append(f"'{name}' must be >= {rule['min']}")
            if rule.get("max") is not None and number > float(rule["max"]):
                errors.append(f"'{name}' must be <= {rule['max']}")
        elif expected_type == "boolean":
            if not isinstance(value, bool) and str(value).lower() not in {"true", "false"}:
                errors.append(f"'{name}' must be a boolean")
        elif expected_type == "string":
            text = str(value)
            pattern = rule.get("pattern")
            if pattern:
                try:
                    if not re.search(str(pattern), text):
                        errors.append(rule.get("patternMessage") or f"'{name}' does not match required format")
                except re.error:
                    pass
        enum = rule.get("enum")
        if enum and value not in enum and str(value) not in [str(item) for item in enum]:
            errors.append(f"'{name}' must be one of {enum}")
    return errors


def _select_policy(bundle: Dict[str, Any], policy_id: Optional[str]) -> Dict[str, Any]:
    if "policy" in bundle and isinstance(bundle["policy"], dict):
        return bundle["policy"]
    policies = bundle.get("policies") or {}
    if isinstance(policies, list):
        policies = {item.get("id"): item for item in policies if isinstance(item, dict)}
    if policy_id and policy_id in policies:
        return policies[policy_id]
    if len(policies) == 1:
        return next(iter(policies.values()))
    raise ValueError(f"policy not found in bundle: {policy_id}")


def _as_map(collection: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(collection, dict):
        return collection
    if isinstance(collection, list):
        return {item.get("id"): item for item in collection if isinstance(item, dict) and item.get("id")}
    return {}


def _resolve_experiment(
    bundle: Dict[str, Any], policy_id: str, subject_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Pure experiment assignment over experiments embedded in the bundle."""
    if not subject_id:
        return None
    for experiment in bundle.get("experiments", []) or []:
        if experiment.get("status") != "running":
            continue
        if experiment.get("target_policy_id") not in (None, policy_id):
            continue
        variant = assign_variant(subject_id, experiment["id"], experiment.get("variants", []))
        if not variant:
            continue
        return {
            "experiment": {"id": experiment["id"], "name": experiment.get("name")},
            "variant": variant,
        }
    return None


def decide(
    bundle: Dict[str, Any],
    payload: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate `payload` against `bundle` and return a decision dict.

    bundle: {"policy"|"policies", "rules", "scorecards", "variables"?,
             "experiments"?, "input_schema"?}
    context: {"policy_id"?, "subject_id"?, "variables"?, "strict_validation"?}
    """
    context = context or {}
    policy = _select_policy(bundle, context.get("policy_id"))
    policy_id = policy.get("id", context.get("policy_id", "policy"))

    # Variables: use pre-computed values from context, else treat the payload
    # itself as the resolved feature map (the common decisioning case).
    variables: Dict[str, Any] = dict(context.get("variables") or payload or {})
    variable_lookup = _as_map(bundle.get("variables"))

    result = DecisionResult(policy_id=policy_id, outcome="pending", variables=variables)

    # Input validation (core-enforced guardrail).
    input_schema = policy.get("input_schema") or bundle.get("input_schema")
    errors = validate_input(payload, input_schema)
    if errors:
        result.input_valid = False
        result.validation_errors = errors
        if context.get("strict_validation", True):
            result.outcome = "reject"
            result.trace.append({"step": {"type": "input_validation"}, "error": errors})
            return result.to_dict()

    # Experiment assignment + override injection (pure).
    rules = _as_map(bundle.get("rules"))
    assignment = _resolve_experiment(bundle, policy_id, context.get("subject_id"))
    if assignment:
        result.experiment_id = assignment["experiment"]["id"]
        result.experiment_variant = assignment["variant"].get("id")
        rules = apply_experiment_overrides(rules, assignment)
    scorecards = _as_map(bundle.get("scorecards"))

    for index, step in enumerate(policy.get("steps", [])):
        step_type = step.get("type")
        ref_id = step.get("ref_id") or step.get("ref")
        if step_type == "rule":
            rule = rules.get(ref_id)
            if not rule:
                result.trace.append({"step": step, "error": f"unknown rule {ref_id}"})
                continue
            evaluation = evaluate_rule_definition(rule, variables, variable_lookup)
            evaluation = {**evaluation, "rule_id": ref_id, "ruleId": ref_id}
            result.rule_results.append(evaluation)
            result.outcome = _merge_outcome(result.outcome, evaluation.get("outcome"))
            result.trace.append({"step": step, "result": evaluation})
        elif step_type == "scorecard":
            scorecard = scorecards.get(ref_id)
            if not scorecard:
                result.trace.append({"step": step, "error": f"unknown scorecard {ref_id}"})
                continue
            scored = evaluate_scorecard(scorecard, variables, variable_lookup)
            result.scorecard_result = scored
            result.score = scored.get("score")
            result.trace.append({"step": step, "result": scored})
        elif step_type == "outcome":
            candidate = ref_id or (step.get("config") or {}).get("outcome") or step.get("label") or "review"
            result.outcome = _merge_outcome(result.outcome, candidate)
            result.trace.append({"step": step, "result": {"outcome": result.outcome}})
        elif step_type in _DEFERRED_STEP_TYPES:
            # The pure core does not perform I/O; the host resolves these.
            result.trace.append({"step": step, "deferred": True})
        else:
            result.trace.append({"step": step, "error": f"unknown step type {step_type}"})

    if result.outcome == "pending":
        result.outcome = "review"
    return result.to_dict()
