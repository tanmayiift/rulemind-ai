import copy
import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import yaml


REDACTED_KEYS = {
    "name",
    "email",
    "phone",
    "telephoneNumber",
    "idNumber",
    "pan",
    "aadhaar",
    "address",
    "line1",
    "line2",
}


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def parse_rule_value(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        try:
            if "." in lowered:
                return float(lowered)
            return int(lowered)
        except ValueError:
            return value
    return value


def variable_label(variable_id: str, variable_lookup: Dict[str, Dict[str, Any]]) -> str:
    return variable_lookup.get(variable_id, {}).get("name", variable_id)


def nodes_to_tree(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not nodes:
        return {"type": "group", "logic": "AND", "children": [], "onPass": "approve", "onFail": "reject"}

    logic = "AND"
    outcome = "approve"
    children: List[Dict[str, Any]] = []
    for node in nodes:
        node_type = node.get("type")
        if node_type == "condition":
            children.append(
                {
                    "type": "condition",
                    "id": node.get("id"),
                    "variable": node.get("variable"),
                    "operator": node.get("operator", "=="),
                    "value": parse_rule_value(node.get("value")),
                }
            )
        elif node_type == "or":
            logic = "OR"
        elif node_type == "and":
            logic = "AND"
        elif node_type in {"approve", "review", "reject"}:
            outcome = str(node_type)
    return {"type": "group", "logic": logic, "children": children, "onPass": outcome, "onFail": "reject"}


def flatten_tree_to_nodes(tree: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not tree:
        return []

    nodes: List[Dict[str, Any]] = []
    counter = {"value": 1}

    def next_id(prefix: str) -> str:
        current = f"{prefix}_{counter['value']}"
        counter["value"] += 1
        return current

    def walk(node: Dict[str, Any], inherited_logic: str = "AND") -> None:
        node_type = node.get("type")
        if node_type == "condition":
            if nodes and nodes[-1]["type"] == "condition":
                nodes.append({"id": next_id("logic"), "type": inherited_logic.lower(), "label": inherited_logic})
            nodes.append(
                {
                    "id": node.get("id") or next_id("condition"),
                    "type": "condition",
                    "variable": node.get("variable"),
                    "operator": node.get("operator", "=="),
                    "value": str(node.get("value", "")),
                    "label": "Condition",
                }
            )
            return

        if node_type == "not":
            child = copy.deepcopy(node.get("child") or {})
            if child.get("type") == "condition":
                child["operator"] = "==" if child.get("operator") == "!=" else "!="
            walk(child, inherited_logic)
            return

        if node_type == "group":
            logic = str(node.get("logic", inherited_logic or "AND")).upper()
            for child in node.get("children", []):
                walk(child, logic)

    walk(tree, str(tree.get("logic", "AND")).upper())
    on_pass = str(tree.get("onPass", "approve"))
    if on_pass in {"approve", "review", "reject"}:
      nodes.append({"id": next_id("outcome"), "type": on_pass, "label": on_pass.title()})
    return nodes


def expression_from_tree(node: Dict[str, Any], variable_lookup: Dict[str, Dict[str, Any]], depth: int = 0) -> str:
    node_type = node.get("type")
    if node_type == "condition":
        return "{0} {1} {2}".format(
            variable_label(str(node.get("variable", "")), variable_lookup),
            node.get("operator", "=="),
            node.get("value", ""),
        )
    if node_type == "not":
        child = node.get("child") or {}
        return "NOT ({0})".format(expression_from_tree(child, variable_lookup, depth + 1))
    if node_type == "group":
        children = node.get("children", [])
        if not children:
            return "()"
        joined = (" {0} ".format(str(node.get("logic", "AND")).upper())).join(
            expression_from_tree(child, variable_lookup, depth + 1) for child in children
        )
        wrapped = "({0})".format(joined)
        if depth > 0:
            return wrapped
        on_pass = str(node.get("onPass", "approve")).upper()
        on_fail = str(node.get("onFail", "reject")).upper()
        return "IF {0} \u2192 {1} ELSE \u2192 {2}".format(wrapped, on_pass, on_fail)
    return "?"


def generate_rule_expression(nodes: List[Dict[str, Any]], variable_lookup: Dict[str, Dict[str, Any]]) -> str:
    if not nodes:
        return "// Add nodes"
    return expression_from_tree(nodes_to_tree(nodes), variable_lookup)


def generate_rule_expression_definition(rule: Dict[str, Any], variable_lookup: Dict[str, Dict[str, Any]]) -> str:
    tree = rule.get("tree")
    if isinstance(tree, dict) and tree:
        return expression_from_tree(tree, variable_lookup)
    return generate_rule_expression(rule.get("nodes", []), variable_lookup)


def compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator in {">=", "<=", ">", "<"}:
        try:
            actual_value = float(actual)
            expected_value = float(expected)
        except (TypeError, ValueError):
            return False
        if operator == ">=":
            return actual_value >= expected_value
        if operator == "<=":
            return actual_value <= expected_value
        if operator == ">":
            return actual_value > expected_value
        return actual_value < expected_value

    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    return False


def evaluate_rule_tree(
    tree: Dict[str, Any],
    variable_values: Dict[str, Any],
    variable_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    variable_lookup = variable_lookup or {}
    condition_results: List[Dict[str, Any]] = []
    group_results: List[Dict[str, Any]] = []

    def evaluate_node(node: Dict[str, Any], inherited_logic: str = "AND") -> bool:
        node_type = node.get("type")
        if node_type == "condition":
            variable_id = str(node.get("variable", ""))
            actual = variable_values.get(variable_id)
            expected = parse_rule_value(node.get("value"))
            passed = compare(actual, str(node.get("operator", "==")), expected)
            condition_results.append(
                {
                    "variable_id": variable_id,
                    "variable_name": variable_label(variable_id, variable_lookup),
                    "source_id": variable_lookup.get(variable_id, {}).get("source_id"),
                    "operator": node.get("operator", "=="),
                    "threshold": expected,
                    "value": actual,
                    "passed": passed,
                    "group": inherited_logic,
                }
            )
            return passed

        if node_type == "not":
            child = node.get("child") or {}
            result = not evaluate_node(child, "NOT")
            group_results.append(
                {
                    "id": node.get("id", "not"),
                    "logic": "NOT",
                    "passed": result,
                    "childCount": 1,
                }
            )
            return result

        if node_type == "group":
            child_logic = str(node.get("logic", "AND")).upper()
            child_results = [evaluate_node(child, child_logic) for child in node.get("children", [])]
            passed = all(child_results) if child_logic == "AND" else any(child_results) if child_results else False
            group_results.append(
                {
                    "id": node.get("id", "group"),
                    "logic": child_logic,
                    "passed": passed,
                    "childCount": len(child_results),
                }
            )
            return passed

        return False

    overall = evaluate_node(tree, str(tree.get("logic", "AND")).upper())
    return {
        "conditions": condition_results,
        "groupResults": group_results,
        "passed": overall,
        "outcome": str(tree.get("onPass", "approve")) if overall else str(tree.get("onFail", "reject")),
        "logic": str(tree.get("logic", "AND")).upper(),
    }


def evaluate_rule_nodes(nodes: List[Dict[str, Any]], variable_values: Dict[str, Any], variable_lookup: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    return evaluate_rule_tree(nodes_to_tree(nodes), variable_values, variable_lookup)


def evaluate_rule_definition(rule: Dict[str, Any], variable_values: Dict[str, Any], variable_lookup: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    tree = rule.get("tree")
    if isinstance(tree, dict) and tree:
        return evaluate_rule_tree(tree, variable_values, variable_lookup)
    return evaluate_rule_nodes(rule.get("nodes", []), variable_values, variable_lookup)


def evaluate_scorecard(scorecard: Dict[str, Any], variable_values: Dict[str, Any], variable_lookup: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    variable_lookup = variable_lookup or {}
    base_score = int(scorecard.get("base_score", 300))
    max_score = int(scorecard.get("max_score", 900))
    breakdown = []
    total_points = 0

    for factor in scorecard.get("bins", []):
        variable_id = factor.get("variable_id")
        value = variable_values.get(variable_id, 0)
        active_range = None
        for candidate in factor.get("ranges", []):
            if float(candidate.get("min", 0)) <= float(value) <= float(candidate.get("max", 0)):
                active_range = candidate
                break
        points = int(active_range.get("points", 0)) if active_range else 0
        total_points += points
        breakdown.append(
            {
                "variable_id": variable_id,
                "variable_name": variable_lookup.get(variable_id, {}).get("name", variable_id),
                "source_id": variable_lookup.get(variable_id, {}).get("source_id"),
                "value": value,
                "active_range": active_range,
                "points": points,
            }
        )

    score = max(0, min(max_score, base_score + total_points))
    return {"score": score, "base_score": base_score, "max_score": max_score, "breakdown": breakdown}


def execute_policy(policy: Dict[str, Any], payload_by_source: Dict[str, Any], variables_map: Dict[str, Dict[str, Any]], rules_map: Dict[str, Dict[str, Any]], scorecards_map: Dict[str, Dict[str, Any]], variable_values: Dict[str, Any]) -> Dict[str, Any]:
    trace = []
    current_score = None
    current_outcome = None

    for step in policy.get("steps", []):
        step_type = step.get("type")
        ref_id = step.get("ref_id")
        label = step.get("label")

        if step_type == "connector":
            payload = payload_by_source.get(ref_id, {})
            trace.append(
                {
                    "type": "connector",
                    "label": label,
                    "ref_id": ref_id,
                    "status": "ok",
                    "payload_keys": sorted(list(payload.keys())),
                }
            )
            continue

        if step_type == "rule" and ref_id in rules_map:
            result = evaluate_rule_definition(rules_map[ref_id], variable_values, variables_map)
            trace.append({"type": "rule", "label": label, "ref_id": ref_id, "result": result})
            if result.get("passed"):
                current_outcome = result["outcome"]
                if result["outcome"] in {"reject", "review"}:
                    break
            continue

        if step_type == "scorecard" and ref_id in scorecards_map:
            current_score = evaluate_scorecard(scorecards_map[ref_id], variable_values, variables_map)
            trace.append({"type": "scorecard", "label": label, "ref_id": ref_id, "result": current_score})
            continue

        if step_type == "outcome":
            current_outcome = ref_id or label or "approve"
            trace.append({"type": "outcome", "label": label, "ref_id": ref_id, "result": current_outcome})

    return {
        "policy_id": policy.get("id"),
        "outcome": current_outcome or "approve",
        "scorecard_result": current_score,
        "trace": trace,
    }


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in REDACTED_KEYS:
                redacted[key] = "***"
            else:
                redacted[key] = redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def export_python_bundle(config: Dict[str, Any]) -> str:
    payload = json.dumps(config, indent=2, sort_keys=True)
    return (
        "# Auto-generated RuleMind export\n"
        "CONFIG = "
        + payload
        + "\n\n"
        "def get_config():\n"
        "    return CONFIG\n"
    )


def export_bundle(config: Dict[str, Any], format_name: str) -> str:
    format_name = format_name.lower()
    if format_name == "json":
        return json.dumps(config, indent=2, sort_keys=True)
    if format_name == "yaml":
        return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    if format_name == "python":
        return export_python_bundle(config)
    raise ValueError("Unsupported export format.")


def find_by_id(items: Iterable[Dict[str, Any]], entity_id: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if item.get("id") == entity_id:
            return item
    return None
