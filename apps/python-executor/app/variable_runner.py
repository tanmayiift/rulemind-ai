"""Computes a decision's variables -- the one loop both decision paths run (plan 4.4).

`PolicyExecutor._compute_variables` and `fast_decide._compute_variables` carried the same loop,
line for line, with comments in each saying it must stay identical to the other: skip what the
policy does not reach BEFORE the budget check, check the budget, clamp the next variable's
timeout to what is left, evaluate, record the value and any error. They now both call this.

In pool mode the variables go to the sandbox in ONE round trip (`sandbox.execute_variable_group`,
which documents the per-variable guarantees it keeps). Otherwise -- inline mode, SANDBOX_BATCH=0,
or a caller that hands in its own `execute` -- they go one call at a time, exactly as before.

`execute` is passed in rather than imported here so each path keeps its own seam: tests replace
`executor.execute_variable` / `fast_decide.execute_variable` to simulate slow, failing or
timed-out variables, and a replaced primitive is honoured call by call. The group trip applies
only to the real sandbox primitive, because batching is a property of how the sandbox runs code,
not of what a stand-in returns.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import sandbox

STAGE = "variable computation"


def compute_variables(variables: Iterable[Dict[str, Any]], payloads: Dict[str, Any], *,
                      execute: Callable[..., Dict[str, Any]], timeout_ms: int, memory_mb: int,
                      check_budget: Callable[[str], None], remaining_ms: Callable[[], Optional[float]],
                      needed_ids: Optional[set] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Returns (values, errors). Raises whatever `check_budget` raises when the budget is spent."""
    # Skip BEFORE any budget check: a variable that is never computed must not consume budget
    # either, or the two paths would abandon the run at different points for the same policy.
    selected: List[Dict[str, Any]] = [v for v in variables if needed_ids is None or v["id"] in needed_ids]
    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    def record(variable: Dict[str, Any], execution: Dict[str, Any]) -> None:
        values[variable["id"]] = execution.get("value")
        # A variable that errors becomes None; the error is kept so a None fed to a gate is never
        # mistaken for a real value (the decision then fails closed to review).
        if execution.get("error"):
            errors[variable["id"]] = str(execution.get("error"))

    resume_at = 0
    if selected and execute is sandbox.execute_variable and sandbox.batching_enabled():
        check_budget(STAGE)
        results = sandbox.execute_variable_group(
            [(v["id"], v["code"], v.get("source_id")) for v in selected],
            {source_id: payloads.get(source_id, {}) for source_id in {v.get("source_id") for v in selected}},
            timeout_ms=timeout_ms, memory_mb=memory_mb, budget_ms=remaining_ms())
        resume_at = len(selected)
        for index, (variable, result) in enumerate(zip(selected, results, strict=True)):
            if result.get("abandoned"):
                # The worker stopped when the budget ran out. The per-call loop below starts with
                # the budget check, which raises here as it always has -- and if the caller's
                # clock disagrees, the rest are simply computed one at a time.
                resume_at = index
                break
            record(variable, result)

    for variable in selected[resume_at:]:
        # Two-part enforcement: the check abandons the run between variables; the clamp stops
        # the NEXT variable overshooting the budget on its own -- without it a 2000 ms variable
        # started with 5 ms left would still run 2000 ms, and across 55 variables that compounds.
        check_budget(STAGE)
        remaining = remaining_ms()
        effective_timeout = timeout_ms if remaining is None else max(1, min(timeout_ms, remaining))
        record(variable, execute(variable["code"], payloads.get(variable.get("source_id"), {}), values,
                                 timeout_ms=effective_timeout, memory_mb=memory_mb))
    return values, errors
