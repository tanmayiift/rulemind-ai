from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict

from .executor import ExecutionContext, PolicyExecutor
from .storage import Storage


def submit_review_decision(storage: Storage, task_id: str, response: Dict[str, Any], reviewer_id: str, decision: str) -> Dict[str, Any]:
    task = storage.get_review_task(task_id)
    if not task or task.get("status") != "pending":
        raise ValueError("Task not found or already reviewed")
    for field in task.get("required_fields", []):
        if field not in response:
            raise ValueError("Missing required field: {0}".format(field))
    updated = storage.update_review_task(
        task_id,
        {
            "status": "approved" if decision == "approve" else "rejected",
            "reviewer_response": copy.deepcopy(response),
            "reviewed_by": reviewer_id,
            "reviewed_at": datetime.utcnow(),
        },
        tenant_id=task["tenant_id"],
    )
    execution = storage.get_workflow_execution(task["execution_id"], tenant_id=task["tenant_id"])
    if not execution:
        raise ValueError("Workflow execution not found")
    ctx = ExecutionContext.from_dict(execution["context"])
    ctx.outcome = "approve" if decision == "approve" else "reject"
    ctx.review_response = copy.deepcopy(response)
    ctx.variables.update(copy.deepcopy(response))
    ctx.current_step_index = int(ctx.paused_at_step or 0) + 1
    executor = PolicyExecutor(storage)
    policy = storage.get_policy(ctx.policy_id, tenant_id=task["tenant_id"])
    if not policy:
        raise ValueError("Policy not found")
    result = executor.execute(policy=policy, payload=ctx.payload, tenant_id=task["tenant_id"], resume_from=ctx, source="review")
    if hasattr(result, "__await__"):
        import asyncio

        result = asyncio.run(result)
    storage.add_audit_event(
        {
            "tenant_id": task["tenant_id"],
            "event_type": "review_completed",
            "entity_type": "review_task",
            "entity_id": task_id,
            "detail": "Review {0} by {1}".format(decision, reviewer_id),
            "metadata": {"execution_id": task["execution_id"], "decision": decision},
        },
        tenant_id=task["tenant_id"],
    )
    return {"task": updated, "execution": result.to_dict()}
