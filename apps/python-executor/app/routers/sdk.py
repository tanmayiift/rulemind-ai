"""SDK edge + execution endpoints — experience manifest, health, bundle + decision-block serving,
server-side decide, on-device execution sync/get/resume, event + decision batch ingest, and the
API-level execution resume/detail. Extracted verbatim from app/main.py.

Stable helpers/models/metrics imported by value from app.main (they internally read the live
main.storage); direct storage calls use main.storage live. Compiler/experience/executor/reviews
helpers imported from their source modules."""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request, Response

from .. import main
from ..compiler import (
    BundleCompilationError,
    NoProductionAssetsError,
    compile_bundle,
    compile_policy_block,
    render_block_response,
    render_bundle_response,
    sign_block_payload,
)
from ..executor import ExecutionContext
from ..experience_studio import ADMIN_ENTITY_SCHEMAS, build_experience_manifest
from ..logic import json_dumps
from ..main import (
    BUNDLE_SYNCS_TOTAL,
    DECISION_LATENCY,
    DECISIONS_INGESTED_TOTAL,
    DECISIONS_TOTAL,
    EVENTS_INGESTED_TOTAL,
    SDK_DECISIONS_BATCH_MAX,
    SdkDecideRequest,
    SdkDecisionsBatchRequest,
    SdkEventsRequest,
    SdkExecutionSyncRequest,
    SdkResumeExecutionRequest,
    active_tenant_id,
    build_sdk_response,
    ensure_exists,
    maybe_create_sdk_decision,
    normalize_sdk_rule_result,
    parse_iso_datetime,
    persist_sdk_action_logs,
    public_api_base_url,
    sync_sdk_review_task,
    workflow_executor,
)
from ..reviews import submit_review_decision

router = APIRouter()


@router.get("/sdk/v1/experience-manifest")
def sdk_experience_manifest(request: Request) -> Dict[str, Any]:
    tenant_id = active_tenant_id(request)
    latest = main.storage.latest_bundle(tenant_id=tenant_id)
    return build_experience_manifest(tenant_id, public_api_base_url(request), latest_bundle_version=int(latest["version"]) if latest else 0)


@router.get("/sdk/v1/health")
def sdk_health(request: Request) -> Dict[str, Any]:
    latest = main.storage.latest_bundle(tenant_id=active_tenant_id(request))
    return {"status": "ok", "latestBundleVersion": latest["version"] if latest else 0}


@router.get("/sdk/v1/bundle")
def sdk_bundle(request: Request) -> Response:
    tenant_id = active_tenant_id(request)
    latest = main.storage.latest_bundle(tenant_id=tenant_id)
    if latest is None:
        try:
            latest = compile_bundle(main.storage, tenant_id, client_public_key=request.headers.get("x-client-public-key"), force=True)
        except NoProductionAssetsError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except BundleCompilationError as error:
            main.storage.add_error_event(
                {
                    "tenant_id": tenant_id,
                    "scope": "bundle",
                    "entity_type": "bundle",
                    "entity_id": error.entity_id,
                    "stage": "compile",
                    "message": str(error),
                    "details": {"entity_type": error.entity_type, "entity_id": error.entity_id},
                },
                tenant_id=tenant_id,
            )
            raise HTTPException(status_code=500, detail=str(error)) from error
        except Exception as error:
            main.storage.add_error_event(
                {
                    "tenant_id": tenant_id,
                    "scope": "bundle",
                    "entity_type": "bundle",
                    "stage": "compile",
                    "message": str(error),
                    "details": {},
                },
                tenant_id=tenant_id,
            )
            raise
    current_version = int(request.headers.get("x-bundle-version", "0") or "0")
    if int(latest["version"]) == current_version:
        BUNDLE_SYNCS_TOTAL.labels(status="not_modified").inc()
        return Response(status_code=304)
    content = latest["content"]
    response_payload = render_bundle_response(content, request.headers.get("x-client-public-key"))
    BUNDLE_SYNCS_TOTAL.labels(status="success").inc()
    return Response(content=json_dumps(response_payload), media_type="application/json")


@router.get("/sdk/v1/blocks/{policy_id}")
def sdk_policy_block(policy_id: str, request: Request) -> Response:
    """Serve a single policy as a self-contained, cacheable **decision block**.

    Unlike the full tenant bundle, a block carries exactly one production policy plus only the
    rules / scorecards / decision tables it references (and the compiled variables). A client
    fetches one block, caches it by its ETag (the block checksum), and evaluates that policy
    on-device with the same SDK evaluators. Supports conditional GET (If-None-Match → 304) and
    optional per-client encryption when an `X-Client-Public-Key` header is supplied.
    """
    tenant_id = active_tenant_id(request)
    block = compile_policy_block(
        main.storage, tenant_id, policy_id, client_public_key=request.headers.get("x-client-public-key")
    )
    if block is None:
        raise HTTPException(status_code=404, detail="No production block for policy '{0}'.".format(policy_id))

    etag = '"{0}"'.format(block["checksum"])
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})

    client_public_key = request.headers.get("x-client-public-key")
    if client_public_key:
        payload = render_block_response(block, client_public_key)
    else:
        # Signed plaintext: the client can inspect + evaluate the block directly, and verify the
        # RSA signature over its canonical bytes. Confidentiality comes from TLS/mTLS in transit.
        payload = {
            "kind": "decision_block",
            "policyId": block["policyId"],
            "blockVersion": block["blockVersion"],
            "blockId": block["blockId"],
            "checksum": block["checksum"],
            "compiledAt": block["compiledAt"],
            "expiresAt": block["expiresAt"],
            "signature": sign_block_payload(block),
            "block": block,
        }
    return Response(
        content=json_dumps(payload),
        media_type="application/json",
        headers={"ETag": etag, "Cache-Control": "private, max-age=300"},
    )


@router.post("/sdk/v1/decide")
def sdk_decide(request: SdkDecideRequest) -> Dict[str, Any]:
    policy = ensure_exists(main.storage.get_policy(request.policyId), "policy", request.policyId)
    started = time.perf_counter()
    ctx = asyncio.run(
        workflow_executor().execute(
            policy=policy,
            payload=request.payload,
            tenant_id=active_tenant_id(),
            user_id=request.userId,
            source="sdk_server",
            sdk_version=request.sdkVersion,
        )
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    ctx.total_latency_ms = latency_ms
    DECISIONS_TOTAL.labels(outcome=ctx.outcome, source="sdk_server").inc()
    DECISION_LATENCY.labels(source="sdk_server").observe(max(latency_ms, 1) / 1000)
    return build_sdk_response(ctx, request_id=request.requestId, source="sdk_server", latency_ms=latency_ms)


@router.post("/sdk/v1/executions/sync")
def sdk_sync_execution(request: SdkExecutionSyncRequest) -> Dict[str, Any]:
    tenant_id = active_tenant_id()
    existing = main.storage.get_workflow_execution(request.executionId, tenant_id=tenant_id)
    synced_review_task = sync_sdk_review_task(request, tenant_id)
    ctx = ExecutionContext(
        payload=copy.deepcopy(request.payload),
        tenant_id=tenant_id,
        policy_id=request.policyId,
        execution_id=request.executionId,
        user_id=request.userId,
        variables=copy.deepcopy(request.variables),
        rule_results=[normalize_sdk_rule_result(item) for item in request.ruleResults],
        scorecard_results=copy.deepcopy(request.scorecardResults),
        action_results=copy.deepcopy(request.actionResults),
        pending_operations=copy.deepcopy(request.pendingOperations),
        outcome=request.outcome,
        status=request.status,
        review_task_id=(synced_review_task or request.reviewTask or {}).get("id"),
        experiment_id=request.experimentId,
        experiment_variant=request.experimentVariant,
        step_trace=copy.deepcopy(request.trace),
        started_at=parse_iso_datetime(request.startedAt) or datetime.utcnow(),
        completed_at=parse_iso_datetime(request.completedAt),
        total_latency_ms=request.latencyMs,
    )
    payload = {
        "id": request.executionId,
        "tenant_id": tenant_id,
        "policy_id": request.policyId,
        "status": request.status,
        "context": ctx.to_dict(),
        "current_step_index": max(len(request.trace) - 1, 0),
        "trigger_type": request.source,
        "trigger_metadata": {"request_id": request.requestId},
        "started_at": ctx.started_at,
        "paused_at": datetime.utcnow() if request.status == "paused" else None,
        "completed_at": ctx.completed_at,
    }
    if existing:
        main.storage.update_workflow_execution(request.executionId, payload, tenant_id=tenant_id)
    else:
        main.storage.create_workflow_execution(payload, tenant_id=tenant_id)
    persist_sdk_action_logs(request.executionId, request.actionResults, tenant_id)
    maybe_create_sdk_decision(request, tenant_id, existing)
    main.storage.add_audit_event(
        {
            "tenant_id": tenant_id,
            "event_type": "sdk_execution_synced",
            "entity_type": "workflow_execution",
            "entity_id": request.executionId,
            "detail": "SDK execution synchronized from device.",
            "metadata": {"status": request.status, "policy_id": request.policyId, "source": request.source},
        },
        tenant_id=tenant_id,
    )
    return build_sdk_response(ctx, request_id=request.requestId, source=request.source, latency_ms=request.latencyMs)


@router.get("/sdk/v1/executions/{execution_id}")
def sdk_get_execution(execution_id: str) -> Dict[str, Any]:
    execution = main.storage.get_workflow_execution(execution_id, tenant_id=active_tenant_id())
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found.")
    ctx = ExecutionContext.from_dict(execution["context"])
    return build_sdk_response(
        ctx,
        request_id=execution.get("trigger_metadata", {}).get("request_id"),
        source=execution.get("trigger_type") or "sdk_edge",
        latency_ms=ctx.total_latency_ms,
    )


@router.post("/sdk/v1/executions/{execution_id}/resume")
def sdk_resume_execution(execution_id: str, request: SdkResumeExecutionRequest) -> Dict[str, Any]:
    execution = main.storage.get_workflow_execution(execution_id, tenant_id=active_tenant_id())
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found.")
    ctx = ExecutionContext.from_dict(execution["context"])
    if request.decision and ctx.review_task_id:
        result = submit_review_decision(
            main.storage,
            ctx.review_task_id,
            request.response,
            request.reviewerId or "sdk",
            request.decision,
        )
        stored = main.storage.get_workflow_execution(execution_id, tenant_id=ctx.tenant_id)
        if not stored:
            raise HTTPException(status_code=404, detail="Execution not found after resume.")
        resumed_ctx = ExecutionContext.from_dict(stored["context"])
        return build_sdk_response(resumed_ctx, source="sdk_edge_resume", latency_ms=resumed_ctx.total_latency_ms)
    policy = main.storage.get_policy(ctx.policy_id, tenant_id=ctx.tenant_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found.")
    resumed = asyncio.run(
        workflow_executor().execute(policy=policy, payload=ctx.payload, tenant_id=ctx.tenant_id, resume_from=ctx, source="sdk_edge_resume")
    )
    return build_sdk_response(resumed, source="sdk_edge_resume", latency_ms=resumed.total_latency_ms)


@router.post("/sdk/v1/events")
def sdk_events(request: SdkEventsRequest) -> Dict[str, int]:
    count = main.storage.add_sdk_events(request.events)
    EVENTS_INGESTED_TOTAL.inc(count)
    return {"received": count, "processed": count}


@router.post("/sdk/v1/decisions")
def sdk_decisions_batch(request: SdkDecisionsBatchRequest) -> Dict[str, Any]:
    """Ingest a batch of on-device decisions from a device's local outbox.

    Idempotent and retry-safe: a decision whose client-stable `id` already exists (or
    repeats within the batch) is acknowledged but NOT duplicated — so a device can retry
    with exponential backoff without ever double-counting. The response's `acked` list is
    exactly the ids the device may now clear locally. Requires the `decide` capability."""
    if len(request.decisions) > SDK_DECISIONS_BATCH_MAX:
        raise HTTPException(status_code=413, detail=f"Batch exceeds the maximum of {SDK_DECISIONS_BATCH_MAX} decisions.")
    result = main.storage.add_decisions_batch(request.decisions, tenant_id=active_tenant_id())
    DECISIONS_INGESTED_TOTAL.labels(result="inserted").inc(result["inserted"])
    DECISIONS_INGESTED_TOTAL.labels(result="duplicate").inc(result["duplicates"])
    return {
        "received": result["received"],
        "inserted": result["inserted"],
        "duplicates": result["duplicates"],
        "acked": result["acked"],
    }


@router.post("/api/v1/executions/{execution_id}/resume")
def resume_execution(execution_id: str) -> Dict[str, Any]:
    execution = main.storage.get_workflow_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found.")
    ctx = ExecutionContext.from_dict(execution["context"])
    policy = main.storage.get_policy(ctx.policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found.")
    result = asyncio.run(workflow_executor().execute(policy=policy, payload=ctx.payload, tenant_id=ctx.tenant_id, resume_from=ctx, source="api"))
    return {"executionId": result.execution_id, "outcome": result.outcome, "status": result.status}


@router.get("/api/v1/executions/{execution_id}")
def get_execution_detail(execution_id: str) -> Dict[str, Any]:
    execution = main.storage.get_workflow_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found.")
    ctx = ExecutionContext.from_dict(execution["context"])
    detail = build_sdk_response(
        ctx,
        request_id=execution.get("trigger_metadata", {}).get("request_id"),
        source=execution.get("trigger_type") or "api",
        latency_ms=ctx.total_latency_ms,
    )
    detail["actionLogs"] = main.storage.list_action_logs(execution_id=execution_id)
    detail["auditEvents"] = [event for event in main.storage.list_audit_events() if event.get("entity_id") == execution_id]
    detail["entitySchemas"] = ADMIN_ENTITY_SCHEMAS
    return detail
