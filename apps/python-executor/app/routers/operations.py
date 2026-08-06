"""Operations endpoints — webhooks, schedules, and human-review queues. Extracted verbatim from
app/main.py. Stable helpers/models imported by value from app.main; direct storage calls use
main.storage live."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .. import main
from ..main import (
    ReviewDecisionRequest,
    ScheduleUpsertRequest,
    WebhookUpsertRequest,
    active_tenant_id,
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
        main.storage.add_error_event(
            {
                "tenant_id": active_tenant_id(),
                "scope": "webhook",
                "entity_type": "workflow_execution",
                "entity_id": webhook_id,
                "stage": "execute",
                "message": str(error),
                "details": {"trigger": "webhook"},
            },
            tenant_id=active_tenant_id(),
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
