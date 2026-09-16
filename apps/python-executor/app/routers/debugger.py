"""Step-through debugging for a policy run (parity P04).

WHAT EXISTED BEFORE. `ctx.step_trace` is POST-HOC -- it tells you what already happened, after the
decision is over -- and `debug_loop` re-evaluates a single loop step in isolation. Neither lets
someone walk a failing execution: stop before a step, look at the variables and outcome AS THEY
STOOD THERE, then advance one step and look again. That is the difference between reading a flight
recorder and flying the plane.

IT REUSES THE REAL EXECUTION PATH, and that is the whole design. The executor already knows how to
stop and continue -- `ctx.status = "paused"` + `paused_at_step` for review gates and async actions,
`_run_steps(start_index=…)` to resume, and `ExecutionContext.to_dict/from_dict` to survive the gap.
A debug-only execution path would be a second set of semantics to keep in agreement with the real
one, and a debugger whose answers differ from production is worse than no debugger. So stepping is
one field -- `PolicyExecutor.debug_stop_before` -- and everything else is the ordinary run.

SIMULATE=TRUE THROUGHOUT. Stepping a policy must not write a Decision row, meter a billable unit,
fire an outbound action or create a review task -- an operator debugging a failure would otherwise
bill the workspace once per step and spray a partner's endpoint with every click.
"""
from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import main
from ..context import get_current_api_key_id
from ..executor import ExecutionContext, PolicyExecutor
from ..main import active_role, active_tenant_id

router = APIRouter()

# In-process, and deliberately not a table. A debug session is scratch state for one person for a
# few minutes; persisting it would mean a migration, a retention policy and a cleanup job for
# something whose natural lifetime is shorter than a deploy. The cap stops a forgotten tab from
# growing the process.
_SESSIONS: Dict[str, Dict[str, Any]] = {}
MAX_SESSIONS = 50


class StartRequest(BaseModel):
    policy_id: str
    payload: Dict[str, Any] = {}


def _policy_or_404(policy_id: str, tenant_id: str) -> Dict[str, Any]:
    policy = main.storage.get_policy(policy_id, tenant_id=tenant_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found.")
    return policy


def _session_or_404(session_id: str, tenant_id: str) -> Dict[str, Any]:
    session = _SESSIONS.get(session_id)
    # The tenant check is the isolation boundary for this surface: sessions live in one process
    # dict, so without it any workspace could step another's execution and read its payload.
    if not session or session["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Debug session not found.")
    return session


def _audit(event_type: str, tenant_id: str, session: Dict[str, Any], detail: str) -> None:
    """Stepping writes no Decision row, which is exactly why the audit event matters.

    A simulate=True run is invisible to `/api/v1/audit/decisions`, so without this the only trace
    that someone walked a production policy's execution -- reading every payload field and every
    computed variable on the way -- is a process dict that dies with the deploy. The read is the
    sensitive part here, not a write.
    """
    try:
        main.storage.add_audit_event(
            {"tenant_id": tenant_id, "event_type": event_type, "entity_type": "debug_session",
             "entity_id": session["id"], "detail": detail,
             "metadata": {"actor_api_key_id": get_current_api_key_id(), "actor_role": active_role(),
                          "policy_id": session["policy_id"]}},
            tenant_id=tenant_id)
    except Exception:  # pragma: no cover - audit is best effort, a decision must not fail on it
        pass


def _snapshot(session: Dict[str, Any]) -> Dict[str, Any]:
    ctx: ExecutionContext = session["ctx"]
    steps: List[Dict[str, Any]] = session["steps"]
    at = ctx.paused_at_step if ctx.status == "paused" else None
    return {
        "session_id": session["id"],
        "policy_id": session["policy_id"],
        "status": ctx.status,
        "finished": ctx.status not in ("paused",),
        "step_count": len(steps),
        # Where the run is STOPPED, and what it will do next -- the two things a person stepping
        # through actually needs, and neither is derivable from a post-hoc trace.
        "paused_before_index": at,
        "next_step": steps[at] if at is not None and at < len(steps) else None,
        "outcome": ctx.outcome,
        # The context AS IT STANDS AT THIS STEP. A trace shows what a step produced; this shows
        # what the next step is about to see, which is where a wrong decision is actually explained.
        "variables": copy.deepcopy(ctx.variables),
        "rule_results": copy.deepcopy(ctx.rule_results),
        "scorecard_results": copy.deepcopy(getattr(ctx, "scorecard_results", None) or {}),
        "executed_steps": len(ctx.step_trace),
        "step_trace": copy.deepcopy(ctx.step_trace),
        "variable_errors": copy.deepcopy(getattr(ctx, "variable_errors", None) or {}),
    }


async def _advance(session: Dict[str, Any], stop_before: Optional[int]) -> None:
    """Run the policy up to `stop_before`, resuming from wherever it is paused."""
    executor = PolicyExecutor(main.storage)
    executor.debug_stop_before = stop_before
    # simulate=True below would otherwise seed every unsupplied field from the connectors' demo
    # payloads -- see PolicyExecutor.seed_samples_override. A debugger that invents inputs is
    # worse than no debugger: the missing field IS usually the bug.
    executor.seed_samples_override = False
    ctx: ExecutionContext = session["ctx"]
    resume_from = ctx if session["started"] else None
    result = await executor.execute(
        policy=session["policy"],
        payload=session["payload"],
        tenant_id=session["tenant_id"],
        resume_from=resume_from,
        source="loop_debug",
        # Never a real decision -- see the module docstring.
        simulate=True,
    )
    session["ctx"] = result
    session["started"] = True


@router.post("/api/v1/debug/sessions")
def start_session(body: StartRequest, request: Request) -> Dict[str, Any]:
    """Begin a debug run, stopped before the first step."""
    tenant_id = active_tenant_id(request)
    policy = _policy_or_404(body.policy_id, tenant_id)

    if len(_SESSIONS) >= MAX_SESSIONS:
        # Oldest first. A forgotten browser tab should cost the process one session, not all of
        # them, and refusing to start is a worse answer than retiring a stale one.
        for stale in list(_SESSIONS)[: max(1, len(_SESSIONS) - MAX_SESSIONS + 1)]:
            _SESSIONS.pop(stale, None)

    session: Dict[str, Any] = {
        "id": "dbg_" + uuid.uuid4().hex[:12],
        "tenant_id": tenant_id,
        "policy_id": body.policy_id,
        "policy": policy,
        "payload": copy.deepcopy(body.payload),
        "steps": policy.get("steps") or [],
        "ctx": None,
        "started": False,
    }
    _SESSIONS[session["id"]] = session
    asyncio.run(_advance(session, stop_before=0))
    _audit("debug_session_started", tenant_id, session,
           "Started a step-through debug run of policy {0}.".format(body.policy_id))
    return _snapshot(session)


@router.get("/api/v1/debug/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> Dict[str, Any]:
    return _snapshot(_session_or_404(session_id, active_tenant_id(request)))


@router.post("/api/v1/debug/sessions/{session_id}/step")
def step_session(session_id: str, request: Request) -> Dict[str, Any]:
    """Execute exactly one step and stop again."""
    tenant_id = active_tenant_id(request)
    session = _session_or_404(session_id, tenant_id)
    ctx: ExecutionContext = session["ctx"]
    if ctx.status != "paused":
        # Not an error: the run finished, and the caller is holding a final snapshot. Raising
        # would make a UI that polls have to special-case its own success.
        return _snapshot(session)
    asyncio.run(_advance(session, stop_before=(ctx.paused_at_step or 0) + 1))
    return _snapshot(session)


@router.post("/api/v1/debug/sessions/{session_id}/run")
def run_session(session_id: str, request: Request) -> Dict[str, Any]:
    """Let it run to completion from where it is paused."""
    tenant_id = active_tenant_id(request)
    session = _session_or_404(session_id, tenant_id)
    if session["ctx"].status != "paused":
        return _snapshot(session)
    asyncio.run(_advance(session, stop_before=None))
    return _snapshot(session)


@router.delete("/api/v1/debug/sessions/{session_id}")
def end_session(session_id: str, request: Request) -> Dict[str, Any]:
    _session_or_404(session_id, active_tenant_id(request))
    _SESSIONS.pop(session_id, None)
    return {"ended": True, "session_id": session_id}
