from __future__ import annotations

import asyncio
import copy
import logging
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .executor import PolicyExecutor
from .storage import Storage
from . import env

logger = logging.getLogger("rulemind.scheduler")

scheduler = AsyncIOScheduler()

# Leader election across replicas: every process renews a DB lease on a short
# interval; only the current lease holder actually runs the scheduled jobs, so a
# report/cron fires exactly once no matter how many pods are running.
_OWNER_ID = "{0}:{1}".format(socket.gethostname(), uuid.uuid4().hex[:8])
_LEASE_TTL_SECONDS = env.env_int("SCHEDULER_LEASE_TTL", 30)
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


def archive_old_decisions(storage: Storage) -> Dict[str, Any]:
    """Per tenant, archive decisions older than its retention window to the configured OLAP sink
    and purge them from the hot DB. No-op unless a sink is configured (DECISION_ARCHIVE_SINK).
    Bounds the transactional decision log while preserving history in cheap analytical storage."""
    from .archiver import archiving_enabled, get_archiver, retention_cutoff

    if not archiving_enabled():
        # SILENT BEFORE THIS. DECISION_ARCHIVE_SINK defaults to "none", so on a default
        # deployment this job returned early on every run and no decision was ever purged --
        # while the settings screen showed "Audit Retention Days: 90"
        # (experience_studio.py:219) and the workspace row said 90 (models.py:745).
        #
        # Two consequences, and the second is worse than the first:
        #   * the decisions table grows without bound. Measured on the soak stack: 13.2 KB per
        #     decision (trace 6.1 KB, rule_results 2.5 KB, computed_variables 1.6 KB,
        #     scorecard_result 1.1 KB), so 1M decisions/day is ~13 GB/day, forever.
        #   * a workspace that set a retention window believes data older than it is gone. It is
        #     not. For a decisioning engine holding bureau data, that is a false compliance
        #     statement, not just a disk problem.
        #
        # Archiving and retention are different jobs -- copy-to-OLAP is optional, honouring the
        # window the UI advertises is not -- but changing the default to delete would silently
        # destroy data on upgrade. So this stays a no-op and says so, loudly, naming the exact
        # mismatch, once per scheduled run.
        configured = [t.get("id") for t in storage.list_tenants()
                      if t.get("id") and retention_cutoff(
                          storage.get_settings(tenant_id=t["id"])) is not None]
        if configured:
            logger.warning(
                "RETENTION IS ADVERTISED BUT NOT ENFORCED: %d workspace(s) have a retention "
                "window configured, and DECISION_ARCHIVE_SINK is 'none', so archive_old_decisions "
                "purges nothing. Decisions are kept forever and the settings screen says "
                "otherwise. Configure a sink, or clear the retention setting so the UI stops "
                "promising it.", len(configured))
        return {"skipped": "archiving_disabled", "retention_configured_tenants": len(configured)}
    archiver = get_archiver()
    total = 0
    tenants = 0
    for tenant in storage.list_tenants():
        tenant_id = tenant.get("id")
        if not tenant_id:
            continue
        # Months when the workspace configured months, days otherwise, None when retention is
        # off — resolved in one place so the purge can never disagree with what the settings
        # screen shows. A calendar month is not 30 days, and this is the job that acts on the
        # difference: getting it wrong here deletes decisions the workspace promised to keep.
        cutoff = retention_cutoff(storage.get_settings(tenant_id=tenant_id))
        if cutoff is None:
            continue  # retention disabled for this workspace
        try:
            total += storage.archive_and_purge_decisions(tenant_id, cutoff, archiver)
            tenants += 1
        except Exception as exc:  # one tenant/sink failure must not stop the others
            logger.warning("archive: tenant %s failed: %s", tenant_id, exc)
    return {"archived": total, "tenants": tenants, "sink": archiver.name}


async def _scheduled_prune_saml_assertions(storage: Storage) -> Dict[str, Any]:
    """Drop consumed SAML assertions whose validity window has passed, across every tenant.

    `storage.prune_saml_assertions` existed and had a test, but nothing ever called it in
    production. The table it prunes gets one row per successful SSO login and is never read
    except to reject a replay, so on any SSO-enabled deployment it grows forever -- and the
    replay check is a lookup against it on every single login, so the cost lands on the hot
    authentication path rather than showing up as a slow query somewhere ignorable.

    Per tenant, because prune_saml_assertions is tenant-scoped like every other storage call.
    """
    if not _IS_LEADER:
        return {"skipped": "not_leader"}

    def _prune() -> Dict[str, Any]:
        pruned = 0
        for tenant in storage.list_tenants():
            tenant_id = tenant.get("id")
            if not tenant_id:
                continue
            try:
                pruned += storage.prune_saml_assertions(tenant_id=tenant_id)
            except Exception as exc:  # noqa: BLE001 - one bad tenant must not stop the sweep
                logger.warning("saml prune: tenant %s failed: %s", tenant_id, exc)
        return {"pruned": pruned}

    return await asyncio.to_thread(_prune)


async def _scheduled_archive_decisions(storage: Storage) -> Dict[str, Any]:
    if not _IS_LEADER:
        return {"skipped": "not_leader"}
    # DB + archive I/O off the event loop.
    return await asyncio.to_thread(archive_old_decisions, storage)


def check_slos(storage: Storage) -> Dict[str, Any]:
    """Per tenant, evaluate the SLO + outcome-drift guard, publish Prometheus gauges, and
    record a durable ``slo_breach`` audit event when the active breach set changes (so an
    on-call trail exists without re-alerting every interval on the same breach)."""
    from . import slo

    checked = 0
    breached = 0
    for tenant in storage.list_tenants():
        tenant_id = tenant.get("id")
        if not tenant_id:
            continue
        try:
            report = slo.evaluate_slo(storage, tenant_id=tenant_id)
        except Exception as exc:  # one tenant must not stop the sweep
            logger.warning("slo: tenant %s evaluation failed: %s", tenant_id, exc)
            continue
        checked += 1
        slo.record_prometheus(tenant_id, report)
        if not report.get("enabled"):
            continue
        active_types = sorted(b.get("type") for b in report.get("breaches", []))
        # Only write a new event when the breach set transitions (fires on new breaches and
        # on recovery), so the audit trail is signal, not one row per interval.
        try:
            prior = storage.list_audit_events(tenant_id=tenant_id, event_type="slo_breach")
            last_types = sorted((prior[0].get("metadata", {}) or {}).get("breach_types", [])) if prior else []
        except Exception:
            last_types = []
        if active_types == last_types:
            if active_types:
                breached += 1
            continue
        if active_types:
            breached += 1
        detail = ("SLO breached: " + ", ".join(active_types)) if active_types else "SLO recovered"
        try:
            storage.add_audit_event({
                "event_type": "slo_breach",
                "entity_type": "slo",
                "detail": detail,
                "metadata": {
                    "breach_types": active_types,
                    "breaches": report.get("breaches", []),
                    "metrics": report.get("metrics", {}),
                    "drift": report.get("drift", {}),
                    "healthy": report.get("healthy", True),
                },
            }, tenant_id=tenant_id)
        except Exception as exc:  # audit write failure must not stop the sweep
            logger.warning("slo: tenant %s audit write failed: %s", tenant_id, exc)
        if active_types:
            logger.warning("slo: tenant %s %s", tenant_id, detail)
    return {"checked": checked, "breached": breached}


def auto_rollback_experiments(storage: Storage) -> Dict[str, Any]:
    """Halt a running experiment whose challenger has breached its guardrails.

    THE GAP THIS CLOSES. `champion_challenger.analyze_champion_challenger` has computed a
    promote / hold / ROLLBACK recommendation for a while, and `routers/experiments.py` uses it --
    but only to REFUSE a promotion a human asked for. Nothing acted on "rollback" on its own, so
    a challenger that started rejecting everyone kept taking its share of live traffic until
    somebody happened to open the analytics page. The row's e2eFlow is "promote at 5% canary ->
    watch the metric -> AUTO-ROLLBACK on SLO breach"; the auto half was the missing word.

    ONLY ON A GUARDRAIL BREACH, never on "the challenger is merely losing". A guardrail is an
    explicit statement by the author that crossing this line is unacceptable -- a floor on
    approval rate, a ceiling on rejects or latency. Underperforming on lift is a business
    judgement and stays a human's call; `recommend_action` returns "rollback" for a negative lift
    too, and acting on that would stop experiments for doing exactly what experiments are for.

    Stopping means status -> "stopped", which `experiments.resolve_experiment_assignment` already
    filters on: it selects `status == "running"`, so a stopped experiment stops receiving traffic
    on the very next decision without any separate teardown. The champion serves 100% again
    because it is what remains.

    Leader-gated like every other job here: N replicas each halting the same experiment would
    write the same row N times and emit N alerts.
    """
    if not _IS_LEADER:
        return {"skipped": "not_leader"}
    from .analytics import experiment_analytics

    stopped: List[Dict[str, Any]] = []
    checked = 0
    for tenant in storage.list_tenants():
        tenant_id = tenant.get("id")
        if not tenant_id:
            continue
        try:
            experiments = [e for e in storage.list_experiments(tenant_id=tenant_id)
                           if e.get("status") == "running"]
        except Exception:  # pragma: no cover - one tenant must not stop the sweep
            logger.warning("could not list experiments for %s", tenant_id, exc_info=True)
            continue
        for experiment in experiments:
            checked += 1
            try:
                analysis = experiment_analytics(storage, tenant_id, experiment["id"])
            except Exception:  # pragma: no cover
                logger.warning("could not analyse experiment %s", experiment.get("id"), exc_info=True)
                continue
            breached = [
                row for row in (analysis.get("championChallenger") or {}).get("challengers", [])
                if row.get("recommendation") == "rollback"
                and (row.get("guardrails") or {}).get("breached")
            ]
            if not breached:
                continue
            reasons = sorted({reason for row in breached
                              for reason in (row.get("guardrails") or {}).get("breaches", [])})
            try:
                storage.create_or_update_experiment(
                    {**experiment, "id": experiment["id"], "status": "stopped"},
                    tenant_id=tenant_id)
            except Exception:  # pragma: no cover
                logger.error("could not stop experiment %s", experiment.get("id"), exc_info=True)
                continue
            # Audited, not just logged: halting an experiment changes what live traffic sees, and
            # an operator arriving later needs to know it happened, when, and on which guardrail.
            # Audit events also feed the outbound webhook stream, so a subscriber can be told.
            try:
                storage.add_audit_event(
                    {"tenant_id": tenant_id, "event_type": "experiment_promoted",
                     "entity_type": "experiment", "entity_id": experiment["id"],
                     "detail": "Auto-rolled back '{0}': {1}".format(
                         experiment.get("name") or experiment["id"], "; ".join(reasons) or "guardrail breach"),
                     "metadata": {"action": "auto_rollback", "breaches": reasons,
                                  "challengers": [row.get("id") for row in breached]}},
                    tenant_id=tenant_id)
            except Exception:  # pragma: no cover - audit is best effort
                pass
            logger.error("auto-rolled back experiment %s (%s): %s",
                         experiment["id"], tenant_id, "; ".join(reasons))
            stopped.append({"tenant_id": tenant_id, "experiment_id": experiment["id"],
                            "breaches": reasons})
    return {"checked": checked, "stopped": stopped}


async def _scheduled_auto_rollback(storage: Storage) -> Dict[str, Any]:
    return await asyncio.to_thread(auto_rollback_experiments, storage)


async def _scheduled_slo_check(storage: Storage) -> Dict[str, Any]:
    if not _IS_LEADER:
        return {"skipped": "not_leader"}
    return await asyncio.to_thread(check_slos, storage)


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
        max_rows=env.env_int("REPORT_MAX_ROWS", 100000),
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


def _scheduled_relay_outbox(storage: Storage) -> None:
    """Move committed outbox rows to Kafka. One pass; the interval drives the loop.

    Swallows its exceptions on purpose: a broker outage must not kill the scheduler thread and
    take every other job with it. Nothing is lost by returning early — the rows stay unsent and
    the next tick retries them, which is the entire reason the outbox is durable.
    """
    if not is_leader():
        return
    try:
        from . import decision_outbox

        result = decision_outbox.relay_once(storage)
        if result.get("failed"):
            logger.warning("decision outbox relay: %s sent, %s failed",
                           result.get("sent"), result.get("failed"))
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("decision outbox relay pass failed")


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
    scheduler.add_job(retry_outbox, "interval", seconds=env.env_int("OUTBOX_RETRY_SECONDS", 60),
                      id="email-outbox-retry", args=[storage], replace_existing=True)
    # The decision outbox relay (parity P09). LEADER-GATED, and that is not optional: Kafka
    # orders within a partition and the outbox is keyed per tenant, so two replicas relaying the
    # same rows concurrently would interleave one tenant's decisions out of order — the single
    # ordering guarantee the partition key exists to provide.
    #
    # Registered unconditionally but a no-op without KAFKA_BOOTSTRAP_SERVERS (see
    # decision_outbox.relay_once), matching how every other optional dependency degrades here.
    scheduler.add_job(_scheduled_relay_outbox, "interval",
                      seconds=env.env_int("DECISION_OUTBOX_RELAY_SECONDS", 5),
                      id="decision-outbox-relay", args=[storage], replace_existing=True)
    scheduler.add_job(_scheduled_archive_decisions, "interval",
                      hours=env.env_int("DECISION_ARCHIVE_INTERVAL_HOURS", 24),
                      id="decision-archive", args=[storage], replace_existing=True)
    scheduler.add_job(_scheduled_slo_check, "interval",
                      minutes=env.env_int("SLO_CHECK_INTERVAL_MINUTES", 15),
                      id="slo-check", args=[storage], replace_existing=True)
    # Off by default. An automated halt is the right behaviour and a surprising one to inherit
    # silently on upgrade -- a workspace mid-experiment would find it stopped by a job it never
    # enabled. Opt in with EXPERIMENT_AUTO_ROLLBACK=1.
    if env.env_bool("EXPERIMENT_AUTO_ROLLBACK", False):
        scheduler.add_job(_scheduled_auto_rollback, "interval",
                          minutes=env.env_int("EXPERIMENT_AUTO_ROLLBACK_MINUTES", 10),
                          id="experiment-auto-rollback", args=[storage], replace_existing=True)
    scheduler.add_job(_scheduled_prune_saml_assertions, "interval",
                      hours=env.env_int("SAML_PRUNE_INTERVAL_HOURS", 6),
                      id="saml-assertion-prune", args=[storage], replace_existing=True)
    scheduler.start()
