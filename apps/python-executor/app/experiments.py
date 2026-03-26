from __future__ import annotations

import copy
import hashlib
from typing import Any, Dict, List, Optional


def assign_variant(user_id: str, experiment_id: str, variants: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not user_id or not variants:
        return None
    hash_input = "{0}:{1}".format(experiment_id, user_id)
    hash_val = int(hashlib.md5(hash_input.encode("utf-8")).hexdigest(), 16) % 100
    cumulative = 0
    for variant in variants:
        cumulative += int(variant.get("weight", 0))
        if hash_val < cumulative:
            return copy.deepcopy(variant)
    return copy.deepcopy(variants[-1]) if variants else None


def resolve_experiment_assignment(storage, tenant_id: str, policy_id: str, user_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    for experiment in storage.list_experiments(tenant_id=tenant_id):
        if experiment.get("status") != "running":
            continue
        if experiment.get("target_policy_id") != policy_id:
            continue
        variant = assign_variant(user_id, experiment["id"], experiment.get("variants", []))
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
