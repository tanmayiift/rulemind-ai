import copy
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import yaml

# Max rule-tree nesting depth. Real trees are a handful deep; this guards against a
# pathological/adversarial tree recursing into a native stack overflow — the on-device
# Kotlin & Dart engines enforce the same cap (see RuleEvaluator).
MAX_RULE_TREE_DEPTH = 200


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
            condition = {
                "type": "condition",
                "id": node.get("id"),
                "variable": node.get("variable"),
                "operator": node.get("operator", "=="),
                "value": parse_rule_value(node.get("value")),
            }
            if node.get("value2") is not None:
                condition["value2"] = parse_rule_value(node.get("value2"))
            field_type = node.get("fieldType") or node.get("field_type")
            if field_type is not None:
                condition["fieldType"] = field_type
            children.append(condition)
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


def _coerce_number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes"}


def _as_option_list(expected: Any) -> List[Any]:
    if isinstance(expected, (list, tuple, set)):
        return list(expected)
    if expected is None:
        return []
    return [item.strip() for item in str(expected).split(",") if item.strip() != ""]


def _loose_equal(actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True
    actual_number = _coerce_number(actual)
    expected_number = _coerce_number(expected)
    if actual_number is not None and expected_number is not None:
        return actual_number == expected_number
    return str(actual) == str(expected)


def compare(
    actual: Any,
    operator: str,
    expected: Any,
    expected2: Any = None,
    field_type: Optional[str] = None,
) -> bool:
    """Evaluate a single condition.

    Mirrors the operator set of the TypeScript engine (packages/rule-engine)
    so that the server runtime, the browser preview, and the mobile SDKs all
    agree. Supports: == != > >= < <= between in not_in regex exists !exists.
    """
    # Existence checks intentionally run before the "missing value" guard.
    if operator == "exists":
        return actual is not None and actual != ""
    if operator == "!exists":
        return actual is None or actual == ""

    # Set membership. `expected` may be a list or a comma-separated string.
    if operator in {"in", "not_in"}:
        options = _as_option_list(expected)
        matched = any(_loose_equal(actual, option) for option in options)
        return matched if operator == "in" else not matched

    # Regular-expression match against the string form of the value.
    if operator == "regex":
        if actual is None:
            return False
        try:
            return re.search(str(expected), str(actual)) is not None
        except re.error:
            return False

    # Boolean-typed equality (only when the field is declared boolean).
    if (field_type or "").lower() == "boolean" and operator in {"==", "!="}:
        matched = _to_bool(actual) == _to_bool(expected)
        return matched if operator == "==" else not matched

    # Ordered comparisons and inclusive range operate on numbers.
    if operator in {">=", "<=", ">", "<", "between"}:
        actual_value = _coerce_number(actual)
        expected_value = _coerce_number(expected)
        if actual_value is None or expected_value is None:
            return False
        if operator == ">=":
            return actual_value >= expected_value
        if operator == "<=":
            return actual_value <= expected_value
        if operator == ">":
            return actual_value > expected_value
        if operator == "<":
            return actual_value < expected_value
        # between is inclusive and requires an upper bound (value2).
        upper_value = _coerce_number(expected2)
        if upper_value is None:
            return False
        return expected_value <= actual_value <= upper_value

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

    def evaluate_node(node: Dict[str, Any], inherited_logic: str = "AND", depth: int = 0) -> bool:
        if depth > MAX_RULE_TREE_DEPTH:
            raise ValueError(f"Rule tree nesting exceeds the maximum depth ({MAX_RULE_TREE_DEPTH})")
        node_type = node.get("type")
        if node_type == "condition":
            variable_id = str(node.get("variable", ""))
            actual = variable_values.get(variable_id)
            expected = parse_rule_value(node.get("value"))
            expected2 = parse_rule_value(node.get("value2")) if node.get("value2") is not None else None
            field_type = node.get("fieldType") or node.get("field_type")
            passed = compare(actual, str(node.get("operator", "==")), expected, expected2, field_type)
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
            result = not evaluate_node(child, "NOT", depth + 1)
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
            child_results = [evaluate_node(child, child_logic, depth + 1) for child in node.get("children", [])]
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


def _evaluate_formula(formula: str, context: Dict[str, Any]) -> Any:
    """Evaluate an Excel-style formula string in a sandboxed context."""
    try:
        from .excel_functions import EXCEL_FUNCTIONS
        safe_ns: Dict[str, Any] = dict(EXCEL_FUNCTIONS)
        safe_ns.update(context)
        safe_ns["__builtins__"] = {
            "abs": abs, "min": min, "max": max, "round": round,
            "int": int, "float": float, "str": str, "bool": bool,
            "len": len, "sum": sum, "True": True, "False": False, "None": None,
        }
        safe_ns["__builtins__"].update(EXCEL_FUNCTIONS)
        return eval(formula, {"__builtins__": safe_ns}, safe_ns)  # noqa: S307
    except Exception as exc:
        return {"error": str(exc)}


def evaluate_scorecard(scorecard: Dict[str, Any], variable_values: Dict[str, Any], variable_lookup: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    variable_lookup = variable_lookup or {}
    base_score = int(scorecard.get("base_score", 300))
    max_score = int(scorecard.get("max_score", 900))
    scoring_method = scorecard.get("scoring_method", "points")
    breakdown = []
    total_points = 0
    total_woe_score = 0.0
    total_iv = 0.0
    woe_details: List[Dict[str, Any]] = []

    for factor in scorecard.get("bins", []):
        variable_id = factor.get("variable_id")
        value = variable_values.get(variable_id, 0)
        weight = float(factor.get("weight", 1.0))
        coefficient = float(factor.get("coefficient", 0.0))

        # Find matching range
        active_range = None
        active_woe_range = None
        for candidate in factor.get("ranges", []):
            try:
                if float(candidate.get("min", 0)) <= float(value) <= float(candidate.get("max", 0)):
                    active_range = candidate
                    break
            except (TypeError, ValueError):
                continue

        # Check WoE ranges if present
        for woe_range in factor.get("woe_values", []) or []:
            try:
                if float(woe_range.get("min", 0)) <= float(value) <= float(woe_range.get("max", 0)):
                    active_woe_range = woe_range
                    break
            except (TypeError, ValueError):
                continue

        # Points-based scoring (original + weight support)
        points = int(active_range.get("points", 0)) if active_range else 0
        weighted_points = int(points * weight)
        total_points += weighted_points

        # WoE scoring
        woe_value = float(active_woe_range.get("woe", 0)) if active_woe_range else 0.0
        iv_contribution = float(active_woe_range.get("iv", 0)) if active_woe_range else 0.0
        total_iv += iv_contribution
        total_woe_score += coefficient * woe_value

        if active_woe_range:
            woe_details.append({
                "variable_id": variable_id,
                "variable_name": variable_lookup.get(variable_id, {}).get("name", variable_id),
                "value": value,
                "woe": woe_value,
                "iv": iv_contribution,
                "coefficient": coefficient,
                "contribution": coefficient * woe_value,
                "event_rate": active_woe_range.get("event_rate"),
                "non_event_rate": active_woe_range.get("non_event_rate"),
            })

        breakdown.append(
            {
                "variable_id": variable_id,
                "variable_name": variable_lookup.get(variable_id, {}).get("name", variable_id),
                "source_id": variable_lookup.get(variable_id, {}).get("source_id"),
                "value": value,
                "active_range": active_range,
                "points": points,
                "weighted_points": weighted_points,
                "weight": weight,
                "woe": woe_value,
                "coefficient": coefficient,
            }
        )

    # Compute score based on method
    if scoring_method == "woe":
        # WoE-based scoring: score = target_score + (PDO / ln(2)) × (intercept + Σ(coefficient × WoE))
        import math as _math
        intercept = float(scorecard.get("intercept", 0))
        pdo = float(scorecard.get("pdo", 20))
        target_score = float(scorecard.get("target_score", 600))
        target_odds = float(scorecard.get("target_odds", 50))
        factor_val = pdo / _math.log(2) if pdo > 0 else 1
        offset = target_score - factor_val * _math.log(target_odds) if target_odds > 0 else target_score
        raw_score = offset + factor_val * (intercept + total_woe_score)
        score = max(0, min(max_score, round(raw_score)))
    elif scoring_method == "formula":
        # Custom formula evaluation
        formula = scorecard.get("formula", f"base_score + total_points")
        formula_context = {
            "base_score": base_score, "max_score": max_score,
            "total_points": total_points, "total_woe_score": total_woe_score,
            "variables": variable_values,
        }
        for entry in breakdown:
            formula_context[entry["variable_id"]] = entry["value"]
        result = _evaluate_formula(formula, formula_context)
        score = max(0, min(max_score, int(result) if isinstance(result, (int, float)) else base_score))
    else:
        # Default points-based scoring (backward compatible)
        score = max(0, min(max_score, base_score + total_points))

    # Compute metric formulas
    metrics_results: Dict[str, Any] = {}
    for metric in scorecard.get("metrics", []) or []:
        metric_context = {
            "score": score, "base_score": base_score, "max_score": max_score,
            "total_points": total_points, "total_woe_score": total_woe_score,
            "total_iv": total_iv, "variables": variable_values,
        }
        for entry in breakdown:
            metric_context[entry["variable_id"]] = entry["value"]
        metrics_results[metric.get("name", "unnamed")] = {
            "value": _evaluate_formula(metric.get("formula", "0"), metric_context),
            "unit": metric.get("unit", ""),
            "category": metric.get("category", "financial"),
        }

    result = {
        "score": score,
        "base_score": base_score,
        "max_score": max_score,
        "scoring_method": scoring_method,
        "breakdown": breakdown,
    }

    if woe_details:
        result["woe_details"] = woe_details
        result["information_value"] = total_iv

    if metrics_results:
        result["metrics"] = metrics_results

    return result


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


def _redact_keys() -> set:
    """The PII field names redacted from the stored decision payload. The built-in set covers
    common identity fields; operators extend it (any domain: patient_id, ssn, card_no, …) via
    RULEMIND_PII_REDACT_KEYS (comma-separated), case-insensitive."""
    extra = {
        item.strip().lower()
        for item in (os.getenv("RULEMIND_PII_REDACT_KEYS", "") or "").split(",")
        if item.strip()
    }
    return {key.lower() for key in REDACTED_KEYS} | extra


def redact_payload(value: Any) -> Any:
    keys = _redact_keys()
    return _redact_with(value, keys)


def _redact_with(value: Any, keys: set) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if str(key).lower() in keys:
                redacted[key] = "***"
            else:
                redacted[key] = _redact_with(item, keys)
        return redacted
    if isinstance(value, list):
        return [_redact_with(item, keys) for item in value]
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


def export_csv_bundle(config: Dict[str, Any]) -> str:
    """Export configuration as CSV with sections per entity type."""
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)

    # --- Connectors ---
    writer.writerow(["## CONNECTORS"])
    writer.writerow(["id", "name", "icon", "color", "description", "is_active", "schema_paths", "sample_payload", "config"])
    for c in config.get("connectors", []):
        writer.writerow([
            c.get("id", ""), c.get("name", ""), c.get("icon", ""), c.get("color", ""),
            c.get("description", ""), c.get("is_active", True),
            json.dumps(c.get("schema_paths", [])), json.dumps(c.get("sample_payload", {})),
            json.dumps(c.get("config", {})),
        ])
    writer.writerow([])

    # --- Variables ---
    writer.writerow(["## VARIABLES"])
    writer.writerow(["id", "name", "category", "source_id", "code", "description", "status", "version"])
    for v in config.get("variables", []):
        writer.writerow([
            v.get("id", ""), v.get("name", ""), v.get("category", ""),
            v.get("source_id", ""), v.get("code", ""), v.get("description", ""),
            v.get("status", "dev"), v.get("version", 1),
        ])
    writer.writerow([])

    # --- Rules ---
    writer.writerow(["## RULES"])
    writer.writerow(["id", "name", "rule_format", "nodes", "tree", "expression", "status", "version"])
    for r in config.get("rules", []):
        writer.writerow([
            r.get("id", ""), r.get("name", ""), r.get("rule_format", "v1"),
            json.dumps(r.get("nodes", [])), json.dumps(r.get("tree", {})),
            r.get("expression", ""), r.get("status", "dev"), r.get("version", 1),
        ])
    writer.writerow([])

    # --- Scorecards ---
    writer.writerow(["## SCORECARDS"])
    writer.writerow(["id", "name", "base_score", "max_score", "scoring_method", "intercept", "pdo",
                      "target_score", "target_odds", "bins", "metrics", "status", "version"])
    for s in config.get("scorecards", []):
        writer.writerow([
            s.get("id", ""), s.get("name", ""), s.get("base_score", 300),
            s.get("max_score", 900), s.get("scoring_method", "points"),
            s.get("intercept", 0), s.get("pdo", 20),
            s.get("target_score", 600), s.get("target_odds", 50),
            json.dumps(s.get("bins", [])), json.dumps(s.get("metrics", [])),
            s.get("status", "dev"), s.get("version", 1),
        ])
    writer.writerow([])

    # --- Policies ---
    writer.writerow(["## POLICIES"])
    writer.writerow(["id", "name", "trigger", "steps", "defaultOutcome", "status", "version"])
    for p in config.get("policies", []):
        writer.writerow([
            p.get("id", ""), p.get("name", ""), json.dumps(p.get("trigger", {})),
            json.dumps(p.get("steps", [])), p.get("defaultOutcome", "review"),
            p.get("status", "dev"), p.get("version", 1),
        ])

    return output.getvalue()


def export_bundle(config: Dict[str, Any], format_name: str) -> str:
    format_name = format_name.lower()
    if format_name == "json":
        return json.dumps(config, indent=2, sort_keys=True)
    if format_name == "yaml":
        return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    if format_name == "python":
        return export_python_bundle(config)
    if format_name == "csv":
        return export_csv_bundle(config)
    raise ValueError("Unsupported export format.")


def find_by_id(items: Iterable[Dict[str, Any]], entity_id: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if item.get("id") == entity_id:
            return item
    return None
