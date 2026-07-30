"""Decision tables — pure, DB-free evaluation and an authoring-time optimiser.

A decision table is spreadsheet-style sugar over MECE-checked rules: input
columns bind to variables, each row states a condition per input and the
output(s) to emit when the row matches. This module has two jobs:

* ``evaluate_decision_table`` — run a table against a set of variable values
  honouring the hit policy (first / priority / unique / collect), returning a
  full per-cell trace so a decision is explainable.
* ``analyze_decision_table`` — the *optimiser*: detect conflicts (overlapping
  rows), gaps (uncovered input space), unreachable/shadowed rows, and invalid
  cell/output values, so the author can fix a table before it ships.

Overlap/gap detection reuses the tested interval + value-set algebra in
``mece.analyze_mece`` (each row compiles to a shorthand rule). The cell-level
"covers" logic used for shadow detection is self-contained and deliberately
conservative: anything it cannot reason about exactly (regex, negations on
numerics) is marked opaque and skipped rather than reported wrongly.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .logic import compare, parse_rule_value
from .mece import analyze_mece

# Operators that require a comparison value; "any"/None/"" is a wildcard cell.
WILDCARD_OPERATORS = {"", "any", "*", "-", None}
VALID_OUTCOMES = {"approve", "review", "reject", "pass", "fail"}
HIT_POLICIES = {"first", "priority", "unique", "collect"}


def _is_wildcard(cell: Optional[Dict[str, Any]]) -> bool:
    if not cell:
        return True
    op = cell.get("operator")
    if op in WILDCARD_OPERATORS:
        return True
    # A present operator with no value (other than existence checks) is a no-op.
    if op not in {"exists", "!exists"} and cell.get("value") in (None, ""):
        return True
    return False


def _input_index(table: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(inp.get("id")): inp for inp in table.get("inputs", []) if inp.get("id") is not None}


def _outcome_output_id(table: Dict[str, Any]) -> Optional[str]:
    for out in table.get("outputs", []):
        if str(out.get("type", "")).lower() == "outcome":
            return str(out.get("id"))
    return None


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def _evaluate_row(
    row: Dict[str, Any],
    inputs: Dict[str, Dict[str, Any]],
    variable_values: Dict[str, Any],
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Return (matched, cell_trace). A row matches when every non-wildcard cell
    matches (AND across input columns)."""
    cells = row.get("cells", {}) or {}
    trace: List[Dict[str, Any]] = []
    matched = True
    for input_id, inp in inputs.items():
        cell = cells.get(input_id)
        variable_id = str(inp.get("variable_id") or inp.get("variable") or input_id)
        actual = variable_values.get(variable_id)
        if _is_wildcard(cell):
            trace.append({"input_id": input_id, "variable_id": variable_id, "operator": "any",
                          "value": actual, "passed": True, "wildcard": True})
            continue
        operator = str(cell.get("operator", "=="))
        expected = parse_rule_value(cell.get("value"))
        expected2 = parse_rule_value(cell.get("value2")) if cell.get("value2") not in (None, "") else None
        field_type = inp.get("field_type") or inp.get("fieldType")
        passed = compare(actual, operator, expected, expected2, field_type)
        trace.append({"input_id": input_id, "variable_id": variable_id, "operator": operator,
                      "threshold": expected, "value": actual, "passed": passed, "wildcard": False})
        if not passed:
            matched = False
    return matched, trace


def evaluate_decision_table(
    table: Dict[str, Any],
    variable_values: Dict[str, Any],
    variable_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Evaluate a decision table against variable values.

    Returns outputs merged per hit policy, the resolved outcome (if the table
    has an outcome-typed output column), the matched row ids, and a full trace.
    """
    inputs = _input_index(table)
    rows = table.get("rows", []) or []
    hit_policy = str(table.get("hit_policy", "first")).lower()
    if hit_policy not in HIT_POLICIES:
        hit_policy = "first"
    outcome_output_id = _outcome_output_id(table)

    row_traces: List[Dict[str, Any]] = []
    matched_rows: List[Dict[str, Any]] = []
    for row in rows:
        matched, cell_trace = _evaluate_row(row, inputs, variable_values)
        row_traces.append({"row_id": row.get("id"), "cells": cell_trace, "matched": matched})
        if matched:
            matched_rows.append(row)

    # Order rows for priority hit policy (highest priority first, stable otherwise).
    def _priority(r: Dict[str, Any]) -> float:
        try:
            return float(r.get("priority"))
        except (TypeError, ValueError):
            return 0.0

    outputs: Dict[str, Any] = {}
    winning_row_id: Optional[str] = None

    if matched_rows:
        if hit_policy == "collect":
            for out in table.get("outputs", []):
                oid = str(out.get("id"))
                outputs[oid] = [r.get("outputs", {}).get(oid) for r in matched_rows]
            winning_row_id = [r.get("id") for r in matched_rows]
        else:
            if hit_policy == "priority":
                winner = max(matched_rows, key=_priority)
            else:  # first / unique both take the first match at eval time
                winner = matched_rows[0]
            outputs = dict(winner.get("outputs", {}) or {})
            winning_row_id = winner.get("id")
    else:
        # No row matched — fall back to the table default if present.
        default_row = table.get("default_row") or {}
        outputs = dict(default_row.get("outputs", {}) or {})
        winning_row_id = "default" if outputs else None

    outcome = None
    if outcome_output_id is not None:
        raw = outputs.get(outcome_output_id)
        outcome = raw[0] if isinstance(raw, list) and raw else raw
        if isinstance(outcome, str):
            outcome = outcome.lower()

    return {
        "outputs": outputs,
        "outcome": outcome,
        "matched_row_ids": [r.get("id") for r in matched_rows],
        "winning_row_id": winning_row_id,
        "hit_policy": hit_policy,
        "trace": row_traces,
        "ambiguous": hit_policy == "unique" and len(matched_rows) > 1,
    }


# --------------------------------------------------------------------------- #
# Optimiser — cell "covers" algebra for shadow / unreachable detection
# --------------------------------------------------------------------------- #
# A cell region is one of:
#   ("all",)                       — wildcard, covers everything
#   ("interval", lo, hi, lo_inc, hi_inc)
#   ("set", frozenset(values))     — categorical / boolean equality or `in`
#   ("opaque",)                    — cannot be reasoned about (regex, !=, not_in, exists)
def _cell_region(cell: Optional[Dict[str, Any]], field_type: Optional[str]) -> Tuple:
    if _is_wildcard(cell):
        return ("all",)
    operator = str(cell.get("operator", "=="))
    value = cell.get("value")
    value2 = cell.get("value2")

    def _num(v: Any) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    if operator in {">", ">=", "<", "<=", "between", "=="} and (field_type or "").lower() in {"number", "integer", "float", ""}:
        v = _num(value)
        if operator == "between":
            v2 = _num(value2)
            if v is not None and v2 is not None:
                return ("interval", min(v, v2), max(v, v2), True, True)
        elif v is not None:
            if operator == "==":
                return ("interval", v, v, True, True)
            if operator == ">=":
                return ("interval", v, math.inf, True, False)
            if operator == ">":
                return ("interval", v, math.inf, False, False)
            if operator == "<=":
                return ("interval", -math.inf, v, False, True)
            if operator == "<":
                return ("interval", -math.inf, v, False, False)
    if operator == "==":
        return ("set", frozenset({str(parse_rule_value(value))}))
    if operator == "in":
        vals = [s.strip() for s in str(value).split(",") if s.strip() != ""] if not isinstance(value, (list, tuple, set)) else list(value)
        return ("set", frozenset(str(parse_rule_value(v)) for v in vals))
    return ("opaque",)


def _interval_covers(a: Tuple, b: Tuple) -> bool:
    _, alo, ahi, alo_inc, ahi_inc = a
    _, blo, bhi, blo_inc, bhi_inc = b
    lo_ok = alo < blo or (alo == blo and (alo_inc or not blo_inc))
    hi_ok = ahi > bhi or (ahi == bhi and (ahi_inc or not bhi_inc))
    return lo_ok and hi_ok


def _cell_covers(a: Tuple, b: Tuple) -> bool:
    """Does region a fully contain region b (so b adds nothing beyond a)?"""
    if a[0] == "all":
        return True
    if a[0] == "opaque" or b[0] == "opaque" or b[0] == "all":
        return False
    if a[0] == "interval" and b[0] == "interval":
        return _interval_covers(a, b)
    if a[0] == "set" and b[0] == "set":
        return b[1].issubset(a[1])
    return False


def _row_covers(inputs: Dict[str, Dict[str, Any]], earlier: Dict[str, Any], later: Dict[str, Any]) -> bool:
    """True if `earlier` matches every input region that `later` matches — i.e.
    `later` is unreachable behind `earlier` under first/priority hit policies.
    Conservative: returns False if any column is opaque."""
    ecells = earlier.get("cells", {}) or {}
    lcells = later.get("cells", {}) or {}
    for input_id, inp in inputs.items():
        ft = inp.get("field_type") or inp.get("fieldType")
        a = _cell_region(ecells.get(input_id), ft)
        b = _cell_region(lcells.get(input_id), ft)
        if a[0] == "opaque" or b[0] == "opaque":
            return False
        if not _cell_covers(a, b):
            return False
    return True


def _row_to_rule(table: Dict[str, Any], row: Dict[str, Any], inputs: Dict[str, Dict[str, Any]], index: int) -> Dict[str, Any]:
    conditions = []
    for input_id, inp in inputs.items():
        cell = (row.get("cells", {}) or {}).get(input_id)
        if _is_wildcard(cell):
            continue
        field = str(inp.get("variable_id") or inp.get("variable") or inp.get("name") or input_id)
        conditions.append({"field": field, "operator": cell.get("operator", "=="), "value": cell.get("value")})
    return {
        "id": str(row.get("id") or f"row_{index + 1}"),
        "name": row.get("label") or f"Row {index + 1}",
        "definition": {"conditions": conditions},
    }


def analyze_decision_table(table: Dict[str, Any]) -> Dict[str, Any]:
    """Optimiser: conflicts, gaps, unreachable rows, and invalid values.

    Overlap/gap severity is hit-policy aware — under first/priority an overlap
    is resolved by order (info), under unique/collect it is a real ambiguity
    (error). Returns a diagnostics list plus boolean summary flags.
    """
    inputs = _input_index(table)
    rows = table.get("rows", []) or []
    hit_policy = str(table.get("hit_policy", "first")).lower()
    diagnostics: List[Dict[str, Any]] = []

    # 1. Invalid values — per input cell.
    for ri, row in enumerate(rows):
        cells = row.get("cells", {}) or {}
        for input_id, inp in inputs.items():
            cell = cells.get(input_id)
            if _is_wildcard(cell):
                continue
            operator = str(cell.get("operator", "=="))
            value = cell.get("value")
            ft = str(inp.get("field_type") or inp.get("fieldType") or "").lower()
            col = inp.get("name") or input_id
            if operator == "between" and cell.get("value2") in (None, ""):
                diagnostics.append({"type": "invalid", "severity": "error", "rows": [row.get("id")],
                                    "description": f'Row {ri + 1}, "{col}": "between" needs an upper bound (value2)'})
            if ft in {"number", "integer", "float"} and operator not in {"in", "not_in", "exists", "!exists"}:
                try:
                    float(value)
                except (TypeError, ValueError):
                    diagnostics.append({"type": "invalid", "severity": "error", "rows": [row.get("id")],
                                        "description": f'Row {ri + 1}, "{col}": "{value}" is not a number'})
            if ft == "boolean" and str(value).strip().lower() not in {"true", "false", "1", "0", "yes", "no"}:
                diagnostics.append({"type": "invalid", "severity": "warning", "rows": [row.get("id")],
                                    "description": f'Row {ri + 1}, "{col}": "{value}" is not a boolean'})
            allowed = inp.get("allowed_values") or inp.get("allowedValues")
            if allowed and operator == "==" and str(parse_rule_value(value)) not in {str(a) for a in allowed}:
                diagnostics.append({"type": "invalid", "severity": "warning", "rows": [row.get("id")],
                                    "description": f'Row {ri + 1}, "{col}": "{value}" is not in the allowed set'})

    # 1b. Invalid outputs — outcome column must hold a known outcome.
    outcome_output_id = _outcome_output_id(table)
    if outcome_output_id is not None:
        for ri, row in enumerate(rows):
            val = (row.get("outputs", {}) or {}).get(outcome_output_id)
            if val is not None and str(val).lower() not in VALID_OUTCOMES:
                diagnostics.append({"type": "invalid", "severity": "warning", "rows": [row.get("id")],
                                    "description": f'Row {ri + 1}: outcome "{val}" is not one of {sorted(VALID_OUTCOMES)}'})

    # 2. Unreachable / shadowed rows (first & priority: earlier rows win).
    if hit_policy in {"first", "priority"}:
        for j in range(len(rows)):
            for i in range(j):
                if _row_covers(inputs, rows[i], rows[j]):
                    diagnostics.append({"type": "unreachable", "severity": "warning",
                                        "rows": [rows[j].get("id")], "shadowedBy": rows[i].get("id"),
                                        "description": f'Row {j + 1} is unreachable — Row {i + 1} already covers all of its inputs'})
                    break

    # 3. Conflicts (overlap) + gaps — reuse the tested MECE algebra.
    mece_rules = [_row_to_rule(table, row, inputs, ri) for ri, row in enumerate(rows)]
    conflict_error = False
    gap_error = False
    if len(mece_rules) >= 1:
        mece = analyze_mece(mece_rules)
        overlap_is_error = hit_policy in {"unique", "collect"}
        has_default = bool((table.get("default_row") or {}).get("outputs"))
        for d in mece.get("diagnostics", []):
            if d.get("type") == "overlap":
                sev = "error" if overlap_is_error else "info"
                if sev == "error":
                    conflict_error = True
                diagnostics.append({"type": "conflict", "severity": sev,
                                    "rows": d.get("involvedRules", []), "fields": d.get("fields", []),
                                    "description": d.get("description")})
            elif d.get("type") == "gap":
                # A default row closes every gap.
                sev = "info" if has_default else d.get("severity", "warning")
                if sev == "error":
                    gap_error = True
                diagnostics.append({"type": "gap", "severity": sev,
                                    "fields": d.get("fields", []), "description": d.get("description")})

    return {
        "diagnostics": diagnostics,
        "hasConflicts": conflict_error or any(d["type"] == "conflict" and d["severity"] == "error" for d in diagnostics),
        "hasGaps": gap_error,
        "hasInvalidValues": any(d["type"] == "invalid" and d["severity"] == "error" for d in diagnostics),
        "hasUnreachableRows": any(d["type"] == "unreachable" for d in diagnostics),
        "rowCount": len(rows),
        "hitPolicy": hit_policy,
        "ok": not any(d["severity"] == "error" for d in diagnostics),
    }
