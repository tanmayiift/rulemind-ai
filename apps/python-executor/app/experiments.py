from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .champion_challenger import current_ramp_weight


def _elapsed_days(start_date: Any) -> Optional[float]:
    """Days since an experiment started, or None if it has no usable start.

    storage.serialize_datetime emits `2026-09-05T04:00:00Z` -- naive UTC with a literal Z that
    fromisoformat rejects before 3.11, and this backend supports 3.10. Hence the manual strip
    rather than a bare fromisoformat.
    """
    if start_date is None:
        return None
    if isinstance(start_date, datetime):
        started = start_date
    else:
        text = str(start_date).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1]
        try:
            started = datetime.fromisoformat(text)
        except ValueError:
            return None
    now = datetime.now(timezone.utc)
    if started.tzinfo is None:
        now = now.replace(tzinfo=None)
    return max(0.0, (now - started).total_seconds() / 86400.0)


def _effective_weight(variant: Dict[str, Any], elapsed_days: Optional[float]) -> int:
    """The weight this variant should actually receive right now.

    THE BUG THIS FIXES. `champion_challenger.current_ramp_weight` has existed for the ramp
    schedule -- `ramp: [{"day": 0, "weight": 10}, {"day": 3, "weight": 25}, ...]` -- and was
    called from exactly one place: `analytics.py`, which renders the dashboard. The live
    assignment below read the STATIC `variant["weight"]` and nothing else, so a canary
    configured to ramp 10% -> 25% -> 50% never ramped. It served 10% forever.

    That is worse than the feature simply being absent. An absent feature is visible; here the
    dashboard reported the challenger at 25% of traffic on day 3 while production was still
    sending it 10%, and every guardrail decision an operator made was made against a traffic
    split that was not real.

    Falls back to the static weight when there is no ramp (current_ramp_weight returns None) or
    when the experiment has no start_date to measure from -- an experiment with a ramp but no
    start is a misconfiguration, and serving the static weight is the conservative reading of it.
    """
    if elapsed_days is not None:
        ramped = current_ramp_weight(variant, elapsed_days)
        if ramped is not None:
            return int(ramped)
    try:
        return int(variant.get("weight", 0))
    except (TypeError, ValueError):
        return 0


def _weights_for(variants: List[Dict[str, Any]], elapsed_days: Optional[float]) -> List[int]:
    """Effective weight per variant, in the order they are walked.

    WHY THE CHAMPION IS RECOMPUTED RATHER THAN LEFT ALONE. A ramp is authored on the CHALLENGER
    only -- `{"role": "challenger", "weight": 10, "ramp": [...25, ...50]}` -- while the champion
    carries a static 90. Ramping the challenger's own number to 25 therefore changes nothing on
    its own: assign_variant walks the champion first, its 90 still swallows the first 90% of the
    hash space, and the challenger still receives exactly the 10% left over. The ramp has to move
    the boundary between them, so the champion takes the REMAINDER of what the challengers are
    currently entitled to. "Promote at 5% canary" is a statement about the canary's share; the
    champion's share is whatever is left.

    Only applied when a ramp is actually in effect. Without one this returns the authored weights
    untouched, because existing experiments were written against the current behaviour and some
    of them do not sum to 100 -- silently renormalising those would move live traffic on
    experiments nobody touched.
    """
    weights = [_effective_weight(variant, elapsed_days) for variant in variants]
    if elapsed_days is None or not any(isinstance(v.get("ramp"), list) and v.get("ramp") for v in variants):
        return weights

    champion_index = next(
        (i for i, v in enumerate(variants) if str(v.get("role", "")).lower() == "champion"), None)
    if champion_index is None:
        # champion_challenger.identify_roles falls back to the first variant when no role is
        # declared; match it, so the two modules cannot disagree about who the champion is.
        champion_index = 0
    others = sum(w for i, w in enumerate(weights) if i != champion_index)
    weights[champion_index] = max(0, 100 - others)
    return weights


def assign_variant(
    user_id: str,
    experiment_id: str,
    variants: List[Dict[str, Any]],
    elapsed_days: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if not user_id or not variants:
        return None
    hash_input = "{0}:{1}".format(experiment_id, user_id)
    hash_val = int(hashlib.md5(hash_input.encode("utf-8")).hexdigest(), 16) % 100
    cumulative = 0
    for variant, weight in zip(variants, _weights_for(variants, elapsed_days)):
        cumulative += weight
        if hash_val < cumulative:
            return copy.deepcopy(variant)
    return copy.deepcopy(variants[-1]) if variants else None


def resolve_experiment_assignment(storage, tenant_id: str, policy_id: str, user_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    # The API enforces at most one running experiment per policy, so normally there is a single
    # candidate. Should legacy/imported data ever hold more than one, resolve deterministically
    # (oldest-created wins) rather than relying on iteration order.
    candidates = [
        experiment
        for experiment in storage.list_experiments(tenant_id=tenant_id)
        if experiment.get("status") == "running" and experiment.get("target_policy_id") == policy_id
    ]
    candidates.sort(key=lambda experiment: (str(experiment.get("created_at") or ""), str(experiment.get("id") or "")))
    for experiment in candidates:
        variant = assign_variant(
            user_id, experiment["id"], experiment.get("variants", []),
            elapsed_days=_elapsed_days(experiment.get("start_date")),
        )
        if not variant:
            continue
        return {
            "experiment": {
                "id": experiment["id"],
                "name": experiment.get("name"),
                "status": experiment.get("status"),
            },
            "variant": variant,
        }
    return None


def _set_tree_condition(tree: Optional[Dict[str, Any]], index: int, field: str, value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(tree, dict):
        return tree
    cloned = copy.deepcopy(tree)
    counter = {"index": -1}

    def walk(node: Dict[str, Any]) -> None:
        node_type = node.get("type")
        if node_type == "condition":
            counter["index"] += 1
            if counter["index"] == index:
                node[field] = value
                return
        elif node_type == "not":
            child = node.get("child")
            if isinstance(child, dict):
                walk(child)
        elif node_type == "group":
            for child in node.get("children", []):
                if isinstance(child, dict):
                    walk(child)

    walk(cloned)
    return cloned


def _set_nodes_condition(nodes: Optional[List[Dict[str, Any]]], index: int, field: str, value: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(nodes, list):
        return nodes
    cloned = copy.deepcopy(nodes)
    condition_index = -1
    for node in cloned:
        if node.get("type") == "condition":
            condition_index += 1
            if condition_index == index:
                node[field] = value
                break
    return cloned


def apply_experiment_overrides(rules: Dict[str, Dict[str, Any]], assignment: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not assignment:
        return rules
    variant = assignment.get("variant") or {}
    overrides = variant.get("overrides") or {}
    if not overrides:
        return rules
    patched = {rule_id: copy.deepcopy(rule) for rule_id, rule in rules.items()}
    for path, value in overrides.items():
        if not isinstance(path, str):
            continue
        parts = path.split(".")
        if len(parts) != 4:
            continue
        rule_id, collection, index_raw, field = parts
        if rule_id not in patched:
            continue
        try:
            index = int(index_raw)
        except ValueError:
            continue
        rule = patched[rule_id]
        if collection == "conditions":
            rule["tree"] = _set_tree_condition(rule.get("tree"), index, field, value)
            rule["nodes"] = _set_nodes_condition(rule.get("nodes"), index, field, value)
    return patched
