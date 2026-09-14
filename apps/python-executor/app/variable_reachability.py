"""Which variables can a policy actually reach?

A reachability analysis over a policy's steps, used to skip computing variables the
policy never reads (`COMPUTE_ONLY_USED_VARS=1`).

WHY THIS IS A MODULE AND NOT A METHOD. Both decision paths need the same answer.
`PolicyExecutor` (the full path) and `fast_decide` (the serving path) must compute the
SAME set of variables for the same policy, or the flag becomes a performance switch that
changes the outcome -- the FAST_DECIDE divergence class this codebase has already fixed
four separate times (input validation, evaluation budget, sample seeding, and this).
A second copy of the walk in `fast_decide` would be that bug pre-written, so there is one
implementation and both paths call it.

THIS ANALYSIS HAS TO BE COMPLETE. A step type missed here is a variable that is never
computed -- `ctx.variables` has no key at all and the decision is made without it. It was
incomplete twice, and both gaps were found by the flag failing tests rather than by
reading:

  - rule + scorecard only        -> 16 failures (connector-supplied variables dropped)
  - + connector, top level only  ->  7 failures (decision tables, loops, nested steps)

So the walk is RECURSIVE over every step type that can reach a variable, and deliberately
OVER-INCLUSIVE: a substring match on variable code counts as a dependency. A false
positive costs one sandbox call; a false negative changes the decision.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional, Set

__all__ = ["needed_variable_ids", "collect_condition_variable_ids", "collect_step_variable_ids",
           "compute_only_used_vars"]


def collect_condition_variable_ids(node: Any, out: Set[str]) -> None:
    """Every `{type: "condition", variable: ...}` node anywhere inside `node`."""
    if isinstance(node, dict):
        if node.get("type") == "condition" and node.get("variable"):
            out.add(str(node["variable"]))
        for value in node.values():
            collect_condition_variable_ids(value, out)
    elif isinstance(node, list):
        for item in node:
            collect_condition_variable_ids(item, out)


def collect_step_variable_ids(
    steps: Any,
    rules: Dict[str, Any],
    scorecards: Dict[str, Any],
    decision_tables: Dict[str, Any],
    by_id: Dict[str, Any],
    out: Set[str],
    get_policy: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    seen: Optional[Set[str]] = None,
) -> None:
    """Every variable reachable from `steps`, INCLUDING nested ones.

    One branch per step type that can name a variable, and a recursive call for every type
    that can contain other steps. The recursion is the point: a rule inside a loop, or a
    decision table inside a branch arm, is just as reachable as one at the top level, and a
    conditional block is the normal way to express "only score this when X".

    `get_policy` resolves a sub-workflow's ref to its policy dict. Pass None to stop at the
    workflow boundary -- a caller without storage access (the serving path holds a bundle,
    not a session) then treats a sub-workflow as opaque.
    """
    if not isinstance(steps, list):
        return
    if seen is None:
        seen = set()

    for step in steps:
        if not isinstance(step, dict):
            continue
        step_type = step.get("type")
        ref = str(step.get("ref_id") or step.get("ref") or "")
        config = step.get("config") if isinstance(step.get("config"), dict) else {}

        if step_type == "rule" and ref in rules:
            rule = rules[ref]
            collect_condition_variable_ids(rule.get("tree"), out)
            collect_condition_variable_ids(rule.get("nodes"), out)
        elif step_type == "scorecard" and ref in scorecards:
            for factor in scorecards[ref].get("bins", []) or []:
                if isinstance(factor, dict) and factor.get("variable_id"):
                    out.add(str(factor["variable_id"]))
        elif step_type == "decision_table":
            # `inputs` is a LIST of {id, variable_id, ...}, not a map -- a first version
            # assumed a map and collected nothing, so the table's variables were never
            # computed and the policy fell through to its default outcome ('review' instead
            # of 'approve'). `_input_index` is decision_tables.py's own normaliser, reused
            # here so the two cannot disagree about the shape.
            #
            # The `or input_id` fallback matches _evaluate_row: with neither variable_id nor
            # variable set, the input's own id IS the variable reference.
            from .decision_tables import _input_index

            table = (decision_tables or {}).get(ref) or {}
            for input_id, spec in _input_index(table).items():
                spec = spec if isinstance(spec, dict) else {}
                out.add(str(spec.get("variable_id") or spec.get("variable") or input_id))
        elif step_type == "connector" and ref:
            # A connector step names a SOURCE, not a variable. Everything reading that source
            # is needed, because the step exists to supply exactly those.
            for vid, variable in by_id.items():
                if str(variable.get("source_id") or "") == ref:
                    out.add(vid)
        elif step_type == "loop":
            collect_step_variable_ids(config.get("steps"), rules, scorecards, decision_tables,
                                      by_id, out, get_policy, seen)
        elif step_type == "branch":
            for branch in config.get("branches", []) or []:
                if isinstance(branch, dict):
                    collect_step_variable_ids(branch.get("steps"), rules, scorecards,
                                              decision_tables, by_id, out, get_policy, seen)
                    collect_condition_variable_ids(branch.get("condition"), out)
            collect_step_variable_ids(config.get("default"), rules, scorecards, decision_tables,
                                      by_id, out, get_policy, seen)
            collect_condition_variable_ids(config.get("condition"), out)
        elif step_type == "workflow":
            # A sub-workflow's steps are as reachable as this policy's own. `seen` guards the
            # same cycle _execute_workflow guards at run time; here a cycle would hang during
            # payload assembly, before any budget check has run.
            if not ref or ref in seen or get_policy is None:
                continue
            seen.add(ref)
            try:
                child = get_policy(ref)
            except Exception:  # pragma: no cover - a missing child fails later, in the step
                child = None
            if child:
                collect_step_variable_ids(child.get("steps"), rules, scorecards, decision_tables,
                                          by_id, out, get_policy, seen)

        # Conditions can sit directly on any step (a guarded step), so check regardless of
        # type -- missing one silently drops the variable the guard reads.
        if config.get("condition") is not None:
            collect_condition_variable_ids(config.get("condition"), out)
        if step.get("condition") is not None:
            collect_condition_variable_ids(step.get("condition"), out)

        # STRING EXPRESSIONS, which the condition-node walk above cannot see. A loop's
        # `over` is `"variables.applicants"` and a branch condition is Python source like
        # `"app['score'] < 600"` -- both name variables as TEXT, not as a {type: condition}
        # node. Missing them left the loop test deciding on an empty collection and falling
        # through to its default outcome.
        #
        # Substring matching, deliberately: it is what the inter-variable dependency closure
        # in needed_variable_ids already does, and this module commits to being
        # over-inclusive. A false positive costs one sandbox call; a false negative changes
        # the decision.
        for text in (config.get("over"), config.get("condition"), step.get("condition")):
            if isinstance(text, str) and text:
                for vid in by_id:
                    if vid in text:
                        out.add(vid)
        for branch in (config.get("branches") or []):
            cond = branch.get("condition") if isinstance(branch, dict) else None
            if isinstance(cond, str) and cond:
                for vid in by_id:
                    if vid in cond:
                        out.add(vid)


def needed_variable_ids(
    policy: Dict[str, Any],
    rules: Dict[str, Any],
    scorecards: Dict[str, Any],
    decision_tables: Dict[str, Any],
    by_id: Dict[str, Any],
    get_policy: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Set[str]:
    """Variable ids the policy uses, closed over inter-variable dependencies.

    `by_id` maps variable id -> variable dict; only `code` (for the dependency closure) and
    `source_id` (for connector steps) are read, so any catalog shape works.

    The closure is what makes a filtered run safe when variables reference each other: if
    `dti` names `income` in its code, computing `dti` alone would fail. Substring matching
    on the code is over-inclusive on purpose -- see the module docstring.
    """
    direct: Set[str] = set()
    collect_step_variable_ids(policy.get("steps", []) or [], rules, scorecards, decision_tables,
                              by_id, direct, get_policy, set())

    needed: Set[str] = set()
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


def compute_only_used_vars() -> bool:
    """Is COMPUTE_ONLY_USED_VARS on?

    ONE reader, because both decision paths ask this question and two different answers to
    "is the flag on" is its own divergence -- the executor would filter while the serving
    path computed the whole catalog, or the reverse, for the same request.

    Strict `== "1"`, not `env.env_bool`, deliberately: FAST_DECIDE is read the same way
    (routers/runtime.py), these two flags are siblings, and a looser read could silently
    turn the flag ON for a deployment that set it to something env_bool considers truthy.
    For a flag that changes which variables get computed, failing closed is the safe side.
    """
    return os.getenv("COMPUTE_ONLY_USED_VARS", "0") == "1"
