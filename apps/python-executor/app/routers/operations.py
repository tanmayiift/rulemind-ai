"""Operations endpoints — webhooks, schedules, and human-review queues. Extracted verbatim from
app/main.py. Stable helpers/models imported by value from app.main; direct storage calls use
main.storage live."""
from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .. import main
from ..main import (
    ReviewDecisionRequest,
    ScheduleUpsertRequest,
    WebhookUpsertRequest,
)
from ..reviews import submit_review_decision
from ..scheduler import execute_cron_policy
from ..webhooks import WebhookAuthenticationError

router = APIRouter()


# ── Webhooks ───────────────────────────────────────────────────────────────
@router.get("/api/v1/webhooks")
def list_webhooks() -> List[Dict[str, Any]]:
    return main.storage.list_webhooks()


@router.get("/api/v1/webhooks/{webhook_id}")
def get_webhook(webhook_id: str) -> Dict[str, Any]:
    webhook = main.storage.get_webhook(webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return webhook


@router.post("/api/v1/webhooks")
def create_webhook(request: WebhookUpsertRequest) -> Dict[str, Any]:
    import secrets as _secrets

    endpoint_id = "wh_" + uuid.uuid4().hex[:12]
    # Always require a signing secret so an inbound webhook is HMAC-authenticated —
    # auto-generate one when the caller doesn't supply it (returned once here).
    secret = request.secret or _secrets.token_urlsafe(32)
    created = main.storage.create_webhook(
        {
            "id": endpoint_id,
            "policy_id": request.policy_id,
            "endpoint_path": "/api/v1/webhooks/{0}".format(endpoint_id),
            "is_active": request.is_active,
            "secret_hash": secret,
            "payload_mapping": request.payload_mapping,
        }
    )
    created["secret"] = secret  # shown once — the caller signs requests with it (x-webhook-signature)
    return created


@router.put("/api/v1/webhooks/{webhook_id}")
def update_webhook(webhook_id: str, request: WebhookUpsertRequest) -> Dict[str, Any]:
    updated = main.storage.update_webhook(
        webhook_id,
        {
            "policy_id": request.policy_id,
            "is_active": request.is_active,
            "secret_hash": request.secret,
            "payload_mapping": request.payload_mapping,
        },
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return updated


@router.delete("/api/v1/webhooks/{webhook_id}")
def delete_webhook(webhook_id: str) -> Dict[str, bool]:
    updated = main.storage.update_webhook(webhook_id, {"is_active": False})
    if not updated:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return {"deactivated": True}


@router.get("/api/v1/webhooks/{webhook_id}/test")
def test_webhook(webhook_id: str) -> Dict[str, str]:
    webhook = main.storage.get_webhook(webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return {
        "url": webhook["endpoint_path"],
        "curl": "curl -X POST http://localhost:8080{0} -H 'Content-Type: application/json' -d '{{}}'".format(webhook["endpoint_path"]),
    }


@router.post("/api/v1/webhooks/{webhook_id}")
async def webhook_trigger(webhook_id: str, request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from error
    try:
        # main.trigger_webhook (re-exported from app.webhooks) is read live so a test patching
        # app.main.trigger_webhook still intercepts this call after the endpoint moved to a router.
        return await main.trigger_webhook(main.storage, webhook_id, body, signature=request.headers.get("x-webhook-signature"))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WebhookAuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except PermissionError as error:
        # The tenant comes from the WEBHOOK RECORD, not from active_tenant_id(). This receiver is
        # unauthenticated by design (a third party posts to it with an HMAC signature), so there
        # is no auth context -- active_tenant_id() was silently resolving to the FIRST tenant,
        # which means on a multi-tenant deployment a failure for tenant B was filed as tenant A's
        # error event. trigger_webhook itself already reads webhook["tenant_id"] everywhere; only
        # this error path did not.
        webhook = main.storage.get_webhook_for_trigger(webhook_id)
        tenant_id = (webhook or {}).get("tenant_id")
        if tenant_id:
            main.storage.add_error_event(
                {
                    "tenant_id": tenant_id,
                    "scope": "webhook",
                    "entity_type": "workflow_execution",
                    "entity_id": webhook_id,
                    "stage": "execute",
                    "message": str(error),
                    "details": {"trigger": "webhook"},
                },
                tenant_id=tenant_id,
            )
        raise HTTPException(status_code=500, detail="Webhook execution failed.") from error


# ── Schedules ──────────────────────────────────────────────────────────────
@router.get("/api/v1/schedules")
def list_schedules() -> List[Dict[str, Any]]:
    return main.storage.list_schedules()


@router.get("/api/v1/schedules/{schedule_id}")
def get_schedule(schedule_id: str) -> Dict[str, Any]:
    schedule = main.storage.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return schedule


@router.post("/api/v1/schedules")
def create_schedule(request: ScheduleUpsertRequest) -> Dict[str, Any]:
    return main.storage.create_schedule(request.model_dump())


@router.put("/api/v1/schedules/{schedule_id}")
def update_schedule(schedule_id: str, request: ScheduleUpsertRequest) -> Dict[str, Any]:
    updated = main.storage.update_schedule(schedule_id, request.model_dump())
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return updated


@router.delete("/api/v1/schedules/{schedule_id}")
def delete_schedule(schedule_id: str) -> Dict[str, bool]:
    updated = main.storage.update_schedule(schedule_id, {"is_active": False})
    if not updated:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return {"deactivated": True}


@router.post("/api/v1/schedules/{schedule_id}/run-now")
def run_schedule_now(schedule_id: str) -> Dict[str, Any]:
    schedule = main.storage.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    result = asyncio.run(execute_cron_policy(main.storage, schedule))
    return result


@router.get("/api/v1/schedules/{schedule_id}/history")
def schedule_history(schedule_id: str) -> List[Dict[str, Any]]:
    return [
        event
        for event in main.storage.list_audit_events(event_type="cron_executed")
        if event.get("metadata", {}).get("schedule_id") == schedule_id
    ]


# ── Human review queues ────────────────────────────────────────────────────
@router.get("/api/v1/reviews")
def list_reviews(queue: Optional[str] = Query(default=None), status: Optional[str] = Query(default=None)) -> List[Dict[str, Any]]:
    return main.storage.list_review_tasks(queue=queue, status=status)


@router.get("/api/v1/reviews/stats")
def review_stats() -> Dict[str, Any]:
    tasks = main.storage.list_review_tasks()
    pending = [task for task in tasks if task["status"] == "pending"]
    reviewed = [task for task in tasks if task["status"] in {"approved", "rejected", "timed_out"}]
    return {"pending": len(pending), "reviewed": len(reviewed), "queues": {queue: len([task for task in tasks if task["queue"] == queue]) for queue in {task["queue"] for task in tasks}}}


@router.get("/api/v1/reviews/{task_id}")
def get_review(task_id: str) -> Dict[str, Any]:
    task = main.storage.get_review_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found.")
    return task


@router.post("/api/v1/reviews/{task_id}/decide")
def decide_review(task_id: str, request: ReviewDecisionRequest) -> Dict[str, Any]:
    try:
        result = submit_review_decision(main.storage, task_id, request.response, request.reviewer_id, request.decision)
    except ValueError as error:
        detail = str(error)
        status_code = 422 if detail.startswith("Missing required field") else 404
        raise HTTPException(status_code=status_code, detail=detail) from error
    return {"executionId": result["execution"]["execution_id"], "outcome": result["execution"]["outcome"], "status": result["execution"]["status"]}


@router.post("/api/v1/reviews/{task_id}/escalate")
def escalate_review(task_id: str, request: ReviewDecisionRequest) -> Dict[str, Any]:
    task = main.storage.get_review_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Review task not found.")
    updated = main.storage.update_review_task(task_id, {"status": "escalated", "reviewer_response": request.response, "reviewed_by": request.reviewer_id})
    return updated or task


# ── Executions ────────────────────────────────────────────────────────────────────────────
# NONE OF THIS EXISTED. A multi-step policy that pauses -- on a `review_gate`, or because an
# SDK client resumes it at the edge -- writes a durable `workflow_executions` row, and the only
# ways to reach one were `GET /sdk/v1/executions/{id}` and its resume twin: both need the id you
# are trying to find. There was no list, no count, no screen. An execution that paused and was
# never resumed was not merely un-resumed, it was INVISIBLE -- you could not have discovered it
# existed. "Nothing ever resumes a paused execution" was not even an observable claim.


@router.get("/api/v1/executions")
def list_executions(request: Request,
                    status: Optional[str] = None,
                    policy_id: Optional[str] = None,
                    paused_before: Optional[str] = None,
                    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
                    offset: Annotated[int, Query(ge=0)] = 0) -> List[Dict[str, Any]]:
    """Executions for this tenant, newest first.

    `paused_before` (ISO-8601) is the operational query: "everything sitting paused since
    before X". That is what an operator runs when the review queue is empty and work is still
    stuck behind tasks that were deleted or expired.

    The execution CONTEXT is omitted here on purpose -- it carries the full decision payload and
    every computed variable, so 200 of them is megabytes of PII in a list view. Fetch one with
    `GET /sdk/v1/executions/{id}`.
    """
    return main.storage.list_workflow_executions(
        tenant_id=main.active_tenant_id(request), status=status, policy_id=policy_id,
        paused_before=paused_before, limit=limit, offset=offset)


@router.get("/api/v1/executions/stuck")
def list_stuck_executions(request: Request,
                          older_than_minutes: Annotated[int, Query(ge=1, le=100_000)] = 1440,
                          limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> Dict[str, Any]:
    """Paused executions with NO open review task -- the genuinely orphaned ones.

    A paused execution waiting on a human is not stuck, it is waiting; the review queue has its
    own timeout job for those. What has no owner at all is an execution whose review task was
    deleted, or which paused for a reason no queue tracks. Nothing resumes those, ever, and
    until this endpoint nothing could even name them.

    Read-only by design. The scheduler job that reports these does not expire them either --
    see `sweep_stuck_executions`.
    """
    from ..scheduler import find_stuck_executions

    # The same function the sweep calls -- see its docstring for why there is only one.
    found = find_stuck_executions(main.storage, main.active_tenant_id(request),
                                  older_than_minutes, limit=limit)
    return {"older_than_minutes": older_than_minutes, **found}
