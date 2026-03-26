from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from typing import Any, Dict, List

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .executor import PolicyExecutor
from .storage import Storage


scheduler = AsyncIOScheduler()


async def fetch_batch_items(payload_source: Dict[str, Any], tenant_id: str) -> List[Dict[str, Any]]:
    source_type = (payload_source or {}).get("type", "static_json")
    if source_type == "static_json":
        items = (payload_source or {}).get("items", [])
        return [copy.deepcopy(item) for item in items if isinstance(item, dict)]
    if source_type == "http_pull":
        config = payload_source or {}
        async with httpx.AsyncClient(timeout=float(config.get("timeout", 5))) as client:
            method = str(config.get("method", "GET")).upper()
            headers = config.get("headers", {})
            if method == "POST":
                response = await client.post(str(config.get("url")), json=config.get("body", {}), headers=headers)
            else:
                response = await client.get(str(config.get("url")), headers=headers)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return [item for item in data.get("items", []) if isinstance(item, dict)]
            return []
    return []


async def execute_cron_policy(storage: Storage, schedule: Dict[str, Any]) -> Dict[str, Any]:
    tenant_id = schedule["tenant_id"]
    policy = storage.get_policy(schedule["policy_id"], tenant_id=tenant_id)
    if not policy:
        raise ValueError("Policy not found")
    items = await fetch_batch_items(schedule.get("payload_source") or {}, tenant_id)
    config = schedule.get("config") or {}
    batch_size = int(config.get("batchSize", 100))
    concurrency = int(config.get("concurrency", 5))
    semaphore = asyncio.Semaphore(concurrency)
    executor = PolicyExecutor(storage)

    async def process_item(item: Dict[str, Any]) -> Dict[str, Any]:
        async with semaphore:
            result = await executor.execute(policy=policy, payload=item, tenant_id=tenant_id, source="cron")
            return {"item_id": item.get("id"), "outcome": result.outcome, "latency_ms": result.total_latency_ms}

    results = await asyncio.gather(*[process_item(item) for item in items[:batch_size]], return_exceptions=True)
    storage.update_schedule(schedule["id"], {"last_run_at": datetime.utcnow()}, tenant_id=tenant_id)
    successes = sum(1 for item in results if not isinstance(item, Exception))
    storage.add_audit_event(
        {
            "tenant_id": tenant_id,
            "event_type": "cron_executed",
            "entity_type": "policy",
            "entity_id": schedule["policy_id"],
            "detail": "Cron executed {0}/{1} items".format(successes, len(results)),
            "metadata": {"schedule_id": schedule["id"], "total": len(results), "successes": successes},
        },
        tenant_id=tenant_id,
    )
    return {"results": results, "successes": successes, "total": len(results)}


async def check_review_timeouts(storage: Storage) -> None:
    expired = storage.get_expired_review_tasks()
    for task in expired:
        execution = storage.get_workflow_execution(task["execution_id"], tenant_id=task["tenant_id"])
        if not execution:
            continue
        policy = storage.get_policy(execution["policy_id"], tenant_id=task["tenant_id"])
        if not policy:
            continue
        step = next((item for item in policy.get("steps", []) if item.get("id") == task.get("step_id")), None)
        on_timeout = ((step or {}).get("config") or {}).get("onTimeout", "reject")
        storage.update_review_task(
            task["id"],
            {
                "status": "timed_out",
                "reviewer_response": {"auto_resolved": True, "reason": "timeout"},
                "reviewed_at": datetime.utcnow(),
            },
            tenant_id=task["tenant_id"],
        )
        from .executor import ExecutionContext

        ctx = ExecutionContext.from_dict(execution["context"])
        ctx.outcome = str(on_timeout)
        ctx.current_step_index = int(ctx.paused_at_step or 0) + 1
        executor = PolicyExecutor(storage)
        await executor.execute(policy=policy, payload=ctx.payload, tenant_id=task["tenant_id"], resume_from=ctx, source="review")


def init_scheduler(storage: Storage) -> None:
    if scheduler.running:
        return
    for schedule in storage.list_active_schedules():
        scheduler.add_job(
            execute_cron_policy,
            CronTrigger.from_crontab(schedule["cron_expression"]),
            id=schedule["id"],
            args=[storage, schedule],
            replace_existing=True,
        )
    scheduler.add_job(check_review_timeouts, "interval", minutes=5, id="review-timeouts", args=[storage], replace_existing=True)
    scheduler.start()
