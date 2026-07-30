from __future__ import annotations

import asyncio
import copy
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .executor import PolicyExecutor
from .storage import Storage

logger = logging.getLogger("rulemind.scheduler")

scheduler = AsyncIOScheduler()

# Leader election across replicas: every process renews a DB lease on a short
# interval; only the current lease holder actually runs the scheduled jobs, so a
# report/cron fires exactly once no matter how many pods are running.
_OWNER_ID = "{0}:{1}".format(socket.gethostname(), uuid.uuid4().hex[:8])
_LEASE_TTL_SECONDS = int(os.getenv("SCHEDULER_LEASE_TTL", "30"))
_LEASE_RENEW_SECONDS = max(5, _LEASE_TTL_SECONDS // 3)
_IS_LEADER = False


def is_leader() -> bool:
    return _IS_LEADER


def _renew_leadership(storage: Storage) -> None:
    global _IS_LEADER
    try:
        was_leader = _IS_LEADER
        _IS_LEADER = storage.try_acquire_scheduler_lease(_OWNER_ID, _LEASE_TTL_SECONDS)
        if _IS_LEADER and not was_leader:
            logger.info("scheduler: acquired leadership (%s)", _OWNER_ID)
        elif was_leader and not _IS_LEADER:
            logger.warning("scheduler: lost leadership (%s)", _OWNER_ID)
    except Exception as exc:  # never let a lease hiccup crash the loop
        logger.warning("scheduler: lease renewal failed: %s", exc)
        _IS_LEADER = False


# ── Leader-gated wrappers — used ONLY for scheduler-triggered runs, so a job
# fires on exactly one replica. Manual endpoints (run-now, send) call the
# underlying functions directly and are never gated. ──────────────────────────
async def _scheduled_cron(storage: Storage, schedule: Dict[str, Any]) -> Dict[str, Any]:
    if not _IS_LEADER:
        return {"skipped": "not_leader"}
    return await execute_cron_policy(storage, schedule)


async def _scheduled_report(storage: Storage, report_id: str, tenant_id: str) -> Dict[str, Any]:
    if not _IS_LEADER:
        return {"skipped": "not_leader"}
    return await deliver_scheduled_report(storage, report_id, tenant_id)


async def _scheduled_review_timeouts(storage: Storage) -> None:
    if not _IS_LEADER:
        return
    await check_review_timeouts(storage)


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


def _report_window_cutoff(report: Dict[str, Any]) -> Any:
    days = (report.get("filters") or {}).get("days")
    if not days:
        return None
    try:
        return datetime.utcnow() - timedelta(days=float(days))
    except (TypeError, ValueError):
        return None


def generate_report_for(storage: Storage, report: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
    """Fetch the report's decisions windowed + paginated (no silent 1000-row cap)
    and render it. Returns the report result with a `truncated` flag."""
    from .reports import generate_report

    decisions, truncated = storage.iter_decisions_window(
        tenant_id=tenant_id, since=_report_window_cutoff(report),
        max_rows=int(os.getenv("REPORT_MAX_ROWS", "100000")),
    )
    result = generate_report(report, decisions)
    result["truncated"] = truncated
    return result


async def deliver_scheduled_report(storage: Storage, report_id: str, tenant_id: str) -> Dict[str, Any]:
    """Generate a scheduled report and email it (CSV attached). A failed or
    unconfigured send is enqueued to the durable outbox for retry, never dropped."""
    report = storage.get_report(report_id, tenant_id=tenant_id)
    if not report:
        return {"delivered": False, "error": "report not found"}
    result = generate_report_for(storage, report, tenant_id)
    recipients = (report.get("schedule") or {}).get("recipients") or []
    delivery = deliver_report_result(storage, tenant_id, report, result, recipients)
    storage.update_report(report_id, {"last_run": {
        "generated_at": result["generated_at"], "row_count": result["row_count"],
        "truncated": result.get("truncated", False), "delivery": delivery,
    }}, tenant_id=tenant_id)
    return delivery


def deliver_report_result(storage: Storage, tenant_id: str, report: Dict[str, Any],
                           result: Dict[str, Any], recipients: List[str]) -> Dict[str, Any]:
    """Attempt delivery; on failure/unconfigured, enqueue to the durable outbox.
    Emits an audit event either way. Shared by the scheduled + manual paths."""
    from .mailer import send_report_email

    subject = "[RuleMind] {0} — {1} rows".format(report["name"], result["row_count"])
    body = 'Your report "{0}" is attached ({1} rows, timezone {2}).'.format(
        report["name"], result["row_count"], result["timezone"])
    filename = "{0}.csv".format(report["id"])
    delivery = send_report_email(storage.get_email_credentials(tenant_id=tenant_id), recipients,
                                 subject=subject, body=body, csv_content=result["csv"], csv_filename=filename)
    if not delivery.get("delivered") and delivery.get("transport") in {"unconfigured", "failed"} and recipients:
        queued = storage.enqueue_email({
            "tenant_id": tenant_id, "recipients": recipients, "subject": subject, "body": body,
            "csv_content": result["csv"], "csv_filename": filename,
            "attempts": 1 if delivery["transport"] == "failed" else 0,
            "last_error": delivery.get("error"),
        }, tenant_id=tenant_id)
        delivery["outbox_id"] = queued["id"]
    try:
        storage.add_audit_event({
            "tenant_id": tenant_id, "event_type": "report_delivered",
            "entity_type": "report", "entity_id": report["id"],
            "detail": "Report '{0}' → {1} ({2})".format(report["name"], delivery.get("transport"), len(recipients)),
            "metadata": {"row_count": result["row_count"], "delivered": delivery.get("delivered"),
                         "transport": delivery.get("transport"), "truncated": result.get("truncated", False)},
        }, tenant_id=tenant_id)
    except Exception:  # pragma: no cover - audit best effort
        pass
    return delivery


async def retry_outbox(storage: Storage, max_attempts: int = 5) -> Dict[str, Any]:
    """Leader-gated: drain the durable email outbox. Each pending message is retried
    until it sends or hits max_attempts (then marked failed for operator attention)."""
    if not _IS_LEADER:
        return {"skipped": "not_leader"}
    from .mailer import send_report_email

    sent = failed = 0
    for msg in storage.list_pending_emails(max_attempts=max_attempts):
        creds = storage.get_email_credentials(tenant_id=msg["tenant_id"])
        delivery = send_report_email(creds, msg["recipients"], msg["subject"], msg["body"],
                                     msg["csv_content"], msg["csv_filename"])
        if delivery.get("delivered"):
            storage.mark_email(msg["id"], "sent")
            sent += 1
        else:
            attempts = msg["attempts"] + 1
            status = "failed" if attempts >= max_attempts else "pending"
            storage.mark_email(msg["id"], status, error=delivery.get("error") or delivery.get("note"), increment_attempt=True)
            failed += 1
    return {"sent": sent, "retried": failed}


def unschedule_report_job(tenant_id: str, report_id: str) -> None:
    """Remove a report's delivery job (called on delete so it stops firing)."""
    if not scheduler.running:
        return
    job_id = "report:{0}:{1}".format(tenant_id or "", report_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def schedule_report_job(storage: Storage, report: Dict[str, Any]) -> None:
    """(Re)register a report's delivery job from its schedule. Safe to call at
    runtime on create/update; a disabled/blank schedule removes the job."""
    if not scheduler.running:
        return
    job_id = "report:{0}:{1}".format(report.get("tenant_id", ""), report["id"])
    scheduler.remove_job(job_id) if scheduler.get_job(job_id) else None
    sched = report.get("schedule") or {}
    if not (sched.get("enabled") and sched.get("cron")):
        return
    try:
        trigger = CronTrigger.from_crontab(sched["cron"], timezone=report.get("timezone") or "UTC")
    except Exception:
        return
    scheduler.add_job(
        _scheduled_report, trigger, id=job_id,
        args=[storage, report["id"], report.get("tenant_id", "")], replace_existing=True,
    )


def init_scheduler(storage: Storage) -> None:
    if scheduler.running:
        return
    # Establish leadership synchronously at boot, then renew on an interval so
    # exactly one replica runs the jobs below (all replicas register them).
    _renew_leadership(storage)
    scheduler.add_job(_renew_leadership, "interval", seconds=_LEASE_RENEW_SECONDS,
                      id="scheduler-leader-renew", args=[storage], replace_existing=True)
    for schedule in storage.list_active_schedules():
        scheduler.add_job(
            _scheduled_cron,
            CronTrigger.from_crontab(schedule["cron_expression"]),
            id=schedule["id"],
            args=[storage, schedule],
            replace_existing=True,
        )
    for report in storage.list_scheduled_reports():
        sched = report.get("schedule") or {}
        try:
            trigger = CronTrigger.from_crontab(sched["cron"], timezone=report.get("timezone") or "UTC")
        except Exception:
            continue
        scheduler.add_job(
            _scheduled_report, trigger,
            id="report:{0}:{1}".format(report.get("tenant_id", ""), report["id"]),
            args=[storage, report["id"], report.get("tenant_id", "")], replace_existing=True,
        )
    scheduler.add_job(_scheduled_review_timeouts, "interval", minutes=5, id="review-timeouts", args=[storage], replace_existing=True)
    scheduler.add_job(retry_outbox, "interval", seconds=int(os.getenv("OUTBOX_RETRY_SECONDS", "60")),
                      id="email-outbox-retry", args=[storage], replace_existing=True)
    scheduler.start()
