"""
MECE (Mutually Exclusive, Collectively Exhaustive) analysis for policy rules.

Industry-agnostic: works with any decision domain — lending, insurance, fraud,
healthcare, supply-chain, compliance, HR, etc. Operates purely on field types
and operators, never assuming domain semantics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

NEG_INF = float("-inf")
POS_INF = float("inf")
BRANCH_CAP = 200


# ---------------------------------------------------------------------------
# Interval
# ---------------------------------------------------------------------------


@dataclass
class Interval:
    lower: float
    upper: float
    lower_inclusive: bool
    upper_inclusive: bool

    def is_empty(self) -> bool:
        if self.lower > self.upper:
            return True
        if self.lower == self.upper and not (self.lower_inclusive and self.upper_inclusive):
            return True
        return False

    def format(self) -> str:
        lo = "-∞" if self.lower == NEG_INF else str(self.lower)
        hi = "+∞" if self.upper == POS_INF else str(self.upper)
        lb = "[" if self.lower_inclusive else "("
        rb = "]" if self.upper_inclusive else ")"
        return f"{lb}{lo}, {hi}{rb}"


def full_interval() -> Interval:
    return Interval(NEG_INF, POS_INF, False, False)


def empty_interval() -> Interval:
    return Interval(0, 0, False, False)


def intervals_overlap(a: Interval, b: Interval) -> bool:
    if a.is_empty() or b.is_empty():
        return False
    if a.upper < b.lower or b.upper < a.lower:
        return False
    if a.upper == b.lower:
        return a.upper_inclusive and b.lower_inclusive
    if b.upper == a.lower:
        return b.upper_inclusive and a.lower_inclusive
    return True


def interval_intersection(a: Interval, b: Interval) -> Optional[Interval]:
    if a.is_empty() or b.is_empty():
        return None
    lower = max(a.lower, b.lower)
    upper = min(a.upper, b.upper)

    if a.lower == b.lower:
        li = a.lower_inclusive and b.lower_inclusive
    elif a.lower > b.lower:
        li = a.lower_inclusive
    else:
        li = b.lower_inclusive

    if a.upper == b.upper:
        ui = a.upper_inclusive and b.upper_inclusive
    elif a.upper < b.upper:
        ui = a.upper_inclusive
    else:
        ui = b.upper_inclusive

    if lower > upper:
        return None
    if lower == upper and not (li and ui):
        return None
    return Interval(lower, upper, li, ui)


def operator_to_interval(op: str, value: float, value2: Optional[float] = None) -> Interval:
    if math.isnan(value):
        return empty_interval()
    if value2 is not None and math.isnan(value2):
        return empty_interval()

    if op == "==":
        return Interval(value, value, True, True)
    if op == "!=":
        return full_interval()
    if op == ">":
        return Interval(value, POS_INF, False, False)
    if op == ">=":
        return Interval(value, POS_INF, True, False)
    if op == "<":
        return Interval(NEG_INF, value, False, False)
    if op == "<=":
        return Interval(NEG_INF, value, False, True)
    if op == "between":
        lo = min(value, value2 or value)
        hi = max(value, value2 or value)
        return Interval(lo, hi, True, True)
    return full_interval()


def invert_interval(iv: Interval) -> List[Interval]:
    if iv.is_empty():
        return [full_interval()]
    result = []
    if iv.lower > NEG_INF:
        result.append(Interval(NEG_INF, iv.lower, False, not iv.lower_inclusive))
    if iv.upper < POS_INF:
        result.append(Interval(iv.upper, POS_INF, not iv.upper_inclusive, False))
    if not result and iv.lower == NEG_INF and iv.upper == POS_INF:
        return []
    return result


# ---------------------------------------------------------------------------
# ValueSet
# ---------------------------------------------------------------------------


@dataclass
class ValueSet:
    included: Optional[Set[str]]  # None = universal
    excluded: Set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return self.included is not None and len(self.included) == 0


def universal_value_set() -> ValueSet:
    return ValueSet(included=None, excluded=set())


def empty_value_set() -> ValueSet:
    return ValueSet(included=set(), excluded=set())


def value_sets_overlap(a: ValueSet, b: ValueSet) -> bool:
    if a.is_empty() or b.is_empty():
        return False
    if a.included is None and b.included is None:
        return True
    if a.included is None:
        return any(v not in a.excluded for v in b.included)
    if b.included is None:
        return any(v not in b.excluded for v in a.included)
    return bool(a.included & b.included)


def value_sets_intersection(a: ValueSet, b: ValueSet) -> ValueSet:
    if a.is_empty() or b.is_empty():
        return empty_value_set()
    if a.included is None and b.included is None:
        return ValueSet(included=None, excluded=a.excluded | b.excluded)
    if a.included is None:
        return ValueSet(included={v for v in b.included if v not in a.excluded})
    if b.included is None:
        return ValueSet(included={v for v in a.included if v not in b.excluded})
    return ValueSet(included=a.included & b.included)


def invert_value_set(vs: ValueSet) -> ValueSet:
    if vs.included is None:
        return ValueSet(included=set(vs.excluded))
    return ValueSet(included=None, excluded=set(vs.included))


# ---------------------------------------------------------------------------
# FieldConstraint
# ---------------------------------------------------------------------------


@dataclass
class FieldConstraint:
    field_name: str
    field_type: str
    intervals: Optional[List[Interval]] = None
    value_set: Optional[ValueSet] = None
    excluded_points: Optional[List[float]] = None
    opaque: bool = False


# ---------------------------------------------------------------------------
# Constraint extraction
# ---------------------------------------------------------------------------


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return float("nan")


def extract_condition_constraint(node: Dict[str, Any]) -> Optional[FieldConstraint]:
    config = node.get("config") or {}
    field_name = config.get("field")
    operator = config.get("operator", "==")
    raw_value = config.get("value")
    # Auto-detect field type from value when fieldType is not explicitly set
    explicit_type = config.get("fieldType")
    if explicit_type:
        field_type = explicit_type
    elif isinstance(raw_value, bool):
        field_type = "boolean"
    elif isinstance(raw_value, (int, float)):
        field_type = "number"
    elif isinstance(raw_value, list) and raw_value and isinstance(raw_value[0], (int, float)):
        field_type = "number"
    elif operator in (">=", ">", "<=", "<", "between"):
        field_type = "number"
    else:
        field_type = "string"
    if not field_name:
        return None

    if field_type == "number" or node.get("type") == "score":
        value = _safe_float(config.get("value", 0))
        value2 = _safe_float(config.get("value2")) if config.get("value2") is not None else None
        if operator == "!=":
            return FieldConstraint(field_name, field_type, intervals=[full_interval()], excluded_points=[value] if not math.isnan(value) else [])
        iv = operator_to_interval(operator, value, value2)
        return FieldConstraint(field_name, field_type, intervals=[iv])

    if field_type == "date":
        value = _safe_float(config.get("value", 0))
        value2 = _safe_float(config.get("value2")) if config.get("value2") is not None else None
        if operator == "!=":
            return FieldConstraint(field_name, field_type, intervals=[full_interval()], excluded_points=[value] if not math.isnan(value) else [])
        if math.isnan(value):
            return FieldConstraint(field_name, field_type, intervals=[full_interval()], opaque=True)
        iv = operator_to_interval(operator, value, value2)
        return FieldConstraint(field_name, field_type, intervals=[iv])

    if field_type == "boolean":
        bool_val = str(config.get("value", "true")).lower() == "true"
        if operator == "==":
            return FieldConstraint(field_name, field_type, value_set=ValueSet(included={str(bool_val).lower()}))
        if operator == "!=":
            return FieldConstraint(field_name, field_type, value_set=ValueSet(included={str(not bool_val).lower()}))
        if operator == "exists":
            return FieldConstraint(field_name, field_type, value_set=ValueSet(included={"true", "false"}))
        if operator == "!exists":
            return FieldConstraint(field_name, field_type, value_set=empty_value_set())
        return FieldConstraint(field_name, field_type, value_set=ValueSet(included={"true", "false"}))

    # String / enum
    if operator == "in":
        raw = config.get("value", "")
        if isinstance(raw, list):
            values = {str(v).strip() for v in raw if str(v).strip()}
        else:
            values = {v.strip() for v in str(raw).split(",") if v.strip()}
        return FieldConstraint(field_name, field_type, value_set=ValueSet(included=values))
    if operator == "not_in":
        raw = config.get("value", "")
        if isinstance(raw, list):
            values = {str(v).strip() for v in raw if str(v).strip()}
        else:
            values = {v.strip() for v in str(raw).split(",") if v.strip()}
        return FieldConstraint(field_name, field_type, value_set=ValueSet(included=None, excluded=values))
    if operator == "==" and config.get("value") is not None:
        return FieldConstraint(field_name, field_type, value_set=ValueSet(included={str(config["value"])}))
    if operator == "!=" and config.get("value") is not None:
        return FieldConstraint(field_name, field_type, value_set=ValueSet(included=None, excluded={str(config["value"])}))
    if operator == "exists":
        return FieldConstraint(field_name, field_type, value_set=universal_value_set())
    if operator == "!exists":
        return FieldConstraint(field_name, field_type, value_set=empty_value_set())
    if operator == "regex":
        return FieldConstraint(field_name, field_type, value_set=universal_value_set(), opaque=True)

    return FieldConstraint(field_name, field_type, value_set=universal_value_set(), opaque=True)


def extract_rule_constraint_space(definition: Dict[str, Any]) -> List[List[FieldConstraint]]:
    # Support shorthand: flat list of conditions (each with field/operator/value)
    conditions = definition.get("conditions")
    if isinstance(conditions, list) and conditions:
        branch: List[FieldConstraint] = []
        for cond in conditions:
            fc = extract_condition_constraint({
                "type": "condition",
                "config": {"field": cond.get("field"), "operator": cond.get("operator"), "value": cond.get("value")},
            })
            if fc:
                branch.append(fc)
        return [branch] if branch else [[]]

    nodes = definition.get("nodes") or []
    connections = definition.get("connections") or []
    if not nodes:
        return [[]]

    node_map: Dict[str, Dict[str, Any]] = {n["id"]: n for n in nodes}
    children: Dict[str, List[str]] = {}
    for c in connections:
        children.setdefault(c["from"], []).append(c["to"])

    outcome_types = {"approve", "reject", "review"}

    def is_outcome(nid: str) -> bool:
        n = node_map.get(nid)
        return n["type"] in outcome_types if n else False

    has_incoming = {c["to"] for c in connections}
    roots = [n for n in nodes if n["id"] not in has_incoming and n["type"] not in outcome_types]

    visiting: set = set()

    def walk(nid: str) -> List[List[FieldConstraint]]:
        if nid in visiting:
            return [[]]
        visiting.add(nid)
        try:
            node = node_map.get(nid)
            if not node:
                return [[]]
            child_ids = [cid for cid in children.get(nid, []) if not is_outcome(cid)]
            ntype = node.get("type", "")

            if ntype in ("condition", "score"):
                constraint = extract_condition_constraint(node)
                if not constraint:
                    return [[]]
                cb = []
                for cid in child_ids:
                    cb.extend(walk(cid))
                if not cb:
                    cb = [[]]
                return [[constraint] + b for b in cb]

            if ntype == "and" or (ntype == "group" and (node.get("config") or {}).get("groupOp", "AND") == "AND"):
                return _expand_and(child_ids, walk)

            if ntype == "or" or (ntype == "group" and (node.get("config") or {}).get("groupOp") == "OR"):
                return _expand_or(child_ids, walk)

            if ntype == "not":
                if not child_ids:
                    return [[]]
                child_branches = walk(child_ids[0])
                return _invert_branches(child_branches)

            if ntype == "trigger":
                if not child_ids:
                    return [[]]
                result = []
                for cid in child_ids:
                    result.extend(walk(cid))
                return result or [[]]

            return [[]]
        finally:
            visiting.discard(nid)

    if not roots:
        return [[]]
    all_branches: List[List[FieldConstraint]] = []
    for root in roots:
        all_branches.extend(walk(root["id"]))
    return (all_branches or [[]])[:BRANCH_CAP]


def _expand_and(child_ids: List[str], walk) -> List[List[FieldConstraint]]:
    if not child_ids:
        return [[]]
    result: List[List[FieldConstraint]] = [[]]
    for cid in child_ids:
        cb = walk(cid)
        if not cb:
            continue
        new_result = []
        for existing in result:
            for branch in cb:
                if len(new_result) >= BRANCH_CAP:
                    break
                new_result.append(existing + branch)
            if len(new_result) >= BRANCH_CAP:
                break
        result = new_result
        if not result:
            return [[]]
    return result


def _expand_or(child_ids: List[str], walk) -> List[List[FieldConstraint]]:
    if not child_ids:
        return [[]]
    branches: List[List[FieldConstraint]] = []
    for cid in child_ids:
        if len(branches) >= BRANCH_CAP:
            break
        branches.extend(walk(cid))
    return (branches or [[]])[:BRANCH_CAP]


def _invert_branches(branches: List[List[FieldConstraint]]) -> List[List[FieldConstraint]]:
    inverted: List[List[FieldConstraint]] = []
    for branch in branches:
        if not branch:
            continue
        if len(branch) == 1:
            c = branch[0]
            if c.opaque:
                inverted.append([FieldConstraint(c.field_name, c.field_type, c.intervals, c.value_set, c.excluded_points, True)])
            elif c.intervals and not (c.excluded_points or []):
                for iv in invert_interval(c.intervals[0]):
                    inverted.append([FieldConstraint(c.field_name, c.field_type, intervals=[iv])])
            elif c.intervals and (c.excluded_points or []):
                for pt in (c.excluded_points or []):
                    inverted.append([FieldConstraint(c.field_name, c.field_type, intervals=[Interval(pt, pt, True, True)])])
            elif c.value_set:
                inverted.append([FieldConstraint(c.field_name, c.field_type, value_set=invert_value_set(c.value_set))])
        else:
            for c in branch:
                inverted.extend(_invert_branches([[c]]))
    return inverted or [[]]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_branch(branch: List[FieldConstraint]) -> Dict[str, FieldConstraint]:
    by_field: Dict[str, FieldConstraint] = {}
    for c in branch:
        existing = by_field.get(c.field_name)
        if not existing:
            by_field[c.field_name] = FieldConstraint(
                c.field_name, c.field_type,
                intervals=list(c.intervals) if c.intervals else None,
                value_set=ValueSet(
                    included=set(c.value_set.included) if c.value_set and c.value_set.included is not None else (None if c.value_set else None),
                    excluded=set(c.value_set.excluded) if c.value_set else set(),
                ) if c.value_set else None,
                excluded_points=list(c.excluded_points) if c.excluded_points else None,
                opaque=c.opaque,
            )
            continue

        if c.opaque:
            existing.opaque = True

        if existing.intervals and c.intervals:
            merged = []
            for ei in existing.intervals:
                for ci in c.intervals:
                    inter = interval_intersection(ei, ci)
                    if inter:
                        merged.append(inter)
            existing.intervals = merged or [empty_interval()]

        if c.excluded_points:
            existing.excluded_points = (existing.excluded_points or []) + c.excluded_points

        if existing.value_set and c.value_set:
            existing.value_set = value_sets_intersection(existing.value_set, c.value_set)

    return by_field


# ---------------------------------------------------------------------------
# Overlap checking
# ---------------------------------------------------------------------------


def constraints_overlap_for_field(a: FieldConstraint, b: FieldConstraint) -> bool:
    if a.opaque or b.opaque:
        return True
    if a.intervals and b.intervals:
        for ia in a.intervals:
            for ib in b.intervals:
                if ia.is_empty() or ib.is_empty():
                    continue
                if not intervals_overlap(ia, ib):
                    continue
                inter = interval_intersection(ia, ib)
                if not inter:
                    continue
                if inter.lower == inter.upper and inter.lower_inclusive and inter.upper_inclusive:
                    pt = inter.lower
                    if (a.excluded_points and pt in a.excluded_points) or (b.excluded_points and pt in b.excluded_points):
                        continue
                return True
        return False
    if a.value_set and b.value_set:
        return value_sets_overlap(a.value_set, b.value_set)
    return True


def branches_overlap(ba: List[FieldConstraint], bb: List[FieldConstraint]) -> bool:
    fa = normalize_branch(ba)
    fb = normalize_branch(bb)
    shared = [f for f in fa if f in fb]
    if not shared:
        return True
    for f in shared:
        if not constraints_overlap_for_field(fa[f], fb[f]):
            return False
    return True


# ---------------------------------------------------------------------------
# Exhaustiveness
# ---------------------------------------------------------------------------


def check_numeric_exhaustiveness(field_name: str, all_intervals: List[Interval]) -> List[Dict[str, Any]]:
    valid = [iv for iv in all_intervals if not iv.is_empty()]
    if not valid:
        return [{"type": "gap", "severity": "error", "fields": [field_name], "description": f"All rules for {field_name} have empty conditions", "involvedRules": []}]

    sorted_ivs = sorted(valid, key=lambda iv: (iv.lower, 0 if iv.lower_inclusive else 1))
    gaps = []

    if sorted_ivs[0].lower > NEG_INF:
        b = sorted_ivs[0].lower
        incl = sorted_ivs[0].lower_inclusive
        gaps.append({"type": "gap", "severity": "error", "fields": [field_name], "description": f"No rule covers {field_name} {'<' if incl else '<='} {b}", "involvedRules": []})

    cur_upper = sorted_ivs[0].upper
    cur_ui = sorted_ivs[0].upper_inclusive

    for i in range(1, len(sorted_ivs)):
        nxt = sorted_ivs[i]
        if cur_upper < nxt.lower:
            gaps.append({"type": "gap", "severity": "error", "fields": [field_name], "description": f"No rule covers {field_name} in ({cur_upper}, {nxt.lower})", "involvedRules": []})
        elif cur_upper == nxt.lower and not cur_ui and not nxt.lower_inclusive:
            gaps.append({"type": "gap", "severity": "error", "fields": [field_name], "description": f"No rule covers {field_name} = {cur_upper} — boundary gap", "involvedRules": []})
        if nxt.upper > cur_upper or (nxt.upper == cur_upper and nxt.upper_inclusive and not cur_ui):
            cur_upper = nxt.upper
            cur_ui = nxt.upper_inclusive

    if cur_upper < POS_INF:
        b = cur_upper
        incl = cur_ui
        gaps.append({"type": "gap", "severity": "error", "fields": [field_name], "description": f"No rule covers {field_name} {'>' if incl else '>='} {b}", "involvedRules": []})

    return gaps


def check_boolean_exhaustiveness(field_name: str, all_values: Set[str]) -> List[Dict[str, Any]]:
    gaps = []
    if "true" not in all_values:
        gaps.append({"type": "gap", "severity": "error", "fields": [field_name], "description": f"No rule covers {field_name} = true", "involvedRules": []})
    if "false" not in all_values:
        gaps.append({"type": "gap", "severity": "error", "fields": [field_name], "description": f"No rule covers {field_name} = false", "involvedRules": []})
    return gaps


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def analyze_mece(rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Analyze a set of rules for MECE properties.

    Each rule dict should have: id, name, definition (with nodes/connections), optional outcome.
    Returns a dict with isMutuallyExclusive, isCollectivelyExhaustive, diagnostics, etc.
    """
    warnings: List[str] = []

    if not rules:
        return {
            "isMutuallyExclusive": True,
            "isCollectivelyExhaustive": False,
            "diagnostics": [{"type": "gap", "severity": "error", "fields": [], "description": "Policy has no rules", "involvedRules": []}],
            "analyzedFields": [],
            "ruleCount": 0,
            "hasOpaqueConstraints": False,
            "warnings": ["No rules to analyze"],
        }

    if len(rules) == 1:
        branches = extract_rule_constraint_space(rules[0].get("definition", {}))
        all_fields = set()
        has_opaque = False
        for branch in branches:
            for c in branch:
                all_fields.add(c.field_name)
                if c.opaque:
                    has_opaque = True
        return {
            "isMutuallyExclusive": True,
            "isCollectivelyExhaustive": False,
            "diagnostics": [{"type": "gap", "severity": "info", "fields": list(all_fields), "description": "Single rule cannot be collectively exhaustive", "involvedRules": [rules[0]["id"]]}],
            "analyzedFields": list(all_fields),
            "ruleCount": 1,
            "hasOpaqueConstraints": has_opaque,
            "warnings": ["Some conditions use opaque operators"] if has_opaque else [],
        }

    # Extract constraint spaces
    rule_spaces = []
    for rule in rules:
        # Support both full definition format and shorthand conditions format
        defn = rule.get("definition", {})
        if not defn and "conditions" in rule:
            defn = {"conditions": rule["conditions"]}
        branches = extract_rule_constraint_space(defn)
        rule_spaces.append({
            "id": rule["id"],
            "name": rule.get("name", rule["id"]),
            "branches": branches,
            "definition": rule.get("definition", {}),
        })

    diagnostics: List[Dict[str, Any]] = []
    has_opaque = False

    # Pairwise overlap check
    for i in range(len(rule_spaces)):
        for j in range(i + 1, len(rule_spaces)):
            ra, rb = rule_spaces[i], rule_spaces[j]
            for ba in ra["branches"]:
                for bb in rb["branches"]:
                    if branches_overlap(ba, bb):
                        fa = normalize_branch(ba)
                        fb = normalize_branch(bb)
                        shared = [f for f in fa if f in fb]
                        descs = []
                        overlap_fields = []
                        for f in shared:
                            if constraints_overlap_for_field(fa[f], fb[f]):
                                overlap_fields.append(f)
                                if fa[f].opaque or fb[f].opaque:
                                    descs.append(f"{f} uses opaque operators")
                                elif fa[f].intervals and fb[f].intervals:
                                    inter = interval_intersection(fa[f].intervals[0], fb[f].intervals[0])
                                    descs.append(f"{f} overlaps in range {inter.format()}" if inter else f"{f} overlaps")
                                elif fa[f].value_set and fb[f].value_set:
                                    isect = value_sets_intersection(fa[f].value_set, fb[f].value_set)
                                    if isect.included and len(isect.included) > 0:
                                        descs.append(f"{f} overlaps on values: {{{', '.join(sorted(isect.included))}}}")
                                    else:
                                        descs.append(f"{f} overlaps")

                        if not shared:
                            diagnostics.append({
                                "type": "overlap",
                                "severity": "error",
                                "fields": [],
                                "description": f'Rules "{ra["name"]}" and "{rb["name"]}" have no shared fields — both fire on any input',
                                "involvedRules": [ra["id"], rb["id"]],
                            })
                        elif descs:
                            diagnostics.append({
                                "type": "overlap",
                                "severity": "error",
                                "fields": overlap_fields,
                                "description": f'Rules "{ra["name"]}" and "{rb["name"]}" overlap: {"; ".join(descs)}',
                                "involvedRules": [ra["id"], rb["id"]],
                            })
                        break  # First overlapping branch pair per rule pair
                else:
                    continue
                break

    # Collect fields for exhaustiveness
    numeric_intervals: Dict[str, List[Interval]] = {}
    boolean_values: Dict[str, Set[str]] = {}
    categorical_info: Dict[str, Dict[str, Any]] = {}
    all_fields: Set[str] = set()

    for space in rule_spaces:
        for branch in space["branches"]:
            normalized = normalize_branch(branch)
            for fname, constraint in normalized.items():
                all_fields.add(fname)
                if constraint.opaque:
                    has_opaque = True
                if constraint.intervals:
                    numeric_intervals.setdefault(fname, []).extend(constraint.intervals)
                if constraint.value_set:
                    if constraint.field_type == "boolean":
                        bv = boolean_values.setdefault(fname, set())
                        if constraint.value_set.included:
                            bv.update(constraint.value_set.included)
                        else:
                            bv.update({"true", "false"})
                    else:
                        ci = categorical_info.setdefault(fname, {"values": set(), "has_universal": False})
                        if constraint.value_set.included is None:
                            ci["has_universal"] = True
                        elif constraint.value_set.included:
                            ci["values"].update(constraint.value_set.included)

    for fname, ivs in numeric_intervals.items():
        diagnostics.extend(check_numeric_exhaustiveness(fname, ivs))

    for fname, vals in boolean_values.items():
        diagnostics.extend(check_boolean_exhaustiveness(fname, vals))

    for fname, info in categorical_info.items():
        if not info["has_universal"] and info["values"]:
            vals_str = ", ".join(sorted(list(info["values"])[:5]))
            diagnostics.append({
                "type": "gap",
                "severity": "warning",
                "fields": [fname],
                "description": f'Field "{fname}" only covers specific values {{{vals_str}}} — other values won\'t match',
                "involvedRules": [],
            })

    if has_opaque:
        warnings.append("Some conditions use regex or opaque operators — analysis may be imprecise")

    is_me = not any(d["type"] == "overlap" and d["severity"] == "error" for d in diagnostics)
    is_ce = not any(d["type"] == "gap" and d["severity"] == "error" for d in diagnostics)

    return {
        "isMutuallyExclusive": is_me,
        "isCollectivelyExhaustive": is_ce,
        "diagnostics": diagnostics,
        "analyzedFields": sorted(all_fields),
        "ruleCount": len(rules),
        "hasOpaqueConstraints": has_opaque,
        "warnings": warnings,
    }
