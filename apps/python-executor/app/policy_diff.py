"""Policy diff for promotion review — show what actually changed before shipping a decision.

A reviewer should see, before promoting a policy, exactly how its decision logic differs from the
currently-live version: which steps were added/removed, and which rules / scorecards / decision
tables changed. Each promotion stores a snapshot of the policy's decision definition, so the diff
is the structural delta between the working copy and the last promoted snapshot — recorded on the
approval for audit. Use-case agnostic: it diffs the generic policy structure, no domain logic.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .logic import json_dumps


def policy_snapshot(storage: Any, tenant_id: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    """Capture a policy's decision definition: its ordered steps plus the definitions of every
    rule / scorecard / decision table it references."""
    steps = [
        {"type": s.get("type"), "ref_id": s.get("ref_id") or s.get("ref"), "id": s.get("id")}
        for s in policy.get("steps", []) or []
    ]

    def _referenced(step_type: str) -> List[str]:
        return [s["ref_id"] for s in steps if s.get("type") == step_type and s.get("ref_id")]

    rules: Dict[str, Any] = {}
    for rid in dict.fromkeys(_referenced("rule")):
        rule = storage.get_rule(rid, tenant_id=tenant_id)
        if rule:
            rules[rid] = {"name": rule.get("name"), "tree": rule.get("tree"), "nodes": rule.get("nodes")}

    scorecards: Dict[str, Any] = {}
    for sid in dict.fromkeys(_referenced("scorecard")):
        sc = storage.get_scorecard(sid, tenant_id=tenant_id)
        if sc:
            scorecards[sid] = {"name": sc.get("name"), "attributes": sc.get("attributes"), "bands": sc.get("bands")}

    tables: Dict[str, Any] = {}
    for tid in dict.fromkeys(_referenced("decision_table")):
        table = storage.get_decision_table(tid, tenant_id=tenant_id)
        if table:
            tables[tid] = {"name": table.get("name"), "hit_policy": table.get("hit_policy"), "rows": table.get("rows")}

    return {
        "policy": {"name": policy.get("name"), "defaultOutcome": policy.get("defaultOutcome")},
        "steps": steps,
        "rules": rules,
        "scorecards": scorecards,
        "decisionTables": tables,
    }


def _entity_diff(current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, List[str]]:
    added = [k for k in current if k not in previous]
    removed = [k for k in previous if k not in current]
    changed = [k for k in current if k in previous and json_dumps(current[k]) != json_dumps(previous[k])]
    return {"added": added, "removed": removed, "changed": changed}


def diff_snapshots(current: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Structural delta between the working policy snapshot and the last promoted one."""
    has_baseline = bool(previous)
    previous = previous or {}

    def step_key(step: Dict[str, Any]):
        return (step.get("type"), step.get("ref_id"))

    cur_keys = [step_key(s) for s in current.get("steps", [])]
    prev_keys = [step_key(s) for s in previous.get("steps", [])]
    steps_added = [{"type": t, "ref_id": r} for (t, r) in cur_keys if (t, r) not in prev_keys]
    steps_removed = [{"type": t, "ref_id": r} for (t, r) in prev_keys if (t, r) not in cur_keys]

    rules = _entity_diff(current.get("rules", {}), previous.get("rules", {}))
    scorecards = _entity_diff(current.get("scorecards", {}), previous.get("scorecards", {}))
    tables = _entity_diff(current.get("decisionTables", {}), previous.get("decisionTables", {}))

    changed = bool(
        steps_added or steps_removed
        or any(rules[k] for k in ("added", "removed", "changed"))
        or any(scorecards[k] for k in ("added", "removed", "changed"))
        or any(tables[k] for k in ("added", "removed", "changed"))
    )
    return {
        "hasBaseline": has_baseline,
        "changed": changed if has_baseline else True,
        "steps": {"added": steps_added, "removed": steps_removed},
        "rules": rules,
        "scorecards": scorecards,
        "decisionTables": tables,
    }
